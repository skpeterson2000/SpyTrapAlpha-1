"""Recurrence / threat scoring over the sightings log.

The question the whole project exists to answer is "is something following me?"

FOLLOWING IS A CLAIM ABOUT PLACE, NOT TIME. This scorer used to treat sessions
(15-minute time buckets) as "our proxy for distinct times/places". Measured
against this rig's own log, that proxy was wrong 88.1% of the time: of 1,181
identities that scored on "seen in >= 2 sessions" and could be located, 1,040
were seen at exactly ONE place. They are stationary neighbours near a parked
Pi, not followers — and they were the bulk of a 410 alert/hour flood.

Worse, the observer is usually stationary too: on 33 of 39 days the rig never
moved, so the question was unanswerable from position and the scorer answered
it anyway with confident MED/HIGH tiers.

So position is now real evidence, and its ABSENCE is stated rather than hidden.
Every identity gets an `evidence` basis:

  position_corroborated — seen at >= 2 distinct places while the observer was
                          itself moving. This is the only thing that actually
                          means "it travelled with me", and the only basis that
                          can reach HIGH on its own.
  local_fixture         — the observer moved, this identity has position data,
                          and it never left one place. That is evidence AGAINST
                          following, so the recurrence bonus is withheld.
  recurrence_only       — no usable position (for it or for us), or the observer
                          never moved. Recurrence is all we have, so it is
                          scored weakly and CAPPED below HIGH. A verdict reached
                          without position must not look like one reached with it.

Ranked contributions:
  1. presence across MANY PLACES     (the core "it travels with me" signal)
  2. presence across many sessions   (weak, and only when not a local fixture)
  3. a sustained time span
  4. raw persistence (lots of sightings)
  5. being a known tracker class      (Tile/SmartTag/Find My/FMDN)
  6. a Find My tag in *separated* mode (a lone tag away from its owner)

Identity = (radio, address): a BLE MAC or an SDR frequency bucket. CAVEAT:
Apple Find My tags rotate their MAC ~every 15 min, so a single physical AirTag
fragments into many short-lived addresses and will be UNDER-counted by this
address-based grouping. `tracker_class_activity()` is the partial mitigation —
it aggregates the dangerous labels across sessions regardless of MAC, at the
cost of not distinguishing one persistent tag from several transient ones.
"""

import math
from collections import defaultdict

from .signatures import TRACKER_LABELS


# ~110 m at 3 decimal places. Coarse on purpose: GPS wander must not manufacture
# "distinct locations" out of a rig sitting still in one parking spot.
PLACE_PRECISION = 3

# Counting distinct grid CELLS is not enough on its own: a stationary rig whose
# fix jitters across a cell boundary produced two "places" 76 m apart and was
# credited with having moved. Movement must therefore be judged by actual
# DISPLACEMENT, not by cell identity. 250 m is comfortably beyond consumer GPS
# wander while still far below any real trip.
MIN_MOVE_M = 250.0


def _haversine_m(p, q):
    lat1, lon1 = p
    lat2, lon2 = q
    r1, r2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(r1) * math.cos(r2) * math.sin(dlam / 2) ** 2)
    return 2 * 6371000.0 * math.asin(math.sqrt(min(1.0, a)))


def _spread_m(cells):
    """Widest separation within a set of cells (0 for <2). Cheap: cells, not rows."""
    pts = list(cells)
    if len(pts) < 2:
        return 0.0
    return max(_haversine_m(pts[i], pts[j])
               for i in range(len(pts)) for j in range(i + 1, len(pts)))


def _cell(lat, lon, precision=PLACE_PRECISION):
    """Round a fix to a grid cell, or None if there is no usable position."""
    if lat is None or lon is None:
        return None
    if lat == 0.0 and lon == 0.0:      # legacy null-island rows
        return None
    return (round(lat, precision), round(lon, precision))


def observer_places(rows, precision=PLACE_PRECISION):
    """Distinct cells the OBSERVER occupied in this window."""
    return {c for c in (_cell(r.get("lat"), r.get("lon"), precision)
                        for r in rows) if c is not None}


def observer_movement(rows, precision=PLACE_PRECISION):
    """(cells, spread_m, moved) for the observer.

    `moved` is False unless we travelled MIN_MOVE_M, because nothing in a
    window can be shown to have followed us if we never went anywhere — that is
    a fact about our own coverage, not about any device.
    """
    cells = observer_places(rows, precision)
    spread = _spread_m(cells)
    return cells, spread, spread >= MIN_MOVE_M


