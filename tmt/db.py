"""Append-only SQLite store for radio sightings.

Every observation is a row; we never mutate history. Recurrence and threat
scoring are computed as queries over this log, so the raw record stays the
source of truth and detection logic can evolve without re-collecting data.

CONCURRENCY — why writes are buffered and flushed, never left open:
Five processes write this one file. SQLite's WAL allows many readers but only
ONE writer, and Python's sqlite3 opens an implicit transaction on the first
INSERT that is held until commit(). The BLE logger inserts per advertisement and
committed every 5s, so it held the write lock ~continuously; measured, other
writers saw a p50 wait of 4837 ms and a max of 10548 ms, which exceeds the
connect timeout. tmt-alertd died and was restarted 138 times in one day, and the
SDR sweep lost most of its cycles.

So: rows accumulate in memory and a flush writes them all inside ONE short
explicit transaction (~0.07 ms/row uncontended). The lock is held for
milliseconds per flush instead of for the whole batching window. Every write
also retries on SQLITE_BUSY rather than letting the exception kill a daemon —
contention must degrade throughput, never take a sensor off the air.
"""

import json
import random
import sqlite3
import time
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "sightings.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
    id            INTEGER PRIMARY KEY,
    ts            REAL    NOT NULL,        -- epoch seconds
    radio         TEXT    NOT NULL,        -- 'ble' | 'sdr' | ...
    address       TEXT,                    -- MAC (often randomized)
    address_type  TEXT,
    name          TEXT,
    rssi          INTEGER,
    tx_power      INTEGER,
    service_uuids TEXT,                    -- JSON list
    mfg_company   INTEGER,                 -- first manufacturer company id
    mfg_data      TEXT,                    -- JSON {company_id: hex}
    service_data  TEXT,                    -- JSON {uuid: hex}
    tracker_type  TEXT,                    -- classified label or NULL
    session       TEXT,                    -- run/location tag for "did it follow me"
    lat           REAL,                    -- gps latitude at sighting (or NULL)
    lon           REAL,                    -- gps longitude
    gps_mode      INTEGER,                 -- 2=2D, 3=3D fix; NULL/0/1 = none
    fix_state     TEXT                     -- ok|no_lock|stale|disabled|poller_down
);
CREATE INDEX IF NOT EXISTS idx_sightings_ts      ON sightings(ts);
CREATE INDEX IF NOT EXISTS idx_sightings_addr    ON sightings(address);
CREATE INDEX IF NOT EXISTS idx_sightings_tracker ON sightings(tracker_type);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY,
    ts          REAL    NOT NULL,        -- when the alert fired
    tier        TEXT,                    -- MED | HIGH | ...
    radio       TEXT,
    identity    TEXT,                    -- BLE address or SDR frequency
    tracker     TEXT,
    score       REAL,
    n_sessions  INTEGER,
    reason      TEXT,
    channels    TEXT                     -- JSON list of channels notified
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts       ON alerts(ts);
CREATE INDEX IF NOT EXISTS idx_alerts_identity ON alerts(identity);

-- User curation: tag an identity so the scorer can suppress the benign and
-- escalate the flagged. category drives behavior (see tmt/score.py):
--   mine|safe|ignore -> suppressed from alerts
--   watch            -> kept/pinned
--   threat           -> forced HIGH
CREATE TABLE IF NOT EXISTS labels (
    radio     TEXT NOT NULL,
    identity  TEXT NOT NULL,
    name      TEXT,
    category  TEXT,
    notes     TEXT,
    updated   REAL,
    PRIMARY KEY (radio, identity)
);

-- Decoded UNENCRYPTED ISM frames (rtl_433). Full structured frame retained for
-- the decode view; each also writes a sighting (radio='decode') so a recurring
-- device id (e.g. a TPMS sensor following you) scores like any other identity.
CREATE TABLE IF NOT EXISTS decodes (
    id        INTEGER PRIMARY KEY,
    ts        REAL NOT NULL,
    model     TEXT,
    dev_id    TEXT,
    identity  TEXT,                      -- model:id  (matches the sighting addr)
    freq_mhz  REAL,
    rssi      REAL,
    json      TEXT                       -- full decoded frame
);
CREATE INDEX IF NOT EXISTS idx_decodes_ts       ON decodes(ts);
CREATE INDEX IF NOT EXISTS idx_decodes_identity ON decodes(identity);

