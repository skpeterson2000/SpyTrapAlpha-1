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

    # Decode of UNENCRYPTED ISM device frames (rtl_433). Ships INERT: BOTH
    # flags must be true before any decoding runs. `authorized` is an explicit
    # attestation that you are permitted to receive these public broadcasts in
    # your jurisdiction. Decoding is strictly clear-frame ISM telemetry — never
    # decryption, voice, or cellular.
    "decode": {
        "enabled": False,
        "authorized": False,
        "device": None,                 # dongle serial; null = first free
        # rtl_433 listens with frequency hopping across these (it decodes 315
        # and 433.92 simultaneously well; 915 added for ITU-2 devices).
        "frequencies": ["433.92M", "315M", "915M"],
        "hop_seconds": 30,
        "protocols": "all",             # all unencrypted ISM decoders
    },

    # Location from a gpsd (e.g. the TowerWitch Pi, exposed with `gpsd -G`).
    # A poller holds the latest fix in fix_file; Store auto-stamps every
    # sighting with lat/lon so all sensors become location-aware for free.
    "gps": {
        "enabled": True,
        "host": "127.0.0.1",            # override in config.local.json (e.g. TowerWitch)
        "port": 2947,
        "fix_file": "/run/tmt/gps.json",
        "max_age_seconds": 30,          # don't stamp sightings with a stale fix
    },

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
