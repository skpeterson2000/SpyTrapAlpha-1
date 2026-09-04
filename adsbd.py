#!/usr/bin/env python3
"""SpyTrap — ADS-B aircraft sensor. Reads dump1090's aircraft.json.

    ./.venv/bin/python adsbd.py                 # loop (service uses this)
    ./.venv/bin/python adsbd.py --duration 20   # short test run
"""

import argparse
import sys
import time

from tmt import config as configmod
from tmt.adsb_sensor import ADSBSensor
from tmt.db import Store

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=0.0)
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    cfg = configmod.load()["adsb"]
    if not cfg.get("enabled"):
        print("adsb disabled in config"); return

    bucket = 15 * 60.0           # 15-min session rotation, matching other sensors

    def session():
        now = time.time(); start = now - (now % bucket)
        return "adsb-" + time.strftime("%Y%m%d-%H%M", time.localtime(start))

    store = Store(args.db) if args.db else Store()

    def on_event(n, ts):
        print(f"[{time.strftime('%H:%M:%S', time.localtime(ts))}] {n} aircraft in view")

    sensor = ADSBSensor(store, session=session, json_path=cfg["json"],
                        mirror_seconds=cfg["mirror_seconds"], on_event=on_event)
    print(f"SpyTrap - ADS-B | {cfg['json']} | poll {cfg['poll_seconds']}s | "
          f"loiter>={cfg['loiter_minutes']}min within {cfg['loiter_radius_km']}km")
    try:
        sensor.run(poll_seconds=cfg["poll_seconds"], duration=args.duration)
    except KeyboardInterrupt:
        pass
    finally:
        store.close()
        print(f"\nRecorded {sensor.count} aircraft observations -> {store.path}")


if __name__ == "__main__":
    main()
