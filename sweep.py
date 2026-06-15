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

from tmt.db import Store
from tmt.devices import list_devices, resolve_serial
from tmt.sdr_sensor import SDRSensor, BANDS

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DIM = "\033[2m"; STAR = "\033[93m"; WARN = "\033[91m"; RST = "\033[0m"


def print_peak(ts, band, mhz, power, snr):
    t = time.strftime("%H:%M:%S", time.localtime(ts))
    hot = STAR if snr >= 20 else DIM
    print(f"{hot}[{t}]   ism-{band:<4} {mhz:10.3f} MHz   "
          f"{power:6.1f} dB   (+{snr:.0f} dB over floor){RST}")


def print_skip(band, reason):
    t = time.strftime("%H:%M:%S")
    print(f"{WARN}[{t}]   ism-{band:<4} SKIPPED — dongle busy ({reason}){RST}")


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
    ap.add_argument("--once", action="store_true", help="one sweep then exit")
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
                       on_event=print_peak, on_skip=print_skip)

    print(f"Track_My_Tracker - SDR sweep | session={args.session} | "
          f"SN={serial} | bands={','.join(bands)}")
    print("Peaks above noise floor logged by frequency; busy dongle is skipped. "
          "Ctrl-C to stop.\n")
    try:
        while True:
            peaks, skips = sensor.sweep_once()
            if not peaks and not skips:
                t = time.strftime("%H:%M:%S")
                print(f"{DIM}[{t}]   (sweep complete, no peaks above "
                      f"{args.threshold:g} dB){RST}")
            if args.once or args.interval <= 0:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        store.close()


if __name__ == "__main__":
    main()
