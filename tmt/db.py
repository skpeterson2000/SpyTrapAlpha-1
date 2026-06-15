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
    session       TEXT                     -- run/location tag for "did it follow me"
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
        self.conn.commit()

    def add_sighting(self, *, radio, address=None, address_type=None, name=None,
                     rssi=None, tx_power=None, service_uuids=None,
                     manufacturer_data=None, service_data=None,
                     tracker_type=None, session=None, ts=None):
        mfg = manufacturer_data or {}
        sd = service_data or {}
        self.conn.execute(
            """INSERT INTO sightings
               (ts, radio, address, address_type, name, rssi, tx_power,
                service_uuids, mfg_company, mfg_data, service_data,
                tracker_type, session)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ts if ts is not None else time.time(),
                radio, address, address_type, name, rssi, tx_power,
                json.dumps(sorted(service_uuids or [])),
                next(iter(mfg), None),
                json.dumps({str(k): v.hex() for k, v in mfg.items()}),
                json.dumps({str(k): v.hex() for k, v in sd.items()}),
                tracker_type, session,
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

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()
