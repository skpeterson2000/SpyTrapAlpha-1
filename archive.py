#!/usr/bin/env python3
"""Track_My_Tracker — rotate the capture log: snapshot, digest, then wipe.

    ./.venv/bin/python archive.py                 # snapshot + digest only
    ./.venv/bin/python archive.py --wipe --yes    # ...then empty the live log

WHY ROTATE AT ALL
The sighting log is append-only on purpose, but it is bulky and overwhelmingly
redundant: 2,001,475 rows resolved to 11,005 distinct identities -- about 182
near-identical repeat advertisements each -- and 181 of those identities ever
carried a position. The detection model was reworked to treat following as a
claim about PLACE, so rows collected almost entirely at one parked location
cannot settle anything it now asks. Carrying them costs 400+ MB and slows every
scoring query for an answer they cannot give.

WHAT IS NEVER LOST
1. A full VACUUM'd snapshot of the whole database goes to archive/ first. The
   raw record is not deleted, it is moved -- every wipe is recoverable from it.
2. A digest row per identity is written to `history` in the LIVE database, so
   recognition survives the rotation. Meet a MAC again next year and the rig
   still answers when, how often and where it was seen, and `archives` names
   the snapshot file holding its raw rows. That is the whole point: the data
   leaves the hot path without the memory of it leaving.
3. `labels` (hand curation) and `track` (our own path) are never touched.

Rotations accumulate: a second run merges into the existing digest rather than
overwriting it, so first_seen keeps reaching back to the very first sighting.
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from tmt.db import Store, DEFAULT_PATH

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ARCHIVE_DIR = Path(__file__).resolve().parent / "archive"

# Emptied by a wipe. `labels`, `history` and `track` are deliberately absent.
WIPE_TABLES = ("sightings", "alerts", "decodes", "adsb")

# Services that hold the DB open. A wipe needs them stopped: VACUUM takes an
# exclusive lock that five live writers will never grant.
SERVICES = ("tmt-ble", "tmt-alertd", "tmt-api", "tmt-gps",
            "tmt-sdr@19481419", "tmt-decode@00000110")

DIGEST_SQL = """
SELECT radio, address,
       COUNT(*)                                        AS n_sightings,
       COUNT(DISTINCT session)                         AS n_sessions,
       MIN(ts)                                         AS first_seen,
       MAX(ts)                                         AS last_seen,
       SUM(CASE WHEN fix_state='ok' THEN 1 ELSE 0 END) AS n_positions,
       AVG(CASE WHEN fix_state='ok' THEN lat END)      AS lat,
       AVG(CASE WHEN fix_state='ok' THEN lon END)      AS lon,
       MAX(rssi)                                       AS rssi_max,
       AVG(rssi)                                       AS rssi_avg,
       MAX(tracker_type)                               AS tracker_type,
       MAX(name)                                       AS name