def _mode(values):
    counts = defaultdict(int)
    for v in values:
        if v is not None:
            counts[v] += 1
    return max(counts, key=counts.get) if counts else None


TIER_RANK = {"info": 0, "LOW": 1, "MED": 2, "HIGH": 3}

# Categories that silence an identity vs. escalate it.
SUPPRESS_CATEGORIES = {"mine", "safe", "ignore"}


def score_identities(rows, labels=None, precision=PLACE_PRECISION):
    """rows: dicts with radio, address, rssi, ts, tracker_type, session.

    labels: optional {(radio, identity): {name, category, notes}} from user
    curation. Categories mine/safe/ignore suppress an identity (drops to info
    and sinks in the ranking); threat forces HIGH; watch pins it visible.

    Returns a list of per-identity dicts sorted with live threats first,
    suppressed/known identities last.
    """
    labels = labels or {}
    obs_places, obs_spread, observer_moved = observer_movement(rows, precision)
    groups = defaultdict(list)
    for r in rows:
        groups[(r["radio"], r["address"])].append(r)

    results = []
    for (radio, address), g in groups.items():
        sessions = {r["session"] for r in g if r["session"]}
        n = len(g)
        n_sessions = len(sessions)
        ts = [r["ts"] for r in g]
        t0, t1 = min(ts), max(ts)
        span_h = (t1 - t0) / 3600.0
        rssis = [r["rssi"] for r in g if r["rssi"] is not None]
        tracker = _mode(r["tracker_type"] for r in g)
        # The device-offered name — the automatic, self-reported identity,
        # distinct from the MAC and from any user label. Only meaningful where
        # the name truly comes from the device: BLE local name / decoded model.
        # (Raw SDR sweep stores an internal "ism-<band>" tag, not a device name.)
        broadcast_name = None
        if radio in ("ble", "decode"):
            broadcast_name = _mode(r.get("name") for r in g if r.get("name"))

        places = {c for c in (_cell(r.get("lat"), r.get("lon"), precision)
                              for r in g) if c is not None}
        n_places = len(places)
        n_located = sum(1 for r in g if _cell(r.get("lat"), r.get("lon"),
                                              precision) is not None)

        # What kind of claim can this evidence actually support? The identity
        # must itself have MOVED, not merely straddled a grid boundary.
        spread_m = _spread_m(places)
        if observer_moved and n_places >= 2 and spread_m >= MIN_MOVE_M:
            evidence = "position_corroborated"
        elif observer_moved and n_located >= 2 and spread_m < MIN_MOVE_M:
            evidence = "local_fixture"
        else:
            evidence = "recurrence_only"

        score = 0.0
        reasons = []
        if evidence == "position_corroborated":
            # The real signal: it turned up in places we travelled between.
            score += 6.0 * n_places
            reasons.append(f"seen at {n_places} locations "
                           f"{spread_m/1000:.1f} km apart")
        if n_sessions >= 2 and evidence != "local_fixture":
            # Time recurrence is weak evidence on its own; it is withheld
            # entirely from a device we can SEE is bolted to one spot.
            weight = 3.0 if evidence == "position_corroborated" else 1.0
            score += weight * n_sessions
            reasons.append(f"seen in {n_sessions} sessions")
        elif evidence == "local_fixture":
            reasons.append(f"stationary: never moved more than "
                           f"{spread_m:.0f} m across {n_sessions} sessions")
        if span_h >= 0.5:
            score += min(span_h, 24.0) * 0.5
            reasons.append(f"persisted {span_h:.1f}h")
        score += min(n, 50) * 0.1
        if tracker in TRACKER_LABELS:
            score += 5.0
            reasons.append(f"known tracker class: {tracker}")
        if tracker == "apple_findmy_separated":
            score += 3.0
            reasons.append("Find My tag in SEPARATED mode (away from owner)")

        tier = _tier(score, n_sessions, tracker, evidence)

        # Apply user curation last so it overrides the automatic verdict.
        lab = labels.get((radio, address))
        category = lab.get("category") if lab else None
        suppressed = False
        if category in SUPPRESS_CATEGORIES:
            suppressed = True
            tier = "info"
            reasons.insert(0, f"labeled {category}"
                              + (f" ({lab['name']})" if lab.get("name") else ""))
        elif category == "threat":
            score += 100.0           # dominate the ranking
            tier = "HIGH"            # explicit user override outranks evidence
            reasons.insert(0, "labeled THREAT"
                              + (f" ({lab['name']})" if lab.get("name") else ""))
        elif category == "watch":
            reasons.insert(0, "on watchlist"
                              + (f" ({lab['name']})" if lab.get("name") else ""))
            if TIER_RANK[tier] < TIER_RANK["LOW"]:
                tier = "LOW"

        results.append({
            "radio": radio, "address": address, "tracker": tracker,
            "n": n, "n_sessions": n_sessions, "sessions": sorted(sessions),
            "n_places": n_places, "n_located": n_located,
            "spread_m": round(spread_m),
            "evidence": evidence,
            "observer_moved": observer_moved,
            "span_h": span_h, "first": t0, "last": t1,
            "rssi_max": max(rssis) if rssis else None,
            "score": round(score, 1),
            "tier": tier,
            "reasons": reasons,
            "broadcast_name": broadcast_name,
            "label": lab.get("name") if lab else None,
            "category": category,
            "notes": lab.get("notes") if lab else None,
            "suppressed": suppressed,
        })

    # Live threats first; suppressed/known identities sink to the bottom.
    results.sort(key=lambda d: (d["suppressed"], -d["score"]))
    return results


