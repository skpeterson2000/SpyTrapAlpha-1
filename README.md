# SpyTrap

*System name: `Track_My_Tracker` (repo, service, and package identifiers).*

> Software that employs AI to survey observed radio signals and reports
> recurrent ones that are potential threats to the user.

A personal, **defensive** counter-surveillance sensor. It watches the radio
around you — Bluetooth Low Energy advertisements and RF energy in the ISM bands
— and flags signals that **keep reappearing across time and place**, the way a
tracker hidden in your bag or car would. Same idea as the "unknown AirTag
travelling with you" alert, but general-purpose and entirely under your own
control.

It is built to run unattended on a Raspberry Pi.

## How it works

```
 BLE radio ─┐                              ┌─ audible (your sounds)
 RTL-SDR  ──┼─> sightings.db ─> alertd ──> ┼─ push (ntfy / Pushover)
 (sweep)   ─┘   (append-only)   (scores)   ├─ log + alerts table
                                           └─ JSON/SSE API ─> web dashboard
```

1. **Sensors** log every observation to an append-only SQLite store.
   - `scan.py` — BLE advertisements, with a classifier for known tracker
     signatures (Apple Find My, Tile, Samsung SmartTag, Google FMDN).
   - `sweep.py` — RTL-SDR `rtl_power` sweep of the 433/915 MHz ISM bands;
     peaks are logged by frequency.
2. **Scoring** (`tmt/score.py`) ranks each identity (a BLE address or an SDR
   frequency) by how much it behaves like something following you: recurring
   across **sessions**, persistent over time, and/or a known tracker class.
3. **Curation** (labels) lets you tag any identity **Mine / Safe / Watch /
   Threat / Ignore** (with a name + notes). The scorer suppresses the benign
   (drops them to *info* and sinks them) and escalates threats to HIGH, turning
   a noisy list into a real watchlist. Click any row in the dashboard to tag it.
4. **Alerting** (`alertd.py`) raises tiered alerts (LOW/MED/HIGH), deduped per
   identity, skipping suppressed ones, and fans them out to audible / push /
   log / database.
5. **Decoding** (optional, gated — `decode.py`) decodes *unencrypted* ISM
   device frames via `rtl_433`; a recurring decoded device id (e.g. a TPMS tire
   sensor) becomes a first-class recurrence identity. **Ships inert** — see below.
6. **API + dashboard** (`serve.py`, `web/`) expose everything as JSON plus a
   live Server-Sent-Events stream, with a self-contained web UI.

### Why "sessions"?
Recurrence is the whole game: a device seen once in one place is noise; the same
one seen again after you've **moved** is a threat. The sensors auto-segment a
continuous run into time-bucketed *sessions*, and the scorer rewards identities
that span many of them.

> **Note on AirTags:** Apple Find My tags rotate their MAC ~every 15 min, so a
> single tag fragments into many short-lived addresses. The report's
> *tracker-class activity* view is MAC-agnostic to compensate.

## Quick start

```bash
python3 -m venv .venv
./.venv/bin/pip install bleak fastapi "uvicorn[standard]"

# one-off live scans
./.venv/bin/python scan.py  --session home --duration 30
./.venv/bin/python sweep.py --device <serial> --once
./.venv/bin/python report.py            # cross-session threat report

# the API + dashboard
./.venv/bin/python serve.py             # http://<host>:8100/
```

`./.venv/bin/python -m tmt.devices` lists RTL-SDR dongles by serial.

## Running as services

Continuous, restart-on-failure, start-on-boot systemd units (see
[`deploy/`](deploy/)):

| Unit | Role |
|------|------|
| `tmt-ble.service` | continuous BLE logger |
| `tmt-sdr@<serial>.service` | SDR sweep — **one instance per dongle, by serial** |
| `tmt-alertd.service` | scoring + alert dispatch |
| `tmt-api.service` | JSON/SSE API + web dashboard |

**Dongles are addressed by serial, never index** — indices renumber when you
add/remove a dongle. This keeps each instance pinned to its radio and lets the
sweep coexist with another SDR consumer (e.g. OP25): a busy dongle is skipped,
not fatal. Hand a dongle to another app simply by not enabling Track_My_Tracker
on that serial.

## Configuration

Defaults live in `tmt/config.py`. Override locally without touching the repo by
creating `config.local.json` (gitignored), e.g.:

```json
{
  "alert_tiers": ["MED", "HIGH"],
  "cooldown_minutes": 30,
  "audio": { "sounds": { "HIGH": "alert-high.wav" } }
}
```

Push secrets come from the environment (never the repo):

```bash
export TMT_NTFY_TOPIC=your-secret-topic        # ntfy.sh
# or
export TMT_PUSHOVER_TOKEN=...  TMT_PUSHOVER_USER=...
```

Alert sounds: see [`sounds/README.md`](sounds/README.md).

## API

| Endpoint | Returns |
|----------|---------|
| `GET /api/health` | liveness + store path |
| `GET /api/stats` | sighting / alert / session counts |
| `GET /api/suspects?hours=&min_tier=` | live ranked scoring |
| `GET /api/alerts?hours=&limit=` | recent persisted alerts |
| `GET /api/stream` | SSE stream of new alerts |

## Ports

SpyTrap owns the **8100–8109** block (block-per-service convention, so each
program has room for auxiliary ports without colliding):

| Port | Use |
|------|-----|
| **8100** | JSON/SSE API + web dashboard |
| 8101–8109 | reserved (metrics / websocket / admin / future) |

Set via `api.port` in config. Neighboring blocks in this deployment: 8080–8089
(OP25), 8090–8099 (separate program). The API binds `0.0.0.0` (LAN-reachable);
set `api.host` to `127.0.0.1` to keep it local-only.

## Decoding (optional, gated)

Beyond detecting *that* a signal recurs, Track_My_Tracker can decode the
**unencrypted** device telemetry already broadcast in the clear on the ISM
bands (via [`rtl_433`](https://github.com/merbanan/rtl_433)) — TPMS tire
sensors, weather stations, remotes, asset tags. The standout for anti-stalking:
a **TPMS sensor id** is broadcast openly, so a vehicle tailing you shows the
same sensor ids reappearing across your sessions — a specific "this car is
following me" signal. Decoded ids flow into the same recurrence / labels /
alerts machinery.

**This is deliberately gated and ships disabled.** It decodes only clear,
unencrypted ISM device frames — never encrypted, voice, or cellular traffic.
Nothing decodes until you turn it on *and* attest authorization:

```bash
sudo apt install rtl-433

# one-off attended run:
./.venv/bin/python decode.py --enable --i-am-authorized --duration 60

# or persistent: set both in config.local.json, then enable the service
#   { "decode": { "enabled": true, "authorized": true } }
sudo systemctl enable --now tmt-decode@<serial>.service
```

By enabling it you attest you may lawfully receive these public unencrypted
broadcasts in your jurisdiction. Point it at a dongle serial no other service
(sweep / OP25) is using.

## Ethics & legality

This is a **defensive** tool: it passively observes signals already being
broadcast publicly, to protect the person running it. Use it on yourself and
your own property. Don't use it to track other people.

## License

MIT — see [LICENSE](LICENSE).
