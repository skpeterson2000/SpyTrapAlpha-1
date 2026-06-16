#!/usr/bin/env python3
"""Track_My_Tracker — alert daemon. Scores recent sightings, fires alerts.

    ./.venv/bin/python alertd.py                 # use config tiers, loop
    ./.venv/bin/python alertd.py --once          # single pass (cron/test)
    ./.venv/bin/python alertd.py --tiers LOW     # demo before you have
                                                 # multi-location recurrence

Runs alongside the sensor services; reads the shared sightings.db (WAL).
"""

import argparse
import logging
import sys
import time

from tmt import config as configmod
from tmt.alerts import AlertEngine
from tmt.db import Store

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RED = "\033[91m"; YEL = "\033[93m"; DIM = "\033[2m"; RST = "\033[0m"


def print_alert(alert):
    c = RED if alert["tier"] == "HIGH" else YEL
    t = time.strftime("%H:%M:%S", time.localtime(alert["ts"]))
    chans = ",".join(alert["channels"]) or "none"
    print(f"{c}[{t}] ALERT {alert['tier']:<4} {alert['radio']:<4} "
          f"{alert['identity']:<20} score={alert['score']} "
          f"-> [{chans}]{RST}\n        {alert['reason']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--poll", type=float, default=None,
                    help="override poll seconds")
    ap.add_argument("--tiers", default=None,
                    help="override alert tiers, e.g. LOW,MED,HIGH")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = configmod.load()
    if args.tiers:
        cfg["alert_tiers"] = [t.strip().upper() for t in args.tiers.split(",")]
    if args.poll:
        cfg["poll_seconds"] = args.poll

    store = Store(args.db) if args.db else Store()
    engine = AlertEngine(store, cfg, on_alert=print_alert)

    push = cfg["push"]
    print(f"SpyTrap - alertd | tiers={cfg['alert_tiers']} | "
          f"window={cfg['window_hours']}h | cooldown={cfg['cooldown_minutes']}m | "
          f"push={'on:'+push['provider'] if push.get('enabled') else 'off'}")
    print("Watching sightings.db; alerts -> audible/push/log/DB. Ctrl-C to stop.\n")

    try:
        while True:
            fired = engine.evaluate()
            if not args.once and not fired:
                ts = time.strftime("%H:%M:%S")
                print(f"{DIM}[{ts}] (pass clear){RST}", end="\r")
            if args.once:
                if not fired:
                    print("No alerts at the current tiers.")
                break
            time.sleep(cfg["poll_seconds"])
    except KeyboardInterrupt:
        pass
    finally:
        store.close()


if __name__ == "__main__":
    main()
