"""Headless JSON/SSE API over the sightings + alerts store.

This is the seam for ANY future UI (web dashboard or native): everything the
engine knows is exposed here as JSON, plus a live Server-Sent-Events stream of
new alerts. No HTML/JS is committed to a particular front-end yet.

Endpoints:
  GET /api/health                      liveness + store path
  GET /api/stats                       sighting/alert/session counts
  GET /api/suspects?hours=&min_tier=   ranked recurrence scoring (live)
  GET /api/alerts?hours=&limit=        recent persisted alerts
  GET /api/radio                       SDR release state (dongles free?)
  POST /api/radio/release|resume       free the dongles for OP25 / take back
  GET /api/stream                      text/event-stream of new alerts (SSE)
"""

import asyncio
import functools
import json
import threading
import time as _time
from pathlib import Path

from fastapi import FastAPI, Query, Body, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config as configmod
from .db import Store
from .score import score_identities, tracker_class_activity, novelty_view
from .alerts import TIER_RANK
from . import radio_control

CFG = configmod.load()
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _ttl_cache(seconds):
    """Serve a view from cache for up to `seconds`, computing it at most once.

    The dashboard polls every 5s, but several views cost SECONDS over a
    multi-million-row table: /api/novelty scores a 7-day window, /api/stats
    COUNT(*)s the whole table. Recomputing per poll demanded more work than the
    box could do, so requests stacked on uvicorn's threads, pinned >1 core,
    drove the Pi 5 SoC into thermal throttling, and (via the thermal governor,
    which reads that CPU temperature) parked the SDRs. A counter-surveillance
    rig stopped collecting because its own dashboard was too expensive to draw.

    These views describe hours-long trends, so serving one a few seconds stale
    costs the user nothing. The single-flight lock is the important half: it is
    what stops a view slower than the poll interval from stacking one
    computation per poll until the CPU is saturated.
    """
    def deco(fn):
        entries, locks = {}, {}
        guard = threading.Lock()

        @functools.wraps(fn)
        def wrapper(*a, **kw):
            key = (a, tuple(sorted(kw.items())))
            with guard:
                hit = entries.get(key)
                if hit and _time.monotonic() < hit[0]:
                    return hit[1]
                lock = locks.setdefault(key, threading.Lock())
            with lock:                       # single-flight
                hit = entries.get(key)       # a racer may have just filled it
                if hit and _time.monotonic() < hit[0]:
                    return hit[1]
                val = fn(*a, **kw)
                with guard:
                    entries[key] = (_time.monotonic() + seconds, val)
                    if len(entries) > 64:    # bound: few distinct arg sets
                        oldest = sorted(entries, key=lambda k: entries[k][0])
                        for k in oldest[:32]:
                            entries.pop(k, None); locks.pop(k, None)
                return val
        return wrapper
    return deco


def _store():
    # Per-request connection; check_same_thread off for uvicorn's worker threads.
    return Store(check_same_thread=False)


app = FastAPI(title="SpyTrap API", version="1.0")

# Serve the dashboard's static assets (vendored Leaflet, etc.) from web/.
# Vendored locally so the map library loads even when the rig is offline in
# the field; only the map *tiles* need a network (markers/trails draw without).
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/api/health")
def health():
    s = _store()
    try:
        n = s.conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
        return {"ok": True, "db": s.path, "sightings": n}
    finally:
        s.close()


@app.get("/api/stats")
@_ttl_cache(30)
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
@_ttl_cache(10)
def suspects(hours: float = Query(24, ge=0), min_tier: str = "info",
             top: int = 50):
    s = _store()
    try:
        import time
        since = time.time() - hours * 3600 if hours else 0
        rows = s.rows_since(since)
        ranked = score_identities(rows, s.get_labels())
        floor = TIER_RANK.get(min_tier, 0)
        # Coverage: how much of this verdict rests on actual position data.
        # Without it the ranking is "recurrence only", and the UI must say so
        # rather than presenting the same tiers with the same confidence.
        from .score import observer_movement as _obs_move, MIN_MOVE_M
        places, spread_m, moved = _obs_move(rows)
        located = sum(1 for r in rows if r.get("lat") is not None
                      and not (r.get("lat") == 0.0 and r.get("lon") == 0.0))
        states = {}
        for r in rows:
            k = r.get("fix_state") or "unknown"
            states[k] = states.get(k, 0) + 1
        coverage = {
            "rows": len(rows),
            "located": located,
            "coverage_pct": round(100.0 * located / len(rows), 1) if rows else 0.0,
            "observer_places": len(places),
            # Displacement, not cell count: a stationary rig whose fix jitters
            # across a grid boundary is not "moving".
            "observer_spread_m": round(spread_m),
            "observer_moved": moved,
            "min_move_m": MIN_MOVE_M,
            "fix_states": states,
        }
        # Always keep suppressed rows visible so the user can un-label them,
        # regardless of the tier filter.
        ranked = [r for r in ranked
                  if r.get("suppressed") or TIER_RANK.get(r["tier"], 0) >= floor]
        return {
            "window_hours": hours,
            "suspects": ranked[:top],
            "tracker_activity": tracker_class_activity(rows),
            "coverage": coverage,
        }
    finally:
        s.close()


