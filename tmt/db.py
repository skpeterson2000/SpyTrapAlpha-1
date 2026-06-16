"""Append-only SQLite store for radio sightings.

Every observation is a row; we never mutate history. Recurrence and threat
scoring are computed as queries over this log, so the raw record stays the
source of truth and detection logic can evolve without re-collecting data.
"""

import json
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
    gps_mode      INTEGER                  -- 2=2D, 3=3D fix; NULL/0/1 = none
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
"""


class Store:
    def __init__(self, path=DEFAULT_PATH, check_same_thread=True):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, timeout=10.0,
                                    check_same_thread=check_same_thread)
        # WAL lets the always-on logger and an ad-hoc scan/report coexist
        # without blocking each other on the single sightings.db.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

        # GPS auto-stamping config (latest fix is shared via a tmpfs file).
        from . import config as _config
        g = _config.load().get("gps", {})
        self._gps_enabled = g.get("enabled", False)
        self._gps_fix_file = g.get("fix_file")
        self._gps_max_age = g.get("max_age_seconds", 30)
        self._fix_cache = None
        self._fix_cache_ts = 0.0

    def _migrate(self):
        """Add columns introduced after a DB was first created."""
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(sightings)")}
        for col, decl in (("lat", "REAL"), ("lon", "REAL"),
                          ("gps_mode", "INTEGER")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE sightings ADD COLUMN {col} {decl}")

    def _current_fix(self):
        """Latest fresh GPS fix (cached ~3s) or None."""
        if not self._gps_enabled or not self._gps_fix_file:
            return None
        now = time.time()
        if now - self._fix_cache_ts > 3.0:
            from .gps import read_fix
            self._fix_cache = read_fix(self._gps_fix_file, self._gps_max_age)
            self._fix_cache_ts = now
        return self._fix_cache

    def add_sighting(self, *, radio, address=None, address_type=None, name=None,
                     rssi=None, tx_power=None, service_uuids=None,
                     manufacturer_data=None, service_data=None,
                     tracker_type=None, session=None, ts=None):
        mfg = manufacturer_data or {}
        sd = service_data or {}
        fix = self._current_fix()
        lat = fix["lat"] if fix else None
        lon = fix["lon"] if fix else None
        gps_mode = fix["mode"] if fix else None
        self.conn.execute(
            """INSERT INTO sightings
               (ts, radio, address, address_type, name, rssi, tx_power,
                service_uuids, mfg_company, mfg_data, service_data,
                tracker_type, session, lat, lon, gps_mode)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ts if ts is not None else time.time(),
                radio, address, address_type, name, rssi, tx_power,
                json.dumps(sorted(service_uuids or [])),
                next(iter(mfg), None),
                json.dumps({str(k): v.hex() for k, v in mfg.items()}),
                json.dumps({str(k): v.hex() for k, v in sd.items()}),
                tracker_type, session, lat, lon, gps_mode,
            ),
        )

    def rows_since(self, since_ts):
        """Sighting dicts at/after since_ts — the engine's scoring window."""
        cur = self.conn.execute(
            "SELECT radio,address,rssi,ts,tracker_type,session "
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
        cur = self.conn.execute(
            """INSERT INTO alerts
               (ts,tier,radio,identity,tracker,score,n_sessions,reason,channels)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (ts, tier, radio, identity, tracker, score, n_sessions, reason,
             _json.dumps(channels)))
        self.conn.commit()
        return cur.lastrowid

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
        self.conn.execute(
            """INSERT INTO decodes (ts,model,dev_id,identity,freq_mhz,rssi,json)
               VALUES (?,?,?,?,?,?,?)""",
            (ts, model, dev_id, identity, freq_mhz, rssi, _json.dumps(frame)))
        # Mirror into sightings so a recurring decoded device id scores like
        # any other identity (recurrence / labels / alerts all apply).
        self.add_sighting(radio="decode", address=identity, name=model,
                          rssi=int(rssi) if rssi is not None else None,
                          session=session, ts=ts)
        self.conn.commit()

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
        self.conn.execute(
            """INSERT INTO labels (radio,identity,name,category,notes,updated)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(radio,identity) DO UPDATE SET
                 name=excluded.name, category=excluded.category,
                 notes=excluded.notes, updated=excluded.updated""",
            (radio, identity, name, category, notes,
             ts if ts is not None else time.time()))
        self.conn.commit()

    def delete_label(self, radio, identity):
        self.conn.execute("DELETE FROM labels WHERE radio=? AND identity=?",
                          (radio, identity))
        self.conn.commit()

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()
