"""Alert engine: turn recurrence scores into deduped, multi-channel alerts.

Runs on a loop (see alertd.py). Each pass scores the recent window, takes
identities at/above the configured tiers, suppresses ones alerted within the
cooldown, then persists each new alert (the API/GUI reads that table) and fans
it out to the delivery channels.
"""

import time

from .channels import AudibleChannel, build_channels
from .score import score_identities

TIER_RANK = {"info": 0, "LOW": 1, "MED": 2, "HIGH": 3}


class AlertEngine:
    def __init__(self, store, config, channels=None, on_alert=None):
        self.store = store
        self.cfg = config
        chans = channels if channels is not None else build_channels(config)
        # Audio is handled specially: persist/log/push EVERY new alert, but play
        # at most one sound per pass (the most severe) so a burst of detections
        # doesn't turn into overlapping noise.
        self.audible = next((c for c in chans if isinstance(c, AudibleChannel)),
                            None)
        self.other_channels = [c for c in chans if c is not self.audible]
        self.on_alert = on_alert or (lambda *a, **k: None)
        self.min_rank = min(TIER_RANK[t] for t in config["alert_tiers"])
        self.cooldown = config["cooldown_minutes"] * 60.0
        self.window = config["window_hours"] * 3600.0

    def evaluate(self, now=None):
        """One pass. Returns the list of newly-fired alerts."""
        now = now or time.time()
        rows = self.store.rows_since(now - self.window)
        ranked = score_identities(rows)

        candidates = []
        for r in ranked:
            if TIER_RANK.get(r["tier"], 0) < self.min_rank:
                continue
            last = self.store.last_alert_ts(r["address"])
            if last is not None and (now - last) < self.cooldown:
                continue
            candidates.append(r)
        if not candidates:
            return []

        # The one alert that gets a sound this pass.
        loudest = max(candidates,
                      key=lambda r: (TIER_RANK[r["tier"]], r["score"]))
        return [self._raise(r, now, play_audio=(r is loudest))
                for r in candidates]

    def _raise(self, r, now, play_audio):
        alert = {
            "ts": now,
            "tier": r["tier"],
            "radio": r["radio"],
            "identity": r["address"],
            "tracker": r["tracker"],
            "score": r["score"],
            "n_sessions": r["n_sessions"],
            "reason": "; ".join(r["reasons"]) or "recurring signal",
        }
        notified = []
        for ch in self.other_channels:
            try:
                name = ch.send(alert)
            except Exception:
                name = None
            if name:
                notified.append(name)
        if play_audio and self.audible:
            try:
                name = self.audible.send(alert)
                if name:
                    notified.append(name)
            except Exception:
                pass
        alert["channels"] = notified
        alert["id"] = self.store.add_alert(
            ts=now, tier=alert["tier"], radio=alert["radio"],
            identity=alert["identity"], tracker=alert["tracker"],
            score=alert["score"], n_sessions=alert["n_sessions"],
            reason=alert["reason"], channels=notified)
        self.on_alert(alert)
        return alert
