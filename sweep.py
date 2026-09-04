#!/usr/bin/env python3
"""Track_My_Tracker — RTL-SDR ISM sweep entry point.

    ./.venv/bin/python sweep.py --list-devices
    ./.venv/bin/python sweep.py --device 19481419 --once
    ./.venv/bin/python sweep.py --device 19481419 --bands 433 --interval 60

Dongles are addressed by SERIAL (stable across re-plugging and independent of
OP25), not index. A busy dongle is skipped, not fatal, so this coexists with
OP25 claiming another radio. Detected peaks are logged by frequency so recurrent
peaks score like recurrent trackers.
"""

import argparse
import os
import sys
import time

from tmt import config as configmod
from tmt.db import Store
from tmt.devices import list_devices, resolve_serial
from tmt.sdr_sensor import (SDRSensor, BANDS, RECON_BANDS,
                            DEFAULT_INTEGRATION,
                            DEFAULT_RECON_INTEGRATION)
from tmt import radio_control
from tmt.thermal import Governor

try:
    # line_buffering so THERMAL/RELEASE state changes reach journald as they
    # happen — block buffering makes a parked sensor look silently dead.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                           line_buffering=True)
except Exception:
    pass

DIM = "\033[2m"; STAR = "\033[93m"; WARN = "\033[91m"; RST = "\033[0m"


def print_peak(ts, band, mhz, power, snr):
    t = time.strftime("%H:%M:%S", time.localtime(ts))
    hot = STAR if snr >= 20 else DIM
    print(f"{hot}[{t}]   ism-{band:<4} {mhz:10.3f} MHz   "
          f"{power:6.1f} dB   (+{snr:.0f} dB over floor){RST}")


def print_skip(band, reason):
    if radio_control.is_released():
        return          # we killed that helper on purpose; already announced
    t = time.strftime("%H:%M:%S")
    print(f"{WARN}[{t}]   ism-{band:<4} SKIPPED — dongle busy ({reason}){RST}")


def print_recon(sweep, bands):
    t = time.strftime("%H:%M:%S")
    print(f"{STAR}[{t}]   RECON — sweep #{sweep}: full-span pass over "
          f"{','.join(bands)} (catches emitters outside the narrow slices){RST}")


def print_release(event, state):
    t = time.strftime("%H:%M:%S")
    if event == "released":
        why = (state or {}).get("reason") or "requested from the dashboard"
        print(f"{WARN}[{t}]   SDR RELEASED — {why}. Parking with the dongle "
              f"free so OP25 can claim it; resume from the dashboard.{RST}")
    else:
        print(f"{STAR}[{t}]   SDR RESUMED — reclaiming the dongle.{RST}")


