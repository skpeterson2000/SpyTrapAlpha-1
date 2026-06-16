#!/usr/bin/env python3
"""Track_My_Tracker — live BLE scan entry point.

Usage:
    ./.venv/bin/python scan.py --session home --duration 30
    ./.venv/bin/python scan.py --session "drive-2026-06-15"   # runs until Ctrl-C

A `session` tags this run; comparing tracker sightings ACROSS sessions taken at
different places is how we answer the real question — "is something following
me?" — so name sessions by where/when you took them.
"""

import argparse
import asyncio
import sys
import time

from tmt.ble_sensor import BLESensor
from tmt.db import Store

# Terminals/locale here may be latin-1; device names can be arbitrary bytes.
# Never let an un-encodable glyph crash a long-running scan.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

STAR = "\033[93m"   # yellow for tracker hits
DIM = "\033[2m"
RST = "\033[0m"


def print_event(ts, address, name, rssi, tracker, first_seen):
    t = time.strftime("%H:%M:%S", time.localtime(ts))
    label = name or "(no name)"
    if tracker in {"tile", "samsung_smarttag", "apple_findmy_separated",
                   "apple_findmy_nearby", "google_fmdn"}:
        print(f"{STAR}[{t}] !! TRACKER {tracker:<24} {address}  "
              f"{rssi:>4} dBm  {label}{RST}")
    elif first_seen:
        tag = f"  ({tracker})" if tracker else ""
        print(f"{DIM}[{t}]   new device   {address}  {rssi:>4} dBm  "
              f"{label}{tag}{RST}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=time.strftime("session-%Y%m%d-%H%M%S"))
    ap.add_argument("--duration", type=float, default=0.0,
                    help="seconds to scan (0 = until Ctrl-C)")
    ap.add_argument("--rotate-minutes", type=float, default=0.0,
                    help="auto-segment the session into time buckets of N "
                         "minutes (0 = one fixed session). Used by the "
                         "continuous logger so the scorer gets distinct windows.")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    base = args.session
    if args.rotate_minutes > 0:
        bucket = args.rotate_minutes * 60.0

        def session():
            now = time.time()
            start = now - (now % bucket)
            return base + "-" + time.strftime("%Y%m%d-%H%M",
                                              time.localtime(start))
    else:
        session = base

    store = Store(args.db) if args.db else Store()
    sensor = BLESensor(store, session=session, on_event=print_event)

    print(f"SpyTrap - BLE scan  | session={args.session}  "
          f"| duration={'inf' if args.duration == 0 else f'{args.duration:g}s'}")
    print("Logging every advertisement; !! flags known tracker signatures. "
          "Ctrl-C to stop.\n")
    try:
        await sensor.run(duration=args.duration)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        store.close()
        print(f"\nStored {sensor.count} sightings "
              f"from {len(sensor._seen)} distinct addresses -> {store.path}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
