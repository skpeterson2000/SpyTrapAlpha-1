"""Configuration for the alert engine + API.

Layering (last wins): built-in DEFAULTS -> config.local.json (gitignored) ->
environment variables (for secrets like push tokens). Keeping secrets in env
means the repo/config never carries a push token.
"""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    # Scoring window the live engine considers, and how often it re-evaluates.
    "window_hours": 24,
    "poll_seconds": 20,
    # Tiers that actually raise an alert. Production = MED+HIGH; drop to "LOW"
    # to exercise the pipeline before you have multi-location recurrence data.
    "alert_tiers": ["MED", "HIGH"],
    # Per-identity re-alert suppression so one persistent tag doesn't spam.
    "cooldown_minutes": 30,

    "audio": {
        "enabled": True,
        "player": None,              # auto-detected if null
        # Drop your files in sounds/ and map tier -> filename here.
        "sounds": {
            "HIGH": "alert-high.wav",
            "MED": "alert-med.wav",
            "_default": "alert.wav",
        },
    },

    "push": {
        "enabled": False,            # auto-enabled if a topic/token is present
        "provider": "ntfy",          # "ntfy" | "pushover"
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "",            # set via TMT_NTFY_TOPIC
        "pushover_token": "",        # set via TMT_PUSHOVER_TOKEN
        "pushover_user": "",         # set via TMT_PUSHOVER_USER
    },

    "log": {"enabled": True, "file": str(ROOT / "alerts.log")},

    "api": {"host": "0.0.0.0", "port": 8080},
}


def _deep_merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load():
    cfg = json.loads(json.dumps(DEFAULTS))   # deep copy
    local = ROOT / "config.local.json"
    if local.exists():
        try:
            _deep_merge(cfg, json.loads(local.read_text()))
        except Exception:
            pass

    # Secret/env overrides — presence also flips the channel on.
    topic = os.environ.get("TMT_NTFY_TOPIC")
    if topic:
        cfg["push"].update(enabled=True, provider="ntfy", ntfy_topic=topic)
    po_t = os.environ.get("TMT_PUSHOVER_TOKEN")
    po_u = os.environ.get("TMT_PUSHOVER_USER")
    if po_t and po_u:
        cfg["push"].update(enabled=True, provider="pushover",
                           pushover_token=po_t, pushover_user=po_u)
    if cfg["push"].get("ntfy_topic") or (cfg["push"].get("pushover_token")
                                         and cfg["push"].get("pushover_user")):
        cfg["push"]["enabled"] = cfg["push"].get("enabled", True)

    return cfg
