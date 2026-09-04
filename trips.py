#!/usr/bin/env python3
"""Track_My_Tracker — read the breadcrumb track: trips, odometer, export.

    ./.venv/bin/python trips.py                 # last 7 days
    ./.venv/bin/python trips.py --days 30
    ./.venv/bin/python trips.py --trip 1788500000
    ./.venv/bin/python trips.py --gpx 1788500000 > drive.gpx
    ./.venv/bin/python trips.py --now           # what the rig thinks right now

The numbers come from the `trips` view over the `track` table (see tmt/db.py);
this file only formats them. duration is wall clock including stops, moving is
time above the stop threshold — average speed is computed against the latter,
because dividing a distance by time spent parked at a light is not a speed.
"""

import argparse
import sys
import time

from tmt import config as configmod
from tmt.db import Store
from tmt.gps import read_fix_state

try:                        # this box's default stdout encoding is latin-1
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

M_PER_MILE = 1609.344
MPS_TO_MPH = 2.2369362920544


def _fmt_dur(sec):
    sec = int(sec or 0)
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{sec % 60:02d}s"


def _fmt_when(ts):
    return time.strftime("%a %d %b %H:%M", time.localtime(ts))


class Units:
    def __init__(self, metric):
        self.metric = metric
        self.dist = "km" if metric else "mi"
        self.speed = "km/h" if metric else "mph"

    def d(self, m):
        return (m or 0) / (1000.0 if self.metric else M_PER_MILE)

    def s(self, mps):
        return (mps or 0) * (3.6 if self.metric else MPS_TO_MPH)


def show_trips(store, u, since, limit):
    rows = store.recent_trips(since_ts=since, limit=limit)
    if not rows:
        print("no trips recorded yet — the track log needs the rig to move.")
        return
    print(f"{'trip':<16} {'started':<18} {'dist':>8} {'dur':>8} {'moving':>8} "
          f"{'avg':>7} {'max':>7} {'pts':>6}")
    for t in rows:
        avg = u.s(t["distance_m"] / t["moving_s"]) if t["moving_s"] else 0.0
        print(f"{t['id']:<16} {_fmt_when(t['started']):<18} "
              f"{u.d(t['distance_m']):>7.2f}{'':1} {_fmt_dur(t['duration_s']):>8} "
              f"{_fmt_dur(t['moving_s']):>8} {avg:>6.0f}{'':1} "
              f"{u.s(t['max_speed_mps']):>6.0f}{'':1} {t['points']:>6}")
    tot_d = sum(t["distance_m"] or 0 for t in rows)
    tot_m = sum(t["moving_s"] or 0 for t in rows)
    print(f"{'':16} {len(rows):>13} trips {u.d(tot_d):>7.2f} {u.dist} "
          f"total, {_fmt_dur(tot_m)} moving")


def show_odometer(store, u, since):
    rows = store.track_odometer(since_ts=since)
    if not rows:
        return
    print(f"\n{'day':<12} {'dist':>9} {'moving':>9} {'trips':>6} {'max':>7}")
    for d in rows:
        print(f"{d['day']:<12} {u.d(d['distance_m']):>8.2f}{'':1} "
              f"{_fmt_dur(d['moving_s']):>9} {d['trips']:>6} "
              f"{u.s(d['max_speed_mps']):>6.0f}{'':1}")


def show_trip(store, u, trip_id):
    t = store.trip_summary(trip_id)
    if not t:
        print(f"no trip {trip_id}"); return 1
    pts = store.track_points(t["started"], t["ended"], trip=trip_id)
    avg = u.s(t["distance_m"] / t["moving_s"]) if t["moving_s"] else 0.0
    print(f"trip {t['id']}")
    print(f"  {_fmt_when(t['started'])}  ->  {_fmt_when(t['ended'])}")
    print(f"  from {t['start_lat']:.5f},{t['start_lon']:.5f} "
          f"to {t['end_lat']:.5f},{t['end_lon']:.5f}")
    print(f"  {u.d(t['distance_m']):.2f} {u.dist} in {_fmt_dur(t['duration_s'])} "
          f"({_fmt_dur(t['moving_s'])} moving)")
    print(f"  avg {avg:.1f} {u.speed}   max {u.s(t['max_speed_mps']):.1f} "
          f"{u.speed}   {t['points']} points")
    stops = [p for p in pts if not p["moving"]]
    if stops:
        print(f"  {len(stops)} stopped sample(s) inside the trip "
              f"(lights, fuel — kept in duration, excluded from moving)")
    return 0


def gpx(store, trip_id):
    t = store.trip_summary(trip_id)
    if not t:
        print(f"no trip {trip_id}", file=sys.stderr); return 1
    pts = store.track_points(t["started"], t["ended"], trip=trip_id)
    iso = lambda ts: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="Track_My_Tracker" '
           'xmlns="http://www.topografix.com/GPX/1/1">',
           f'  <trk><name>trip {trip_id}</name><trkseg>']
    for p in pts:
        ele = f'<ele>{p["alt"]:.1f}</ele>' if p["alt"] is not None else ""
        out.append(f'    <trkpt lat="{p["lat"]:.7f}" lon="{p["lon"]:.7f}">'
                   f'{ele}<time>{iso(p["ts"])}</time></trkpt>')
    out += ['  </trkseg></trk>', '</gpx>']
    print("\n".join(out))
    return 0


def show_now(store, u):
    cfg = configmod.load()
    g = cfg["gps"]
    fix, state = read_fix_state(g.get("fix_file"), g.get("max_age_seconds", 30),
                                g.get("enabled", False))
    if fix:
        print(f"fix {fix['lat']:.5f},{fix['lon']:.5f}  mode={fix['mode']}  "
              f"{u.s(fix.get('speed')):.1f} {u.speed}  "
              f"age {time.time() - fix['ts']:.0f}s")
    else:
        print(f"no usable fix: {state}")
    row = store.last_track_row()
    if not row:
        print("track: no crumbs yet"); return
    age = time.time() - row["ts"]
    if row["trip"] is not None:
        t = store.trip_summary(row["trip"])
        print(f"track: TRIP {row['trip']} in progress — "
              f"{u.d(t['distance_m']):.2f} {u.dist}, "
              f"{_fmt_dur(t['duration_s'])} so far")
    else:
        print(f"track: parked (last crumb {age:.0f}s ago)")


def main():
    ap = argparse.ArgumentParser(description="breadcrumb track reports")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--trip", type=int)
    ap.add_argument("--gpx", type=int, metavar="TRIP")
    ap.add_argument("--now", action="store_true")
    ap.add_argument("--km", action="store_true", help="metric instead of miles")
    ap.add_argument("--db", help="read a different sightings.db")
    a = ap.parse_args()

    store = Store(a.db) if a.db else Store()  # opening syncs the trips view
    u = Units(a.km)
    if a.gpx:
        return gpx(store, a.gpx)
    if a.trip:
        return show_trip(store, u, a.trip)
    if a.now:
        return show_now(store, u)
    since = time.time() - a.days * 86400
    show_trips(store, u, since, a.limit)
    show_odometer(store, u, since)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        pass
