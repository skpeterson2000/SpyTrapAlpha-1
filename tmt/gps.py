"""gpsd client: hold the latest fix and share it with every sensor.

A single poller (gpsdaemon.py / tmt-gps.service) streams TPV reports from a
gpsd and writes the latest fix to a small tmpfs file. Each sensor process reads
that file (cached) via read_fix() when storing a sighting, so all of BLE / SDR /
decode get stamped with lat/lon without any per-sensor wiring.

Reachability + protocol verified against TowerWitch gpsd 3.22.
"""

import json
import math
import os
import socket
import time
from pathlib import Path

# A consumer receiver occasionally emits a wildly wrong position that is
# numerically valid — this rig logged one 1,785 km away, in Georgia, for a
# single sample. Reject a fix implying a speed no ground vehicle reaches.
MAX_IMPLIED_SPEED_MPS = 80.0     # ~290 km/h
MIN_JUMP_M = 500.0               # ignore the check for small hops (wander)


def _haversine_m(lat1, lon1, lat2, lon2):
    r1, r2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(r1) * math.cos(r2) * math.sin(dlam / 2) ** 2)
    return 2 * 6371000.0 * math.asin(math.sqrt(min(1.0, a)))


# Public alias: tmt/track.py measures path length with the very same maths
# that guards against teleports here, rather than keeping a second copy.
haversine_m = _haversine_m


def fix_problem(d):
    """Why this TPV is unusable as a position, or None if it is fine.

    gpsd cannot be taken at its word. Observed on this rig: lat 0.0 / lon 0.0
    reported with mode=3 (a claimed 3D fix) whenever the receiver loses lock —
    that is Null Island, in the Gulf of Guinea. Because the old check only
    rejected None, 46,310 sightings were stamped with an ocean position, which
    quietly poisons the "did this identity follow me between locations"
    analysis that recording position exists to support. A wrong position is
    worse than no position, so validate the numbers instead of the mode flag.
    """
    if not isinstance(d, dict):
        return "malformed"
    if (d.get("mode") or 0) < 2:
        return "no fix (mode < 2)"
    lat, lon = d.get("lat"), d.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return "lat/lon missing"
    if isinstance(lat, bool) or isinstance(lon, bool):
        return "lat/lon not numeric"
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return f"lat/lon out of range ({lat},{lon})"
    if lat == 0.0 and lon == 0.0:
        return "null island (0,0) - receiver reporting no real lock"
    return None


def valid_fix(d):
    return fix_problem(d) is None


# Why a sighting has no position. Stored per-row so a later analysis can tell
# "we were somewhere and did not know" from "GPS was switched off" — an
# undifferentiated NULL conflates them and hides a broken receiver for months.
FIX_OK = "ok"
FIX_NO_LOCK = "no_lock"        # gpsd talking, receiver has no usable position
FIX_STALE = "stale"            # last fix too old to stamp
FIX_DISABLED = "disabled"      # gps turned off in config
FIX_POLLER_DOWN = "poller_down"  # no fix file at all


def read_fix_state(path, max_age_seconds=30, enabled=True):
    """Return (fix_or_None, state) — the fix plus WHY when there isn't one."""
    if not enabled or not path:
        return None, FIX_DISABLED
    try:
        d = json.loads(Path(path).read_text())
    except Exception:
        return None, FIX_POLLER_DOWN
    if not valid_fix(d):
        return None, FIX_NO_LOCK
    if time.time() - d.get("ts", 0) > max_age_seconds:
        return None, FIX_STALE
    return d, FIX_OK


def read_fix(path, max_age_seconds=30):
    """Return the current fix dict if present, valid and fresh, else None.

    None when: file missing, the fix fails validation (see fix_problem), or it
    is older than max_age_seconds — so we never stamp a sighting with a stale
    or bogus position. Callers treat None as "location unknown", which the
    schema stores as NULL and is the honest answer.
    """
    try:
        d = json.loads(Path(path).read_text())
    except Exception:
        return None
    if not valid_fix(d):
        return None
    if time.time() - d.get("ts", 0) > max_age_seconds:
        return None
    return d


def _atomic_write(path: Path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


class GpsPoller:
    """Maintain a gpsd connection and mirror the latest TPV to fix_file."""

    def __init__(self, host, port, fix_file, on_fix=None, on_reject=None):
        self.host = host
        self.port = port
        self.fix_file = Path(fix_file)
        self.on_fix = on_fix or (lambda *a, **k: None)
        self.on_reject = on_reject or (lambda *a, **k: None)
        self._last_reject_log = 0.0
        self._last_good = None          # (lat, lon, ts) for the teleport check

    def run(self):
        try:                              # service provides this via RuntimeDirectory
            self.fix_file.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass
        while True:
            try:
                self._session()
            except Exception:
                time.sleep(5)            # reconnect on any drop

    def _session(self):
        s = socket.create_connection((self.host, self.port), timeout=10)
        s.settimeout(30)
        s.sendall(b'?WATCH={"enable":true,"json":true};\n')
        f = s.makefile("r")
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("class") != "TPV":
                continue
            fix = {
                "lat": obj.get("lat"),
                "lon": obj.get("lon"),
                "alt": obj.get("alt"),
                "speed": obj.get("speed"),
                "track": obj.get("track"),
                "mode": obj.get("mode", 0),   # 0/1 = no fix, 2 = 2D, 3 = 3D
                "gps_time": obj.get("time"),
                "ts": time.time(),
            }
            problem = fix_problem(fix)
            if problem is None and self._last_good is not None:
                plat, plon, pts = self._last_good
                dt = max(1e-3, fix["ts"] - pts)
                dist = _haversine_m(plat, plon, fix["lat"], fix["lon"])
                if dist > MIN_JUMP_M and dist / dt > MAX_IMPLIED_SPEED_MPS:
                    problem = (f"implausible jump: {dist/1000:.0f} km in "
                               f"{dt:.0f}s ({dist/dt:.0f} m/s)")
            fix["problem"] = problem
            # Always publish the record, INCLUDING a bad one tagged with why.
            # read_fix() is the gate that stops a bad position being used, so
            # writing it costs nothing and means the dashboard can say "no
            # lock: receiver reporting 0,0" instead of the far more confusing
            # "no fix file yet". A silent absence looks like a crashed poller.
            _atomic_write(self.fix_file, fix)
            if not problem:
                self._last_good = (fix["lat"], fix["lon"], fix["ts"])
            if problem:
                now = time.time()
                if now - self._last_reject_log > 60:   # don't flood the log
                    self._last_reject_log = now
                    self.on_reject(problem, fix)
                continue
            self.on_fix(fix)