-- ADS-B aircraft observations (from dump1090). hex (ICAO) is a STABLE identity.
-- lat/lon here is the AIRCRAFT's position (vs sightings.lat/lon = ours), which
-- is what the loiter detector analyses. Each also mirrors to a sighting
-- (radio='adsb') so a recurring aircraft scores like any other identity.
CREATE TABLE IF NOT EXISTS adsb (
    id       INTEGER PRIMARY KEY,
    ts       REAL NOT NULL,
    hex      TEXT NOT NULL,
    flight   TEXT,
    lat      REAL,
    lon      REAL,
    alt      INTEGER,
    gs       REAL,
    track    REAL,
    rssi     REAL,
    session  TEXT
);
CREATE INDEX IF NOT EXISTS idx_adsb_ts  ON adsb(ts);
CREATE INDEX IF NOT EXISTS idx_adsb_hex ON adsb(hex);

-- OUR OWN path, sampled on the GPS's cadence instead of a radio's. A sighting
-- carries a position only when a radio happened to hear something in the same
-- moment the fix was good, which is why 0.15% of two million of them have one.
-- This table is the continuous record, so distance / duration / speed become
-- answerable at all, and tmt/score.py gets an observer path to interpolate
-- against rather than inferring movement from sparse opportunistic stamps.
--
-- dist_m is the TRUE PATH LENGTH accumulated from every 1 Hz fix since the
-- previous crumb, not the straight line between the two -- so widening the
-- crumb spacing costs storage, never accuracy. It is 0 while parked and NULL
-- across a gap: a stationary receiver wanders for ever, and summing that would
-- grow an odometer in an empty driveway.
CREATE TABLE IF NOT EXISTS track (
    id     INTEGER PRIMARY KEY,
    ts     REAL NOT NULL,        -- epoch seconds (our clock, as elsewhere)
    lat    REAL NOT NULL,
    lon    REAL NOT NULL,
    alt    REAL,                 -- metres
    speed  REAL,                 -- m/s from gpsd (derived when it omits one)
    track  REAL,                 -- heading, degrees true
    mode   INTEGER,              -- 2 = 2D, 3 = 3D
    dist_m REAL,                 -- path length since the previous crumb
    dt     REAL,                 -- seconds since the previous crumb
    moving INTEGER,              -- 1 = above the stop threshold at this crumb
    trip   INTEGER               -- trip id (its start epoch); NULL when parked
);
CREATE INDEX IF NOT EXISTS idx_track_ts   ON track(ts);
CREATE INDEX IF NOT EXISTS idx_track_trip ON track(trip, ts);

