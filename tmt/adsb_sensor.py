"""ADS-B sensor: read dump1090's aircraft.json and store observations.

We consume dump1090's OUTPUT (its JSON), not the radio — so this coexists with
dump1090 owning the dongle, exactly like the gpsd poller. Each aircraft becomes
an `adsb` row (with the aircraft's own position) and, throttled, a sighting
keyed by ICAO hex so a recurring aircraft scores like any other identity.
"""

import json
import time
from pathlib import Path


def _first(a, keys):
    for k in keys:
        if a.get(k) is not None:
            return a[k]
    return None


def parse_aircraft(obj, max_seen=20.0):
    """Normalize dump1090 aircraft entries (mutability or -fa field names)."""
    out = []
    for a in obj.get("aircraft", []):
        hexid = a.get("hex")
        if not hexid:
            continue
        if a.get("seen") is not None and a["seen"] > max_seen:
            continue                     # stale entry, skip
        out.append({
            "hex": hexid.strip().lower(),
            "flight": (a.get("flight") or "").strip() or None,
            "lat": a.get("lat"),
            "lon": a.get("lon"),
            "alt": _first(a, ("alt_baro", "altitude", "alt")),
            "gs": _first(a, ("gs", "speed")),
            "track": a.get("track"),
            "rssi": a.get("rssi"),
        })
    return out


class ADSBSensor:
    def __init__(self, store, session, json_path, mirror_seconds=60,
                 on_event=None):
        self.store = store
        self.session = session
        self.json_path = Path(json_path)
        self.mirror_seconds = mirror_seconds
        self.on_event = on_event or (lambda *a, **k: None)
        self._last_mirror = {}
        self.count = 0

    def poll_once(self):
        try:
            obj = json.loads(self.json_path.read_text())
        except (OSError, ValueError):
            return 0
        session = self.session() if callable(self.session) else self.session
        now = time.time()
        seen = 0
        for ac in parse_aircraft(obj):
            mirror = (now - self._last_mirror.get(ac["hex"], 0)) >= self.mirror_seconds
            self.store.add_adsb(
                ts=now, hex=ac["hex"], flight=ac["flight"], lat=ac["lat"],
                lon=ac["lon"], alt=ac["alt"], gs=ac["gs"], track=ac["track"],
                rssi=ac["rssi"], session=session, mirror=mirror)
            if mirror:
                self._last_mirror[ac["hex"]] = now
            seen += 1
            self.count += 1
        if seen:
            self.on_event(n=seen, ts=now)
        return seen

    def run(self, poll_seconds=5, duration=0):
        start = time.time()
        while True:
            self.poll_once()
            if duration and time.time() - start >= duration:
                break
            time.sleep(poll_seconds)
