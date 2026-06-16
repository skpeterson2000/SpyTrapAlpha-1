#!/usr/bin/env python3
"""Track_My_Tracker — GPS poller. Mirrors the latest gpsd fix to a tmpfs file
that every sensor reads to stamp sightings with location.

    ./.venv/bin/python gpsdaemon.py
"""

import sys
import time

from tmt import config as configmod
from tmt.gps import GpsPoller

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    g = configmod.load()["gps"]
    if not g.get("enabled"):
        print("gps disabled in config"); return

    last = {"n": 0}

    def on_fix(fix):
        last["n"] += 1
        if fix["mode"] >= 2 and last["n"] % 10 == 1:
            print(f"fix {fix['lat']:.5f},{fix['lon']:.5f} "
                  f"mode={fix['mode']} spd={fix.get('speed')}")
        elif fix["mode"] < 2 and last["n"] % 30 == 1:
            print(f"connected, no fix yet (mode={fix['mode']}) — "
                  f"gpsd has no device/lock")

    print(f"gps poller -> {g['host']}:{g['port']} -> {g['fix_file']}")
    GpsPoller(g["host"], g["port"], g["fix_file"], on_fix=on_fix).run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