CATEGORIES = {"mine", "safe", "watch", "threat", "ignore"}


class LabelIn(BaseModel):
    radio: str
    identity: str
    name: str | None = None
    category: str | None = None
    notes: str | None = None


@app.get("/api/labels")
def list_labels():
    s = _store()
    try:
        return {"labels": s.list_labels(), "categories": sorted(CATEGORIES)}
    finally:
        s.close()


@app.post("/api/labels")
def upsert_label(label: LabelIn = Body(...)):
    if label.category and label.category not in CATEGORIES:
        raise HTTPException(422, f"category must be one of {sorted(CATEGORIES)}")
    s = _store()
    try:
        s.set_label(radio=label.radio, identity=label.identity,
                    name=label.name, category=label.category, notes=label.notes)
        return {"ok": True}
    finally:
        s.close()


@app.delete("/api/labels")
def delete_label(radio: str, identity: str):
    s = _store()
    try:
        s.delete_label(radio, identity)
        return {"ok": True}
    finally:
        s.close()


@app.get("/api/novelty")
@_ttl_cache(60)
def novelty(new_hours: float = Query(48, ge=1), min_sessions: int = Query(2, ge=1),
            lookback_hours: float = Query(168, ge=1), top: int = 50):
    """'New & sticking' — recently first-seen AND persistent across sessions."""
    s = _store()
    try:
        import time
        now = time.time()
        rows = s.rows_since(now - lookback_hours * 3600)
        scored = score_identities(rows, s.get_labels())
        items = novelty_view(scored, now, new_hours=new_hours,
                             min_sessions=min_sessions)
        return {"new_hours": new_hours, "min_sessions": min_sessions,
                "items": items[:top]}
    finally:
        s.close()


_dongles_cache = {"ts": 0.0, "val": None}


def _list_dongles_cached(ttl=4.0):
    """Enumerate dongles, cached briefly. Enumeration reads USB descriptors
    without opening the device (safe alongside OP25/sweep); the cache just keeps
    frequent dashboard polls from re-spawning the rtl_test fallback subprocess."""
    import time
    from .devices import list_devices
    now = time.time()
    if _dongles_cache["val"] is None or now - _dongles_cache["ts"] > ttl:
        try:
            _dongles_cache["val"] = list_devices()
        except Exception:
            _dongles_cache["val"] = []
        _dongles_cache["ts"] = now
    return _dongles_cache["val"]


@app.get("/api/dongles")
def dongles():
    """Which RTL-SDR dongles are on the bus right now, vs. expected.

    Lets the dashboard distinguish 'USB power was cut' (dongles vanish from the
    bus — e.g. a battery box idle auto-shutoff) from a heat problem; both
    otherwise just look like 'no SDR sightings'.
    """
    present = _list_dongles_cached()
    serials = [d.get("serial") for d in present]
    expected = CFG.get("sdr", {}).get("expected_serials") or []
    missing = [s for s in expected if s not in serials]
    return {
        "present": [{"serial": d.get("serial"), "product": d.get("product")}
                    for d in present],
        "count": len(present),
        "expected": expected,
        "expected_count": len(expected),
        "missing": missing,
        # ok = all expected accounted for; if none expected, ok = at least one present
        "ok": (not missing) if expected else (len(present) > 0),
    }


class ReleaseIn(BaseModel):
    reason: str | None = None


@app.get("/api/radio")
def radio():
    """Whether the SDR loops are parked with the dongles free (for OP25).

    See tmt/radio_control.py: this is a cooperative flag, not `systemctl stop`.
    The services stay active and simply stop opening a dongle, so nothing here
    needs elevated privileges.
    """
    return radio_control.status()


@app.post("/api/radio/release")
def radio_release(body: ReleaseIn = Body(default=ReleaseIn())):
    """Free the dongles so OP25 can claim one.

    POST (never GET) so the dashboard cannot release the radios by being
    prefetched or embedded — this has a real side effect on what the rig is
    collecting.
    """
    return radio_control.release(reason=body.reason, by="dashboard")


@app.post("/api/radio/resume")
def radio_resume():
    """Hand the dongles back to SpyTrap."""
    return radio_control.resume()


