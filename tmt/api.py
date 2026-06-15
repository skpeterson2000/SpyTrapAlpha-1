"""Headless JSON/SSE API over the sightings + alerts store.

This is the seam for ANY future UI (web dashboard or native): everything the
engine knows is exposed here as JSON, plus a live Server-Sent-Events stream of
new alerts. No HTML/JS is committed to a particular front-end yet.

Endpoints:
  GET /api/health                      liveness + store path
  GET /api/stats                       sighting/alert/session counts
  GET /api/suspects?hours=&min_tier=   ranked recurrence scoring (live)
  GET /api/alerts?hours=&limit=        recent persisted alerts
  GET /api/stream                      text/event-stream of new alerts (SSE)
"""

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse

from . import config as configmod
from .db import Store
from .score import score_identities, tracker_class_activity
from .alerts import TIER_RANK

CFG = configmod.load()
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _store():
    # Per-request connection; check_same_thread off for uvicorn's worker threads.
    return Store(check_same_thread=False)


app = FastAPI(title="Track_My_Tracker API", version="1.0")


@app.get("/api/health")
def health():
    s = _store()
    try:
        n = s.conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
        return {"ok": True, "db": s.path, "sightings": n}
    finally:
        s.close()


@app.get("/api/stats")
def stats():
    s = _store()
    try:
        c = s.conn
        out = {
            "sightings": c.execute("SELECT COUNT(*) FROM sightings").fetchone()[0],
            "alerts": c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0],
            "sessions": c.execute(
                "SELECT COUNT(DISTINCT session) FROM sightings").fetchone()[0],
            "by_radio": {},
        }
        for radio, n, last in c.execute(
                "SELECT radio, COUNT(*), MAX(ts) FROM sightings GROUP BY radio"):
            out["by_radio"][radio] = {"sightings": n, "last_ts": last}
        return out
    finally:
        s.close()


@app.get("/api/suspects")
def suspects(hours: float = Query(24, ge=0), min_tier: str = "info",
             top: int = 50):
    s = _store()
    try:
        import time
        since = time.time() - hours * 3600 if hours else 0
        rows = s.rows_since(since)
        ranked = score_identities(rows)
        floor = TIER_RANK.get(min_tier, 0)
        ranked = [r for r in ranked if TIER_RANK.get(r["tier"], 0) >= floor]
        return {
            "window_hours": hours,
            "suspects": ranked[:top],
            "tracker_activity": tracker_class_activity(rows),
        }
    finally:
        s.close()


@app.get("/api/alerts")
def alerts(hours: float = Query(0, ge=0), limit: int = 100):
    s = _store()
    try:
        import time
        since = time.time() - hours * 3600 if hours else None
        return {"alerts": s.recent_alerts(since_ts=since, limit=limit)}
    finally:
        s.close()


@app.get("/api/stream")
async def stream():
    """SSE: emit each new alert row as it appears. UIs subscribe here."""
    async def gen():
        s = _store()
        try:
            last_id = s.conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM alerts").fetchone()[0]
            yield "event: ready\ndata: {}\n\n"
            while True:
                rows = await asyncio.to_thread(
                    s.recent_alerts, None, last_id, 50)
                for a in reversed(rows):          # oldest-first
                    last_id = max(last_id, a["id"])
                    yield f"event: alert\ndata: {json.dumps(a)}\n\n"
                yield ": keepalive\n\n"            # comment ping keeps it open
                await asyncio.sleep(2)
        finally:
            s.close()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.get("/")
def index():
    """Serve the web dashboard; fall back to the API listing if it's absent."""
    page = WEB_DIR / "index.html"
    if page.exists():
        return FileResponse(str(page))
    return JSONResponse({
        "service": "Track_My_Tracker API",
        "endpoints": ["/api/health", "/api/stats", "/api/suspects",
                      "/api/alerts", "/api/stream"],
    })


@app.get("/api")
def api_index():
    return {
        "service": "Track_My_Tracker API",
        "endpoints": ["/api/health", "/api/stats", "/api/suspects",
                      "/api/alerts", "/api/stream"],
        "note": "headless API; the web dashboard is served at /.",
    }
