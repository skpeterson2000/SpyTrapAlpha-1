"""Loiter/orbit detection over ADS-B tracks.

A transiting aircraft enters and leaves — large net displacement, roughly
straight. A loitering one stays put: it remains within a small radius for a
sustained time and its net displacement is small relative to the area it covers
(it's circling/holding). That orbit pattern is the surveillance / firefighting /
spotter signature. We also compute the orbit's centre and its distance from the
operator (our GPS), so "circling me" can be told from "circling something near
me".
"""

import math


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def analyze(tracks, our_fix=None, loiter_minutes=5, loiter_radius_km=18):
    """tracks: {hex: [ {ts,flight,lat,lon,alt,gs,track,rssi}, ... ]} -> list."""
    out = []
    for hexid, pts in tracks.items():
        pos = [p for p in pts if p.get("lat") is not None and p.get("lon") is not None]
        dur_min = (pts[-1]["ts"] - pts[0]["ts"]) / 60.0
        flight = next((p["flight"] for p in reversed(pts) if p.get("flight")), None)
        rssis = [p["rssi"] for p in pts if p.get("rssi") is not None]
        alt = next((p["alt"] for p in reversed(pts) if p.get("alt") is not None), None)
        gs = next((p["gs"] for p in reversed(pts) if p.get("gs") is not None), None)

        rec = {
            "hex": hexid, "flight": (flight or "").strip() or None,
            "duration_min": round(dur_min, 1), "n": len(pts),
            "alt": alt, "gs": gs,
            "rssi": round(max(rssis), 1) if rssis else None,
            "last": pts[-1]["ts"],
            "loiter": False, "radius_km": None, "center": None,
            "dist_to_us_km": None, "focus": None,
        }

        if len(pos) >= 4 and dur_min >= 1:
            clat = sum(p["lat"] for p in pos) / len(pos)
            clon = sum(p["lon"] for p in pos) / len(pos)
            radius = max(haversine_km(clat, clon, p["lat"], p["lon"]) for p in pos)
            net = haversine_km(pos[0]["lat"], pos[0]["lon"],
                               pos[-1]["lat"], pos[-1]["lon"])
            rec["radius_km"] = round(radius, 1)
            rec["center"] = {"lat": round(clat, 5), "lon": round(clon, 5)}
            if our_fix and our_fix.get("lat") is not None:
                d = haversine_km(clat, clon, our_fix["lat"], our_fix["lon"])
                rec["dist_to_us_km"] = round(d, 1)
            # On station long enough, contained in a small radius, and doubling
            # back (net displacement small vs the area covered) => orbiting.
            if (dur_min >= loiter_minutes and radius <= loiter_radius_km
                    and net <= radius * 1.5):
                rec["loiter"] = True
                # Is the orbit centred on us, or on something nearby?
                d = rec["dist_to_us_km"]
                if d is not None:
                    rec["focus"] = "you" if d <= max(loiter_radius_km, 5) else "elsewhere"

        out.append(rec)

    # Loiterers first, then strongest/most-recent.
    out.sort(key=lambda r: (not r["loiter"], -(r["last"])))
    return out