def _tier(score, n_sessions, tracker, evidence="recurrence_only"):
    """Tier, capped by what the evidence can actually support.

    HIGH is a claim that something is following you, so it requires position
    corroboration. Without it the honest ceiling is MED — "recurring, unable to
    confirm it travelled with you". A device we can see never leaves one spot
    is capped lower still.
    """
    is_tracker = tracker in TRACKER_LABELS
    if evidence == "position_corroborated":
        if is_tracker and n_sessions >= 2:
            return "HIGH"
        if score >= 18:
            return "HIGH"
        if score >= 10 or is_tracker:
            return "MED"
        return "LOW" if score >= 5 else "info"
    if evidence == "local_fixture":
        # Demonstrably parked next to us. Keep it visible, never alarming.
        return "LOW" if is_tracker else "info"
    # recurrence_only: capped below HIGH no matter how persistent.
    if score >= 10 or (is_tracker and n_sessions >= 2):
        return "MED"
    if score >= 5 or is_tracker:
        return "LOW"
    return "info"


def novelty_view(scored, now, new_hours=48.0, min_sessions=2):
    """The 'new AND sticking' intersection over already-scored identities.

    new+transient is noise; new+persistent is the thing to worry about. Keep
    identities whose FIRST sighting is within new_hours and that have since
    appeared in >= min_sessions distinct sessions. Excludes suppressed
    (labeled mine/safe/ignore). Rank by persistence, then recency.

    Call with `scored` from score_identities() over a window LONGER than
    new_hours, so first-seen is real and not just the edge of the window.
    """
    out = []
    for s in scored:
        if s.get("suppressed"):
            continue
        age_h = (now - s["first"]) / 3600.0
        if age_h > new_hours or s["n_sessions"] < min_sessions:
            continue
        out.append({
            **s,
            "age_h": age_h,
            "first_seen_h_ago": round(age_h, 1),
            # 0..1 recency: 1 = just appeared, 0 = at the new_hours edge.
            "novelty": round(max(0.0, 1.0 - age_h / new_hours), 2),
        })
    out.sort(key=lambda d: (-d["n_sessions"], d["age_h"]))
    return out


def tracker_class_activity(rows):
    """MAC-agnostic view: how widely each dangerous tracker class appears.

    Mitigates MAC rotation — counts distinct sessions per tracker label across
    all addresses. High session-spread for 'apple_findmy_separated' is a strong
    "a lone tag is traveling with me" indicator even though we can't pin it to
    one MAC.
    """
    by_label = defaultdict(lambda: {"sessions": set(), "addrs": set(), "n": 0})
    for r in rows:
        lbl = r["tracker_type"]
        if lbl in TRACKER_LABELS:
            d = by_label[lbl]
            d["n"] += 1
            if r["session"]:
                d["sessions"].add(r["session"])
            if r["address"]:
                d["addrs"].add(r["address"])
    out = []
    for lbl, d in by_label.items():
        out.append({
            "tracker": lbl, "n": d["n"],
            "n_sessions": len(d["sessions"]),
            "n_addrs": len(d["addrs"]),
        })
    out.sort(key=lambda d: (d["n_sessions"], d["n"]), reverse=True)
    return out
