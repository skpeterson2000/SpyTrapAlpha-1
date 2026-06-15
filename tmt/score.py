"""Recurrence / threat scoring over the sightings log.

The question the whole project exists to answer is "is something following me?"
A thing follows you if it keeps reappearing — across sessions (our proxy for
distinct times/places) and over a sustained span — rather than being a one-off
in a single place. So the score rewards, in order of importance:

  1. presence across MANY sessions   (the core "it travels with me" signal)
  2. a sustained time span
  3. raw persistence (lots of sightings)
  4. being a known tracker class      (Tile/SmartTag/Find My/FMDN)
  5. a Find My tag in *separated* mode (a lone tag away from its owner)

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


def _mode(values):
    counts = defaultdict(int)
    for v in values:
        if v is not None:
            counts[v] += 1
    return max(counts, key=counts.get) if counts else None


def score_identities(rows):
    """rows: dicts with radio, address, rssi, ts, tracker_type, session.

    Returns a list of per-identity dicts sorted by descending score.
    """
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

        score = 0.0
        reasons = []
        if n_sessions >= 2:
            score += 3.0 * n_sessions
            reasons.append(f"seen in {n_sessions} sessions")
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

        results.append({
            "radio": radio, "address": address, "tracker": tracker,
            "n": n, "n_sessions": n_sessions, "sessions": sorted(sessions),
            "span_h": span_h, "first": t0, "last": t1,
            "rssi_max": max(rssis) if rssis else None,
            "score": round(score, 1),
            "tier": _tier(score, n_sessions, tracker),
            "reasons": reasons,
        })

    results.sort(key=lambda d: d["score"], reverse=True)
    return results


def _tier(score, n_sessions, tracker):
    is_tracker = tracker in TRACKER_LABELS
    if is_tracker and n_sessions >= 3:
        return "HIGH"
    if score >= 10 or (is_tracker and n_sessions >= 2):
        return "MED"
    if score >= 5:
        return "LOW"
    return "info"


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
