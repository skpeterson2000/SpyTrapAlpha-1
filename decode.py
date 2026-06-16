#!/usr/bin/env python3
"""Track_My_Tracker — ISM decode (rtl_433). SHIPS INERT.

Decoding runs ONLY when it is both enabled and authorized:
  - config decode.enabled = true   (turn the feature on), and
  - config decode.authorized = true  (you attest you are permitted to receive
    these unencrypted public broadcasts in your jurisdiction),
  or pass --enable --i-am-authorized for a one-off attended run.

It decodes only clear, unencrypted ISM device telemetry — never encrypted,
voice, or cellular traffic.

    ./.venv/bin/python decode.py --enable --i-am-authorized --duration 30
"""

import argparse
import sys
import time

from tmt import config as configmod
from tmt.db import Store
from tmt.decode_sensor import DecodeSensor
from tmt.devices import resolve_serial

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DIM = "\033[2m"; CYAN = "\033[96m"; WARN = "\033[91m"; RST = "\033[0m"

ATTEST = (
    "Decode is gated. It is OFF until you explicitly authorize it.\n"
    "  - Set decode.enabled=true and decode.authorized=true in "
    "config.local.json, OR\n"
    "  - pass --enable --i-am-authorized for a one-off run.\n"
    "By authorizing you attest you may lawfully receive these UNENCRYPTED "
    "public ISM broadcasts in your jurisdiction. This tool decodes only clear "
    "device telemetry — never encrypted, voice, or cellular traffic.")


def print_decode(ts, model, identity, freq, rssi, frame):
    t = time.strftime("%H:%M:%S", time.localtime(ts))
    extra = []
    for k in ("type", "channel", "temperature_C", "pressure_kPa", "battery_ok"):
        if k in frame:
            extra.append(f"{k}={frame[k]}")
    f = f"{freq:.1f}MHz" if isinstance(freq, (int, float)) else "?"
    print(f"{CYAN}[{t}] DECODE {model:<22}{RST} id={identity.split(':')[-1]:<10} "
          f"{f:>9} {('%g dB' % rssi) if rssi is not None else '':>7}  "
          f"{DIM}{' '.join(extra)}{RST}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=time.strftime("dec-%Y%m%d-%H%M%S"))
    ap.add_argument("--device", default=None, help="dongle serial; default config/first")
    ap.add_argument("--rotate-minutes", type=float, default=0.0)
    ap.add_argument("--duration", type=float, default=0.0,
                    help="seconds then stop (0 = run forever)")
    ap.add_argument("--enable", action="store_true",
                    help="bypass config decode.enabled for this run")
    ap.add_argument("--i-am-authorized", action="store_true",
                    help="attest lawful authorization for this run")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    cfg = configmod.load()
    dcfg = cfg["decode"]
    enabled = dcfg.get("enabled") or args.enable
    authorized = dcfg.get("authorized") or args.i_am_authorized
    if not (enabled and authorized):
        print(f"{WARN}Decode not authorized — refusing to run.{RST}\n{ATTEST}")
        sys.exit(3)

    serial = resolve_serial(args.device or dcfg.get("device") or "0")
    if serial is None:
        print(f"{WARN}No RTL-SDR dongle matches the decode device.{RST}")
        sys.exit(2)

    base = args.session
    if args.rotate_minutes > 0:
        bucket = args.rotate_minutes * 60.0

        def session():
            now = time.time(); start = now - (now % bucket)
            return base + "-" + time.strftime("%Y%m%d-%H%M", time.localtime(start))
    else:
        session = base

    store = Store(args.db) if args.db else Store()
    sensor = DecodeSensor(store, session=session, device=serial,
                          frequencies=dcfg.get("frequencies"),
                          hop_seconds=dcfg.get("hop_seconds", 30),
                          on_event=print_decode)

    print(f"SpyTrap - ISM decode | SN={serial} | "
          f"freqs={','.join(dcfg.get('frequencies', []))} | "
          f"hop={dcfg.get('hop_seconds')}s")
    print("Decoding UNENCRYPTED ISM device frames only. Ctrl-C to stop.\n")
    try:
        sensor.run(duration=args.duration)
    except KeyboardInterrupt:
        pass
    finally:
        store.close()
        print(f"\nDecoded {sensor.count} frames -> {store.path}")


if __name__ == "__main__":
    main()
