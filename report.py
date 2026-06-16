#!/usr/bin/env python3
"""Track_My_Tracker — recurrence / threat report over collected sightings.

    ./.venv/bin/python report.py                 # all data
    ./.venv/bin/python report.py --hours 24      # last 24h only
    ./.venv/bin/python report.py --min-tier LOW  # hide noise

Ranks identities (BLE addresses + SDR frequencies) by how much they behave like
something following you: recurring across sessions, persistent over time, and/or
a known tracker class.
"""

import argparse
import sqlite3
import sys
import time

from tmt.db import DEFAULT_PATH, Store
from tmt.score import score_identities, tracker_class_activity

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BOLD = "\033[1m"; RST = "\033[0m"
TIER_COLOR = {"HIGH": "\033[91m", "MED": "\033[93m",
              "LOW": "\033[96m", "info": "\033[2m"}
TIER_ORDER = {"info": 0, "LOW": 1, "MED": 2, "HIGH": 3}


def load_rows(db, since_ts):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    q = "SELECT radio,address,rssi,ts,tracker_type,session FROM sightings"
    args = ()
    if since_ts:
        q += " WHERE ts >= ?"; args = (since_ts,)
    rows = [dict(r) for r in c.execute(q, args)]
    c.close()
    return rows


def fmt_ago(ts):
    d = time.time() - ts
    if d < 90:
        return f"{int(d)}s ago"
    if d < 5400:
        return f"{int(d/60)}m ago"
    if d < 172800:
        return f"{d/3600:.1f}h ago"
    return f"{d/86400:.1f}d ago"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_PATH))
    ap.add_argument("--hours", type=float, default=0.0,
                    help="only consider the last N hours (0 = all)")
    ap.add_argument("--min-tier", default="info",
                    choices=["info", "LOW", "MED", "HIGH"])
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    since = time.time() - args.hours * 3600 if args.hours else None
    rows = load_rows(args.db, since)
    if not rows:
        print("No sightings found. Run scan.py / sweep.py first.")
        return

    store = Store(args.db, check_same_thread=False)
    labels = store.get_labels()
    store.close()
    ranked = score_identities(rows, labels)
    floor = TIER_ORDER[args.min_tier]
    shown = [r for r in ranked if TIER_ORDER[r["tier"]] >= floor][:args.top]

    window = f"last {args.hours:g}h" if args.hours else "all time"
    n_sessions = len({r["session"] for r in rows if r["session"]})
    print(f"{BOLD}SpyTrap — threat report{RST}  "
          f"({window}, {len(rows)} sightings, {n_sessions} sessions)\n")

    print(f"{BOLD}Ranked suspects{RST}  "
          f"(identity = BLE address or SDR frequency)")
    print(f"  {'tier':<5} {'score':>5}  {'radio':<4} {'identity':<20} "
          f"{'sess':>4} {'span':>6} {'rssi':>5}  last / why")
    for r in shown:
        c = TIER_COLOR[r["tier"]]
        rssi = f"{r['rssi_max']}" if r["rssi_max"] is not None else "  -"
        why = "; ".join(r["reasons"]) or "single transient sighting"
        print(f"  {c}{r['tier']:<5}{RST} {r['score']:>5}  {r['radio']:<4} "
              f"{(r['address'] or '?'):<20} {r['n_sessions']:>4} "
              f"{r['span_h']:>5.1f}h {rssi:>5}  {fmt_ago(r['last'])} | {why}")
    if not shown:
        print("  (nothing at or above the chosen tier)")

    activity = tracker_class_activity(rows)
    if activity:
        print(f"\n{BOLD}Tracker-class activity{RST}  "
              f"(MAC-agnostic — survives AirTag MAC rotation)")
        for a in activity:
            print(f"  {a['tracker']:<26} sessions={a['n_sessions']:<3} "
                  f"addrs={a['n_addrs']:<4} sightings={a['n']}")

    print(f"\n{BOLD}Read this as:{RST} HIGH/MED = recurring and/or a known "
          "tracker across sessions. One bench session can't show recurrence — "
          "collect from several places/times, then re-run.")


if __name__ == "__main__":
    main()