@app.get("/api/thermal")
def thermal():
    """Live thermal state so the dashboard can show the rig's heat struggle.

    The dongles have no temperature sensor; we report the Pi SoC temperature (a
    proxy for enclosure heat) and whether the governor would back off or pause
    SDR work. A hot reading is the cue for a human to add airflow/heatsinks.
    """
    from .thermal import Governor, read_soc_temp_c, read_throttle_flags, \
        THROTTLE_SOFT_TEMP, THROTTLE_UNDERVOLT
    gov = Governor.from_config(CFG)
    level, temp_c = gov.assess()
    flags = read_throttle_flags()
    if temp_c is None:
        temp_c = read_soc_temp_c()
    msg = {
        "OK": "nominal",
        "SOFT": "warm — sweeps slowing to shed heat; consider adding airflow",
        "HARD": "overheating — SDR paused until it cools; add a fan / heatsink "
                "or improve airflow across the dongles",
    }.get(level, "unknown")
    return {
        "enabled": bool(gov.enabled),
        "level": level,                       # OK | SOFT | HARD
        "temp_c": round(temp_c, 1) if temp_c is not None else None,
        "soft_c": gov.soft_c, "hard_c": gov.hard_c, "resume_c": gov.resume_c,
        "soc_throttling": bool(flags & THROTTLE_SOFT_TEMP) if flags else False,
        "undervolt": bool(flags & THROTTLE_UNDERVOLT) if flags else False,
        "message": msg,
    }


@app.get("/api/gps")
def gps():
    """Current GPS fix + config, so a UI can show location state."""
    import json as _json
    import time as _t
    from pathlib import Path as _P
    from .gps import read_fix, fix_problem
    g = CFG.get("gps", {})
    enabled = bool(g.get("enabled")) and bool(g.get("fix_file"))
    fix = read_fix(g.get("fix_file"), g.get("max_age_seconds", 30)) \
        if enabled else None
    # When there is no usable fix, say WHY. "enabled and connected" while the
    # map sits in the Gulf of Guinea is the failure this reports on: a bogus
    # 0,0 from gpsd used to be published as a real position.
    reason = None
    if enabled and fix is None:
        try:
            raw = _json.loads(_P(g["fix_file"]).read_text())
        except Exception:
            reason = "no fix file yet (gps poller not running?)"
        else:
            reason = fix_problem(raw)
            if reason is None:
                age = _t.time() - raw.get("ts", 0)
                reason = f"fix is stale ({age:.0f}s old)"
    return {"enabled": bool(g.get("enabled")),
            "source": f"{g.get('host')}:{g.get('port')}",
            "have_fix": fix is not None, "fix": fix, "reason": reason}


@app.get("/api/track")
def track(identity: str, radio: str = None, hours: float = Query(168, ge=0)):
    """Location trail for one identity — the 'followed me from X to Y' path."""
    s = _store()
    try:
        import time
        since = time.time() - hours * 3600 if hours else 0
        q = ("SELECT ts,lat,lon,rssi FROM sightings WHERE address=? "
             "AND lat IS NOT NULL AND ts>=?")
        args = [identity, since]
        if radio:
            q += " AND radio=?"; args.append(radio)
        q += " ORDER BY ts"
        pts = [{"ts": ts, "lat": lat, "lon": lon, "rssi": rssi}
               for ts, lat, lon, rssi in s.conn.execute(q, args)]
        return {"identity": identity, "points": pts}
    finally:
        s.close()


@app.get("/api/aircraft")
@_ttl_cache(10)
def aircraft(minutes: float = Query(30, ge=1)):
    """ADS-B aircraft with loiter/orbit analysis (the surveillance signature)."""
    from .adsb_analysis import analyze
    from .gps import read_fix
    s = _store()
    try:
        import time
        tracks = s.adsb_tracks_since(time.time() - minutes * 60)
        g = CFG.get("gps", {})
        fix = read_fix(g.get("fix_file"), g.get("max_age_seconds", 30)) \
            if g.get("enabled") and g.get("fix_file") else None
        a = CFG.get("adsb", {})
        items = analyze(tracks, our_fix=fix,
                        loiter_minutes=a.get("loiter_minutes", 5),
                        loiter_radius_km=a.get("loiter_radius_km", 18))
        return {"window_minutes": minutes, "enabled": bool(a.get("enabled")),
                "count": len(items),
                "loitering": sum(1 for x in items if x["loiter"]),
                "aircraft": items[:100]}
    finally:
        s.close()


@app.get("/api/decodes")
@_ttl_cache(15)
def decodes(hours: float = Query(6, ge=0), limit: int = 100):
    s = _store()
    try:
        import time
        since = time.time() - hours * 3600 if hours else None
        return {"decode_enabled": bool(CFG["decode"].get("enabled")),
                "decodes": s.recent_decodes(since_ts=since, limit=limit)}
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
        # no-cache so an updated dashboard shows on reload (revalidate every time)
        return FileResponse(str(page), headers={"Cache-Control": "no-cache"})
    return JSONResponse({
        "service": "SpyTrap API",
        "endpoints": ["/api/health", "/api/stats", "/api/suspects",
                      "/api/alerts", "/api/stream"],
    })


@app.get("/api")
def api_index():
    return {
        "service": "SpyTrap API",
        "endpoints": ["/api/health", "/api/stats", "/api/suspects",
                      "/api/alerts", "/api/stream"],
        "note": "headless API; the web dashboard is served at /.",
    }
