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

    # Thermal backstop. IMPORTANT: the only readable sensor is the Pi SoC DIE
    # (`cpu-thermal`). RTL-SDR dongles have NO sensor, and the die is NOT a
    # proxy for them — measured on this rig, the die sat at 82 C mean while both
    # radios were released and provably idle, because die temperature tracks CPU
    # load, not radio activity. The heat that used to trip this was the
    # dashboard API recomputing million-row views every 5s (fixed by the
    # _ttl_cache in tmt/api.py), and pausing the SDRs never touched it.
    #
    # So these thresholds are about the COMPUTER's health only, sized for a
    # Pi 5 (BCM2712 throttles itself ~80 C soft / ~85 C hard). resume_c sits
    # above this rig's ~70 C idle so it is actually reachable, and
    # max_pause_seconds bounds a pause so the governor can never latch the
    # sensors off the way the old resume_c of 60 C did.
    "thermal": {
        "enabled": True,
        "soft_c": 80.0,              # stretch the sweep interval a little
        "hard_c": 85.0,              # SoC hard limit: back off briefly
        "resume_c": 78.0,            # reachable hysteresis (idle is ~70 C)
        "poll_seconds": 15.0,
        "max_interval_factor": 4.0,
        "max_pause_seconds": 300.0,  # never park longer than this; 0 = unbounded
    },

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

        # Duty cycle. rtl_433 streams continuously, so left alone it keeps the
        # tuner and ADC powered 100% of the time — on a two-dongle rig that is
        # the dominant heat source, and unlike the sweep it never idles. Running
        # it in bounded bursts sheds that heat AND is the only way the thermal
        # governor can act on it at all: rtl_433 owns the radio until it exits,
        # so the gap between bursts is the governor's only decision point.
        # burst 60 / idle 180 = 25% duty. Set enabled=false for the old
        # always-on behaviour.
        "duty": {
            "enabled": True,
            "burst_seconds": 60,        # rtl_433 -T: receive this long, then exit
            "idle_seconds": 180,        # radio off; governor re-checked before
                                        # the next burst (stretched when warm)
        },
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

    # Breadcrumb track log (tmt/track.py). Writes OUR path to the `track`
    # table on the GPS's cadence, which is what makes trips, distance, duration
    # and speed answerable -- a sighting's position only exists when a radio
    # happened to hear something at the same moment.
    "track": {
        "enabled": True,
        "min_dist_m": 25.0,             # crumb spacing while moving (~64/mile)
        "min_heading_deg": 25.0,        # ...but always crumb a real corner
        "min_interval_seconds": 1.0,    # never faster than gpsd's own 1 Hz
        "park_heartbeat_seconds": 30.0, # parked: prove we stayed put
        "move_start_mps": 1.5,          # ~3.4 mph opens a trip
        "move_stop_mps": 0.7,           # ~1.6 mph is a candidate for closing
        "stop_seconds": 180.0,          # ...sustained, so lights don't split it
        "max_gap_seconds": 300.0,       # beyond this, distance is unknowable
        "flush_seconds": 10.0,
        "max_pending": 20,
    },

    # ADS-B aircraft via dump1090 (we read its JSON output, not the radio, so
    # no dongle contention). ADS-B is openly broadcast / legal to receive, so
    # unlike decode it needs no authorization gate. The loiter detector flags
    # aircraft that ORBIT (stay within a small radius for a sustained time)
    # rather than transit — the surveillance/firefighting/spotter signature.
    "adsb": {
        "enabled": True,
        "json": "/run/dump1090-mutability/aircraft.json",
        "poll_seconds": 5,
        "mirror_seconds": 60,        # how often an aircraft re-mirrors to sightings
        "loiter_minutes": 5,         # min time on-station to call it loitering
        "loiter_radius_km": 18,      # stays within this radius => orbiting
    },

    # SDR dongle inventory. List the dongle serials you expect on the bus so the
    # dashboard can show "present/expected" and name which one dropped — the cue
    # that USB power was cut (e.g. a battery box's idle auto-shutoff) rather than
    # a heat problem. Empty = just report whatever dongles are currently present.
    "sdr": {
        "expected_serials": [],
        # Cooperative dongle release (the dashboard's "Release SDRs" button).
        # The SDR loops park without opening a dongle while this file exists, so
        # OP25 can claim the radio. On tmpfs on purpose: a reboot always clears
        # it, so a release can never become accidentally permanent.
        "release_file": "/run/tmt/radio-release",
        "release_poll_seconds": 5.0,
    },

    "api": {"host": "0.0.0.0", "port": 8100},
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