-- What we knew about an identity BEFORE the raw rows were archived away.
-- The sighting log is bulky and mostly redundant -- two million rows resolved
-- to 11,005 identities, ~182 near-identical repeat adverts each, collected
-- almost entirely at one parked location -- so it can be rotated out without
-- losing the answer to the only question the old rows can still settle: "have
-- we met this thing before, and where?" One digest row per identity survives
-- every wipe, and `archives` names the snapshot file holding its raw rows, so
-- a hit here always leads back to the full record. See archive.py.
CREATE TABLE IF NOT EXISTS history (
    radio        TEXT NOT NULL,
    identity     TEXT NOT NULL,
    name         TEXT,
    tracker_type TEXT,
    n_sightings  INTEGER,          -- rows archived, summed across rotations
    n_sessions   INTEGER,
    first_seen   REAL,
    last_seen    REAL,
    n_positions  INTEGER,          -- how many of those rows had a real fix
    lat          REAL,             -- centroid of the fixes we did have
    lon          REAL,
    rssi_max     INTEGER,
    rssi_avg     REAL,
    archives     TEXT,             -- JSON list of snapshot files holding it
    archived_ts  REAL,
    PRIMARY KEY (radio, identity)
);
CREATE INDEX IF NOT EXISTS idx_history_last ON history(last_seen);
"""

# One row per journey, derived rather than stored, so a change of mind about
# what counts as a trip is a code edit and not a re-collection. duration_s is
# wall clock from first crumb to last INCLUDING the red lights; moving_s counts
# only crumbs above the stop threshold. Both are kept because "the drive took
# 40 minutes" and "I drove for 31 of them" are different true answers, and
# average speed is honest only against the second.
TRIPS_VIEW = """
CREATE VIEW trips AS
SELECT t.trip                                    AS id,
       MIN(t.ts)                                 AS started,
       MAX(t.ts)                                 AS ended,
       MAX(t.ts) - MIN(t.ts)                     AS duration_s,
       COALESCE(SUM(t.dist_m), 0.0)              AS distance_m,
       COALESCE(SUM(CASE WHEN t.moving = 1 THEN t.dt END), 0.0) AS moving_s,
       MAX(t.speed)                              AS max_speed_mps,
       COUNT(*)                                  AS points,
       (SELECT lat FROM track s WHERE s.trip = t.trip ORDER BY s.ts LIMIT 1)
                                                 AS start_lat,
       (SELECT lon FROM track s WHERE s.trip = t.trip ORDER BY s.ts LIMIT 1)
                                                 AS start_lon,
       (SELECT lat FROM track s WHERE s.trip = t.trip ORDER BY s.ts DESC LIMIT 1)
                                                 AS end_lat,
       (SELECT lon FROM track s WHERE s.trip = t.trip ORDER BY s.ts DESC LIMIT 1)
                                                 AS end_lon
FROM track t
WHERE t.trip IS NOT NULL
GROUP BY t.trip
"""


_INSERT_SIGHTING = """INSERT INTO sightings
    (ts, radio, address, address_type, name, rssi, tx_power,
     service_uuids, mfg_company, mfg_data, service_data,
     tracker_type, session, lat, lon, gps_mode, fix_state)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

_INSERT_DECODE = """INSERT INTO decodes
    (ts,model,dev_id,identity,freq_mhz,rssi,json) VALUES (?,?,?,?,?,?,?)"""

_INSERT_TRACK = """INSERT INTO track
    (ts,lat,lon,alt,speed,track,mode,dist_m,dt,moving,trip)
    VALUES (?,?,?,?,?,?,?,?,?,?,?)"""

_INSERT_ADSB = """INSERT INTO adsb
    (ts,hex,flight,lat,lon,alt,gs,track,rssi,session)
    VALUES (?,?,?,?,?,?,?,?,?,?)"""


def _is_busy(exc):
    """True for SQLite's lock/busy errors, which are worth retrying."""
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


