#!/usr/bin/env python3
"""Track_My_Tracker — GPS poller. Mirrors the latest gpsd fix to a tmpfs file
that every sensor reads to stamp sightings with location, and writes our own
breadcrumb track (tmt/track.py) so the path itself is durable.

    ./.venv/bin/python gpsdaemon.py
"""

import sys
import time

from tmt import config as configmod
from tmt.db import Store
from tmt.gps import GpsPoller
from tmt.track import TrackLogger

try:
    # line_buffering: under systemd stdout is a pipe, so the default block
    # buffering held startup and trip lines back until 8 KiB had accumulated.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                           line_buffering=True)
except Exception:
    pass

M_PER_MILE = 1609.344


def _fmt_trip(s):
    if not s:
        return ""
    mi = (s["distance_m"] or 0) / M_PER_MILE
    dur = (s["duration_s"] or 0) / 60.0
    mov = (s["moving_s"] or 0) / 60.0
    mph = ((s["distance_m"] or 0) / s["moving_s"] * 2.23694) if s["moving_s"] else 0.0
    top = (s["max_speed_mps"] or 0) * 2.23694
    return (f"{mi:.2f} mi in {dur:.0f} min ({mov:.0f} min moving, "
            f"avg {mph:.0f} mph, max {top:.0f} mph, {s['points']} points)")


def main():
    cfg = configmod.load()
    g = cfg["gps"]
    if not g.get("enabled"):
        print("gps disabled in config"); return

    last = {"n": 0}
    store = None
    tracker = None
    tcfg = cfg.get("track", {})
    if tcfg.get("enabled", True):
        # The poller is the only process that sees every fix, so it is the only
        # honest place to measure a path. It writes at most ~1 row/s and buffers
        # them, so it adds a negligible sixth writer to the WAL (see tmt/db.py).
        store = Store()
        tracker = TrackLogger(store, tcfg)
        resumed = tracker.resume()
        if resumed:
            print(f"track: rejoined trip {resumed} still in progress")

    def on_fix(fix):
        last["n"] += 1
        if tracker is not None:
            try:
                ev = tracker.on_fix(fix)
            except Exception as e:                 # never take the poller off
                ev = None                          # the air over a log write
                print(f"track: write failed: {e!r}", flush=True)
            if ev and ev["event"] == "trip_start":
                print(f"track: TRIP {ev['trip']} started", flush=True)
            elif ev and ev["event"] == "trip_end":
                print(f"track: TRIP {ev['trip']} ended — "
                      f"{_fmt_trip(ev['summary'])}", flush=True)
        if fix["mode"] >= 2 and last["n"] % 10 == 1:
            print(f"fix {fix['lat']:.5f},{fix['lon']:.5f} "
                  f"mode={fix['mode']} spd={fix.get('speed')}")
        elif fix["mode"] < 2 and last["n"] % 30 == 1:
            print(f"connected, no fix yet (mode={fix['mode']}) — "
                  f"gpsd has no device/lock")

    print(f"gps poller -> {g['host']}:{g['port']} -> {g['fix_file']}")
    if tracker is not None:
        print(f"track log -> {store.path} (crumb {tcfg.get('min_dist_m', 25)} m "
              f"moving / {tcfg.get('park_heartbeat_seconds', 30)} s parked)")

    def on_reject(problem, fix):
        print(f"gps REJECTED fix: {problem}  (mode={fix.get('mode')} "
              f"lat={fix.get('lat')} lon={fix.get('lon')}) — publishing no "
              f"position rather than a wrong one", flush=True)

    try:
        GpsPoller(g["host"], g["port"], g["fix_file"], on_fix=on_fix,
                  on_reject=on_reject).run()
    finally:
        if tracker is not None:
            try:
                tracker.close()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