def print_thermal(event, temp_c, flags):
    t = time.strftime("%H:%M:%S")
    tc = f"{temp_c:.1f}C" if temp_c is not None else "?C"
    if event == "pause":
        print(f"{WARN}[{t}]   THERMAL PAUSE — the Pi SoC is throttling ({tc}); "
              f"backing off briefly. NOTE this is CPU heat, not the dongles — "
              f"check what is loading the CPU before adding cooling.{RST}")
    elif event == "resume":
        print(f"{STAR}[{t}]   THERMAL OK — SoC {tc}; resuming sweeps.{RST}")
    elif event == "timeout":
        print(f"{WARN}[{t}]   THERMAL — SoC still {tc} after the pause cap; "
              f"resuming sweeps anyway at a stretched interval. Sensing "
              f"nothing is the worse failure.{RST}")
    else:  # waiting
        print(f"{DIM}[{t}]   …waiting for the SoC to settle, {tc}{RST}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=time.strftime("sdr-%Y%m%d-%H%M%S"))
    ap.add_argument("--device", default=None,
                    help="dongle SERIAL (preferred), suffix, or index. "
                         "Default: first dongle found.")
    ap.add_argument("--gain", default=None, help="tuner gain dB, or 'auto'")
    ap.add_argument("--threshold", type=float, default=12.0,
                    help="dB above noise floor to count as a peak")
    ap.add_argument("--bands", default="",
                    help="comma list of band keys (e.g. 433,915). Empty = all.")
    ap.add_argument("--interval", type=float, default=0.0,
                    help="seconds between sweeps (0 with --once = single sweep)")
    ap.add_argument("--rotate-minutes", type=float, default=0.0,
                    help="auto-segment session into N-minute buckets so the "
                         "scorer gets distinct windows (match scan.py).")
    ap.add_argument("--integration", type=float, default=DEFAULT_INTEGRATION,
                    help="rtl_power integration seconds per sweep. This is the "
                         "sweep's main thermal dial: tuner on-air time scales "
                         "linearly with it (span barely matters). Raise for "
                         "sensitivity, lower to run cooler.")
    ap.add_argument("--recon-integration", type=float,
                    default=DEFAULT_RECON_INTEGRATION,
                    help="integration seconds for the wide recon pass")
    ap.add_argument("--recon-every", type=int, default=20,
                    help="every Nth sweep, sweep the FULL band spans instead of "
                         "the narrowed routine slices (0 = never). The narrow "
                         "slices are what keep the tuner — and the enclosure — "
                         "cool; this is the periodic wide look that stops them "
                         "from becoming a blind spot.")
    ap.add_argument("--once", action="store_true", help="one sweep then exit")
    ap.add_argument("--no-thermal", action="store_true",
                    help="disable the SoC thermal governor for this run")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    if args.list_devices:
        devs = list_devices()
        if not devs:
            print("No RTL-SDR dongles found.")
            return
        print("RTL-SDR dongles (address by SERIAL):")
        for d in devs:
            print(f"  index {d['index']}  SN {d['serial']:<12} "
                  f"{d['manufacturer']} {d['product']}")
        return

    # Resolve the requested dongle to a stable serial.
    serial = resolve_serial(args.device if args.device is not None else "0")
    if serial is None:
        print(f"{WARN}No dongle matches --device={args.device!r}. "
              f"Try --list-devices.{RST}")
        sys.exit(2)

    # Select bands. CLI wins; else TMT_BANDS env (per-dongle split); else all.
    bands_spec = args.bands.strip() or os.environ.get("TMT_BANDS", "").strip()
    if bands_spec:
        keys = [k.strip() for k in bands_spec.split(",") if k.strip()]
        bands = {k: BANDS[k] for k in keys if k in BANDS}
        unknown = [k for k in keys if k not in BANDS]
        if unknown:
            print(f"{WARN}Unknown band(s): {unknown}. Known: "
                  f"{list(BANDS)}{RST}")
        if not bands:
            sys.exit(2)
    else:
        bands = BANDS

    # Recon uses the full spans for whichever bands are actually selected, so
    # --bands stays the single switch for what this dongle covers.
    recon_every = max(0, args.recon_every)
    recon_bands = {k: RECON_BANDS[k] for k in bands if k in RECON_BANDS}
    if not recon_bands:
        recon_every = 0

    base = args.session
    if args.rotate_minutes > 0:
        bucket = args.rotate_minutes * 60.0

        def session():
            now = time.time()
            start = now - (now % bucket)
            return base + "-" + time.strftime("%Y%m%d-%H%M",
                                              time.localtime(start))
    else:
        session = base

    gain = None if args.gain in (None, "auto") else float(args.gain)
    store = Store(args.db) if args.db else Store()
    sensor = SDRSensor(store, session=session, bands=bands, device=serial,
                       gain=gain, threshold_db=args.threshold,
                       recon_bands=recon_bands, recon_every=recon_every,
                       integration=args.integration,
                       recon_integration=args.recon_integration,
                       on_event=print_peak, on_skip=print_skip,
                       on_recon=print_recon)

    # Thermal governor: pause sweeping when the SoC (a proxy for enclosure /
    # dongle heat — the dongles have no own sensor) gets too hot, and stretch
    # the interval when merely warm. Only governs the continuous loop; a one-off
    # or --no-thermal run is never blocked.
    gov = Governor.from_config(configmod.load(), on_state=print_thermal)
    if args.no_thermal:
        gov.enabled = False
    continuous = args.interval > 0 and not args.once

    span_mhz = sum((hi - lo) for segs in bands.values() for lo, hi, _ in segs) / 1e6
    print(f"SpyTrap - SDR sweep | session={args.session} | "
          f"SN={serial} | bands={','.join(bands)} | {span_mhz:g} MHz per sweep "
          f"| -i {args.integration:g}s")
    if recon_every:
        wide = sum((hi - lo) for segs in recon_bands.values()
                   for lo, hi, _ in segs) / 1e6
        print(f"Recon: full-span pass ({wide:g} MHz, -i "
              f"{args.recon_integration:g}s) every {recon_every} sweeps.")
    if gov.enabled:
        print(f"Thermal backstop: soft {gov.soft_c:g}C, pause {gov.hard_c:g}C, "
              f"resume {gov.resume_c:g}C, pause cap {gov.max_pause_seconds:g}s "
              f"— Pi SoC die, NOT the dongles.")
    print("Peaks above noise floor logged by frequency; busy dongle is skipped. "
          "Ctrl-C to stop.\n")
    try:
        while True:
            if continuous:
                # Released takes precedence over hot: if the user has asked for
                # the dongle, park without a radio rather than cooling with it.
                radio_control.wait_while_released(on_state=print_release)
                gov.wait_until_safe(time.sleep)   # blocks while HARD-hot
            elif radio_control.is_released():
                print_release("released", radio_control.read_state())
                break
            peaks, skips = sensor.sweep_once(
                abort=radio_control.is_released)
            if not peaks and not skips:
                t = time.strftime("%H:%M:%S")
                print(f"{DIM}[{t}]   (sweep complete, no peaks above "
                      f"{args.threshold:g} dB){RST}")
            if not continuous:
                break
            level, _ = gov.assess()
            time.sleep(gov.adjust_interval(args.interval, level))
    except KeyboardInterrupt:
        pass
    finally:
        store.close()


if __name__ == "__main__":
    main()