class Store:
    def __init__(self, path=DEFAULT_PATH, check_same_thread=True,
                 busy_timeout=30.0, max_buffer=500, init_schema=True):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, timeout=busy_timeout,
                                    check_same_thread=check_same_thread)
        # isolation_level=None turns OFF the driver's implicit BEGIN. We open
        # transactions explicitly at flush time instead, so an INSERT can never
        # leave the single WAL write lock held while we wait for more rows.
        self.conn.isolation_level = None
        # WAL lets the always-on logger and an ad-hoc scan/report coexist
        # without blocking each other on the single sightings.db.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(f"PRAGMA busy_timeout={int(busy_timeout * 1000)}")
        if init_schema:
            self.conn.executescript(SCHEMA)
            self._migrate()
        # Pending rows, flushed together in one short transaction. Buffered
        # rows are invisible to other processes until flush — same as the
        # uncommitted rows they replace — and are capped so memory and the
        # amount at risk on a crash both stay bounded.
        self._pending_sightings = []
        self._pending_decodes = []
        self._pending_adsb = []
        self._pending_track = []
        self._max_buffer = int(max_buffer)

        # GPS auto-stamping config (latest fix is shared via a tmpfs file).
        from . import config as _config
        g = _config.load().get("gps", {})
        self._gps_enabled = g.get("enabled", False)
        self._gps_fix_file = g.get("fix_file")
        self._gps_max_age = g.get("max_age_seconds", 30)
        self._fix_cache = (None, "disabled")
        self._fix_cache_ts = 0.0

    def _retry(self, fn, attempts=6, base=0.12):
        """Run a write, retrying on SQLITE_BUSY with jittered backoff.

        busy_timeout already makes SQLite wait; this is the outer belt so that
        losing a race still degrades throughput instead of killing the process.
        Jitter stops several sensors from retrying in lockstep forever.
        """
        for i in range(attempts):
            try:
                return fn()
            except sqlite3.OperationalError as e:
                if not _is_busy(e) or i == attempts - 1:
                    raise
                time.sleep(base * (2 ** i) + random.random() * 0.05)

    def _pending_count(self):
        return (len(self._pending_sightings) + len(self._pending_decodes)
                + len(self._pending_adsb) + len(self._pending_track))

    def _flush(self):
        """Write every buffered row in ONE short transaction."""
        if not self._pending_count():
            return
        sightings = self._pending_sightings
        decodes = self._pending_decodes
        adsb = self._pending_adsb
        track = self._pending_track

        def write():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                if sightings:
                    self.conn.executemany(_INSERT_SIGHTING, sightings)
                if decodes:
                    self.conn.executemany(_INSERT_DECODE, decodes)
                if adsb:
                    self.conn.executemany(_INSERT_ADSB, adsb)
                if track:
                    self.conn.executemany(_INSERT_TRACK, track)
                self.conn.execute("COMMIT")
            except Exception:
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

        # Clear the buffers only once the rows are durably written, so a failed
        # flush retries the same rows instead of dropping them.
        self._retry(write)
        self._pending_sightings = []
        self._pending_decodes = []
        self._pending_adsb = []
        self._pending_track = []

    def _maybe_flush(self):
        if self._pending_count() >= self._max_buffer:
            self._flush()

    def _migrate(self):
        """Add columns introduced after a DB was first created."""
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(sightings)")}
        for col, decl in (("lat", "REAL"), ("lon", "REAL"),
                          ("gps_mode", "INTEGER"), ("fix_state", "TEXT")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE sightings ADD COLUMN {col} {decl}")
        # A view holds no data, so redefining it is free -- and dropping it
        # first means the definition can never drift behind TRIPS_VIEW the way
        # a CREATE VIEW IF NOT EXISTS silently would.
        self._retry(lambda: self.conn.executescript(
            "DROP VIEW IF EXISTS trips;\n" + TRIPS_VIEW))

    def _current_fix(self):
        """Latest fresh GPS fix (cached ~3s) plus WHY, as (fix_or_None, state)."""
        now = time.time()
        if now - self._fix_cache_ts > 3.0:
            from .gps import read_fix_state
            self._fix_cache = read_fix_state(
                self._gps_fix_file, self._gps_max_age, self._gps_enabled)
            self._fix_cache_ts = now
        return self._fix_cache

    def add_sighting(self, *, radio, address=None, address_type=None, name=None,
                     rssi=None, tx_power=None, service_uuids=None,
                     manufacturer_data=None, service_data=None,
                     tracker_type=None, session=None, ts=None):
        mfg = manufacturer_data or {}
        sd = service_data or {}
        fix, fix_state = self._current_fix()
        lat = fix["lat"] if fix else None
        lon = fix["lon"] if fix else None
        gps_mode = fix["mode"] if fix else None
        self._pending_sightings.append((
            ts if ts is not None else time.time(),
            radio, address, address_type, name, rssi, tx_power,
            json.dumps(sorted(service_uuids or [])),
            next(iter(mfg), None),
            json.dumps({str(k): v.hex() for k, v in mfg.items()}),
            json.dumps({str(k): v.hex() for k, v in sd.items()}),
            tracker_type, session, lat, lon, gps_mode, fix_state,
        ))
        self._maybe_flush()

    def rows_since(self, since_ts):
        """Sighting dicts at/after since_ts — the engine's scoring window."""
        self._flush()          # a caller must always see its own writes
        cur = self.conn.execute(
            "SELECT radio,address,rssi,ts,tracker_type,session,name,"
            "lat,lon,fix_state "
            "FROM sightings WHERE ts >= ? ORDER BY ts", (since_ts,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def last_alert_ts(self, identity):
        """Most recent alert time for an identity (for cooldown), or None."""
        row = self.conn.execute(
            "SELECT MAX(ts) FROM alerts WHERE identity = ?", (identity,)
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def add_alert(self, *, ts, tier, radio, identity, tracker, score,
                  n_sessions, reason, channels):
        import json as _json
        row = (ts, tier, radio, identity, tracker, score, n_sessions, reason,
               _json.dumps(channels))

        def write():
            cur = self.conn.execute(
                """INSERT INTO alerts
                   (ts,tier,radio,identity,tracker,score,n_sessions,reason,
                    channels) VALUES (?,?,?,?,?,?,?,?,?)""", row)
            return cur.lastrowid

        # Alerts are single low-rate rows, so autocommit is fine; what matters
        # is that losing a lock race no longer kills alertd (138 restarts/day).
        return self._retry(write)

    def recent_alerts(self, since_ts=None, after_id=None, limit=100):
        q = "SELECT * FROM alerts"
        clauses, args = [], []
        if since_ts is not None:
            clauses.append("ts >= ?"); args.append(since_ts)
        if after_id is not None:
            clauses.append("id > ?"); args.append(after_id)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        cur = self.conn.execute(q, args)
        cols = [d[0] for d in cur.description]
        import json as _json
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            try:                                  # decode channels back to list
                d["channels"] = _json.loads(d["channels"]) if d["channels"] else []
            except (ValueError, TypeError):
                d["channels"] = []
            out.append(d)
        return out

    # ---- decoded ISM frames ----------------------------------------------
    def add_decode(self, *, ts, model, dev_id, identity, freq_mhz, rssi,
                   frame, session):
        import json as _json
        self._pending_decodes.append(
            (ts, model, dev_id, identity, freq_mhz, rssi, _json.dumps(frame)))
        # Mirror into sightings so a recurring decoded device id scores like
        # any other identity (recurrence / labels / alerts all apply). Both rows
        # land in the same flush, so the decode and its mirror are atomic.
        self.add_sighting(radio="decode", address=identity, name=model,
                          rssi=int(rssi) if rssi is not None else None,
                          session=session, ts=ts)
        self._flush()

    def recent_decodes(self, since_ts=None, limit=100):
        import json as _json
        q = "SELECT ts,model,dev_id,identity,freq_mhz,rssi,json FROM decodes"
        args = []
        if since_ts is not None:
            q += " WHERE ts >= ?"; args.append(since_ts)
        q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        out = []
        for ts, model, dev_id, identity, freq, rssi, j in self.conn.execute(q, args):
            try:
                frame = _json.loads(j) if j else {}
            except ValueError:
                frame = {}
            out.append({"ts": ts, "model": model, "dev_id": dev_id,
                        "identity": identity, "freq_mhz": freq, "rssi": rssi,
                        "frame": frame})
        return out

    # ---- ADS-B aircraft --------------------------------------------------
    def add_adsb(self, *, ts, hex, flight, lat, lon, alt, gs, track, rssi,
                 session, mirror=False):
        self._pending_adsb.append(
            (ts, hex, flight, lat, lon, alt, gs, track, rssi, session))
        if mirror:
            # Recurrence identity = the ICAO hex; name = callsign if known.
            self.add_sighting(radio="adsb", address=hex,
                              name=(flight or None),
                              rssi=int(rssi) if rssi is not None else None,
                              session=session, ts=ts)
        self._flush()

    def adsb_tracks_since(self, since_ts):
        """Per-aircraft point lists for loiter analysis: {hex: [rows...]}."""
        self._flush()
        cur = self.conn.execute(
            "SELECT ts,hex,flight,lat,lon,alt,gs,track,rssi FROM adsb "
            "WHERE ts >= ? ORDER BY ts", (since_ts,))
        cols = [d[0] for d in cur.description]
        out = {}
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            out.setdefault(d["hex"], []).append(d)
        return out

    # ---- our own track ---------------------------------------------------
    def add_track(self, *, ts, lat, lon, alt=None, speed=None, track=None,
                  mode=None, dist_m=None, dt=None, moving=None, trip=None):
        """Buffer one breadcrumb. The caller (TrackLogger) owns the cadence."""
        self._pending_track.append((ts, lat, lon, alt, speed, track, mode,
                                    dist_m, dt, moving, trip))
        self._maybe_flush()

    def last_track_row(self):
        """Newest crumb, so a restarted poller can rejoin a trip in progress."""
        self._flush()
        cur = self.conn.execute(
            "SELECT ts,lat,lon,alt,speed,track,mode,trip FROM track "
            "ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip([d[0] for d in cur.description], row))

    def trip_summary(self, trip_id):
        self._flush()
        cur = self.conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip([d[0] for d in cur.description], row))

    def recent_trips(self, since_ts=None, limit=50):
        self._flush()
        q, args = "SELECT * FROM trips", []
        if since_ts is not None:
            q += " WHERE started >= ?"; args.append(since_ts)
        q += " ORDER BY started DESC LIMIT ?"; args.append(limit)
        cur = self.conn.execute(q, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def track_points(self, start_ts, end_ts=None, trip=None):
        """Crumbs in a window -- the observer path for maps and for scoring."""
        self._flush()
        q = ("SELECT ts,lat,lon,alt,speed,track,mode,dist_m,dt,moving,trip "
             "FROM track WHERE ts >= ?")
        args = [start_ts]
        if end_ts is not None:
            q += " AND ts <= ?"; args.append(end_ts)
        if trip is not None:
            q += " AND trip = ?"; args.append(trip)
        q += " ORDER BY ts"
        cur = self.conn.execute(q, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def track_odometer(self, since_ts=None):
        """Per-local-day distance and driving time. The odometer, in short."""
        self._flush()
        q = ("SELECT date(ts,'unixepoch','localtime') AS day, "
             "SUM(dist_m) AS distance_m, "
             "SUM(CASE WHEN moving = 1 THEN dt END) AS moving_s, "
             "COUNT(DISTINCT trip) AS trips, MAX(speed) AS max_speed_mps "
             "FROM track WHERE trip IS NOT NULL")
        args = []
        if since_ts is not None:
            q += " AND ts >= ?"; args.append(since_ts)
        q += " GROUP BY day ORDER BY day"
        cur = self.conn.execute(q, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ---- pre-archive history ---------------------------------------------
    def history_for(self, radio, identity):
        """What we knew about this identity before its raw rows were rotated."""
        cur = self.conn.execute(
            "SELECT * FROM history WHERE radio=? AND identity=?",
            (radio, identity))
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip([d[0] for d in cur.description], row))

    def known_identities(self, radio=None):
        """Set of identities seen in any archived era -- cheap recognition."""
        q = "SELECT radio,identity FROM history"
        args = []
        if radio:
            q += " WHERE radio=?"; args.append(radio)
        return {tuple(r) for r in self.conn.execute(q, args)}

    # ---- labels (user curation) ------------------------------------------
    def get_labels(self):
        """Map {(radio, identity): {name, category, notes, updated}}."""
        cur = self.conn.execute(
            "SELECT radio,identity,name,category,notes,updated FROM labels")
        out = {}
        for radio, identity, name, category, notes, updated in cur.fetchall():
            out[(radio, identity)] = {
                "name": name, "category": category, "notes": notes,
                "updated": updated}
        return out

    def list_labels(self):
        cur = self.conn.execute(
            "SELECT radio,identity,name,category,notes,updated "
            "FROM labels ORDER BY updated DESC")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def set_label(self, *, radio, identity, name=None, category=None,
                  notes=None, ts=None):
        self._retry(lambda: self.conn.execute(
            """INSERT INTO labels (radio,identity,name,category,notes,updated)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(radio,identity) DO UPDATE SET
                 name=excluded.name, category=excluded.category,
                 notes=excluded.notes, updated=excluded.updated""",
            (radio, identity, name, category, notes,
             ts if ts is not None else time.time())))

    def delete_label(self, radio, identity):
        self._retry(lambda: self.conn.execute(
            "DELETE FROM labels WHERE radio=? AND identity=?",
            (radio, identity)))

    def commit(self):
        """Flush buffered rows. Kept as `commit` so callers are unchanged."""
        self._flush()

    def close(self):
        try:
            self._flush()
        finally:
            self.conn.close()
