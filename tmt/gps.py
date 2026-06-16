"""gpsd client: hold the latest fix and share it with every sensor.

A single poller (gpsdaemon.py / tmt-gps.service) streams TPV reports from a
gpsd and writes the latest fix to a small tmpfs file. Each sensor process reads
that file (cached) via read_fix() when storing a sighting, so all of BLE / SDR /
decode get stamped with lat/lon without any per-sensor wiring.

Reachability + protocol verified against TowerWitch gpsd 3.22.
"""

import json
import os
import socket
import time
from pathlib import Path


def read_fix(path, max_age_seconds=30):
    """Return the current fix dict if present and fresh, else None.

    None when: file missing, no 2D/3D fix (lat/lon null), or older than
    max_age_seconds (so we never stamp a sighting with a stale position).
    """
    try:
        d = json.loads(Path(path).read_text())
    except Exception:
        return None
    if d.get("lat") is None or d.get("lon") is None:
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

    def __init__(self, host, port, fix_file, on_fix=None):
        self.host = host
        self.port = port
        self.fix_file = Path(fix_file)
        self.on_fix = on_fix or (lambda *a, **k: None)

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
            _atomic_write(self.fix_file, fix)
            self.on_fix(fix)