FROM sightings
WHERE address IS NOT NULL
GROUP BY radio, address
"""


def running_services():
    out = []
    for s in SERVICES:
        try:
            r = subprocess.run(["systemctl", "is-active", s],
                               capture_output=True, text=True, timeout=10)
            if r.stdout.strip() == "active":
                out.append(s)
        except Exception:
            pass
    return out


def snapshot(db_path, tag=None):
    """VACUUM the whole DB into archive/ — the untouched raw record."""
    ARCHIVE_DIR.mkdir(exist_ok=True)
    tag = tag or time.strftime("%Y%m%d-%H%M%S")
    dest = ARCHIVE_DIR / f"sightings-{tag}.db"
    if dest.exists():
        raise SystemExit(f"refusing to overwrite existing snapshot {dest}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)
    try:
        con.execute("VACUUM INTO ?", (str(dest),))
    finally:
        con.close()
    return dest


def build_digest(store, archive_name):
    """Fold every raw row into one row per identity, merged with prior eras."""
    rows = store.conn.execute(DIGEST_SQL).fetchall()
    now = time.time()
    merged = 0
    store.conn.execute("BEGIN IMMEDIATE")
    try:
        for (radio, ident, n, nsess, first, last, npos, lat, lon,
             rmax, ravg, ttype, name) in rows:
            prev = store.conn.execute(
                "SELECT n_sightings,n_sessions,first_seen,last_seen,"
                "n_positions,lat,lon,rssi_max,archives FROM history "
                "WHERE radio=? AND identity=?", (radio, ident)).fetchone()
            archives = [archive_name]
            if prev:
                merged += 1
                pn, pns, pf, pl, pnp, plat, plon, prm, parch = prev
                try:
                    old = json.loads(parch) if parch else []
                except ValueError:
                    old = []
                archives = old + [archive_name]
                # Position centroid is weighted by how many fixes each era had,
                # so a long-ago single fix cannot outvote a hundred recent ones.
                if lat is not None and plat is not None:
                    w, pw = (npos or 0), (pnp or 0)
                    if w + pw:
                        lat = (lat * w + plat * pw) / (w + pw)
                        lon = (lon * w + plon * pw) / (w + pw)
                elif lat is None:
                    lat, lon = plat, plon
                n = (pn or 0) + (n or 0)
                nsess = (pns or 0) + (nsess or 0)
                first = min(x for x in (first, pf) if x is not None)
                last = max(x for x in (last, pl) if x is not None)
                npos = (pnp or 0) + (npos or 0)
                rmax = max(x for x in (rmax, prm) if x is not None) \
                    if (rmax is not None or prm is not None) else None
            store.conn.execute(
                """INSERT INTO history (radio,identity,name,tracker_type,
                     n_sightings,n_sessions,first_seen,last_seen,n_positions,
                     lat,lon,rssi_max,rssi_avg,archives,archived_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(radio,identity) DO UPDATE SET
                     name=COALESCE(excluded.name, history.name),
                     tracker_type=COALESCE(excluded.tracker_type,
                                           history.tracker_type),
                     n_sightings=excluded.n_sightings,
                     n_sessions=excluded.n_sessions,
                     first_seen=excluded.first_seen,
                     last_seen=excluded.last_seen,
                     n_positions=excluded.n_positions,
                     lat=excluded.lat, lon=excluded.lon,
                     rssi_max=excluded.rssi_max, rssi_avg=excluded.rssi_avg,
                     archives=excluded.archives,
                     archived_ts=excluded.archived_ts""",
                (radio, ident, name, ttype, n, nsess, first, last, npos,
                 lat, lon, rmax, ravg, json.dumps(archives), now))
        store.conn.execute("COMMIT")
    except Exception:
        store.conn.execute("ROLLBACK")
        raise
    return len(rows), merged


def wipe(store):
    counts = {}
    store.conn.execute("BEGIN IMMEDIATE")
    try:
        for t in WIPE_TABLES:
            counts[t] = store.conn.execute(
                f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            store.conn.execute(f"DELETE FROM {t}")
        store.conn.execute("COMMIT")
    except Exception:
        store.conn.execute("ROLLBACK")
        raise
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.conn.execute("VACUUM")
    return counts


def main():
    ap = argparse.ArgumentParser(description="rotate the capture log")
    ap.add_argument("--db", default=str(DEFAULT_PATH))
    ap.add_argument("--tag", help="snapshot filename tag (default: timestamp)")
    ap.add_argument("--wipe", action="store_true",
                    help="empty sightings/alerts/decodes/adsb after archiving")
    ap.add_argument("--yes", action="store_true", help="required with --wipe")
    ap.add_argument("--force-running", action="store_true",
                    help="wipe even with services live (VACUUM will likely fail)")
    a = ap.parse_args()

    if a.wipe and not a.yes:
        raise SystemExit("--wipe needs --yes (this empties the live log)")
    if a.wipe and not a.force_running:
        live = running_services()
        if live:
            raise SystemExit(
                "refusing to wipe while these hold the database open:\n  "
                + "\n  ".join(live)
                + "\n\nstop them first:  sudo systemctl stop " + " ".join(live))

    before = os.path.getsize(a.db)
    print(f"database {a.db}  ({before/1024/1024:.0f} MiB)")

    dest = snapshot(a.db, a.tag)
    print(f"snapshot  -> {dest}  ({dest.stat().st_size/1024/1024:.0f} MiB)")

    store = Store(a.db)
    n, merged = build_digest(store, dest.name)
    print(f"digest    -> history: {n} identities "
          f"({merged} merged with an earlier era)")

    if not a.wipe:
        print("\nno --wipe given: live log untouched.")
        return 0

    counts = wipe(store)
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    after = os.path.getsize(a.db)
    print("wiped     -> " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    print(f"reclaimed -> {before/1024/1024:.0f} MiB "
          f"to {after/1024/1024:.1f} MiB")
    kept = {t: store.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("labels", "history", "track")}
    print("kept      -> " + ", ".join(f"{v} {k}" for k, v in kept.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
