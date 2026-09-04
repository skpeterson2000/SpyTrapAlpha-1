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
from tmt import radio_control
from tmt.thermal import Governor

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                           line_buffering=True)
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
                    help="total seconds then stop, across all duty-cycle "
                         "bursts (0 = run forever)")
    ap.add_argument("--no-duty", action="store_true",
                    help="ignore decode.duty and stream continuously (hot)")
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

    duty = dcfg.get("duty") or {}
    burst = float(duty.get("burst_seconds", 60) or 0)
    idle = float(duty.get("idle_seconds", 180) or 0)
    duty_on = bool(duty.get("enabled", True)) and burst >= 1 and not args.no_duty

    print(f"SpyTrap - ISM decode | SN={serial} | "
          f"freqs={','.join(dcfg.get('frequencies', []))} | "
          f"hop={dcfg.get('hop_seconds')}s")
    if duty_on:
        pct = 100.0 * burst / (burst + idle) if burst + idle else 100.0
        print(f"Duty cycle: {burst:g}s receiving / {idle:g}s radio off "
              f"(~{pct:.0f}% tuner on-time); governor checked between bursts.")
    else:
        print(f"{WARN}Duty cycle OFF — streaming continuously "
              f"(100% tuner on-time).{RST}")
    print("Decoding UNENCRYPTED ISM device frames only. Ctrl-C to stop.\n")

    # rtl_433 owns the radio until it exits, so the governor cannot interrupt a
    # burst mid-flight. Bounded bursts give it a decision point between each
    # one: HARD-hot -> wait_until_safe() blocks with the radio already off;
    # merely warm -> the idle gap is stretched, exactly as the sweep stretches
    # its interval. Continuous mode keeps the old startup-only gate.
    def _thermal(event, temp_c, flags):
        tc = f"{temp_c:.1f}C" if temp_c is not None else "?C"
        t = time.strftime("%H:%M:%S")
        msg = {
            "pause": f"SoC {tc} throttling; holding decode, radio off",
            "waiting": f"waiting for the SoC to settle, {tc}",
            "resume": f"SoC {tc}; resuming decode",
            # Bounded pause expired: run anyway rather than sense nothing.
            "timeout": (f"SoC still {tc} after the pause cap — resuming decode "
                        f"anyway; this is CPU heat, not the dongles"),
        }.get(event, f"SoC {tc}")
        print(f"{WARN if event != 'resume' else DIM}[{t}] THERMAL — {msg}{RST}")

    def _release(event, state):
        t = time.strftime("%H:%M:%S")
        if event == "released":
            why = (state or {}).get("reason") or "requested from the dashboard"
            print(f"{WARN}[{t}] SDR RELEASED — {why}. Decode parked with the "
                  f"dongle free for OP25; resume from the dashboard.{RST}")
        else:
            print(f"{DIM}[{t}] SDR RESUMED — reclaiming the dongle.{RST}")

    gov = Governor.from_config(cfg, on_state=_thermal)
    deadline = (time.time() + args.duration) if args.duration > 0 else None
    try:
        if not duty_on:
            radio_control.wait_while_released(on_state=_release)
            gov.wait_until_safe(time.sleep)
            sensor.run(duration=args.duration)
        else:
            while True:
                # Released takes precedence over hot: if the user has asked for
                # the dongle, park without a radio rather than cooling with it.
                # A release mid-burst already killed rtl_433, so we land here
                # immediately rather than after the remaining burst seconds.
                radio_control.wait_while_released(on_state=_release)
                gov.wait_until_safe(time.sleep)      # blocks while HARD-hot
                run_for = burst
                if deadline is not None:
                    remaining = deadline - time.time()
                    if remaining < 1:                # rtl_433 -T needs >=1s
                        break
                    run_for = min(burst, remaining)
                sensor.run(duration=run_for)
                if deadline is not None and time.time() >= deadline:
                    break
                if idle > 0:
                    # Radio is off for this gap — the heat we are shedding.
                    level, _ = gov.assess()
                    time.sleep(gov.adjust_interval(idle, level))
    except KeyboardInterrupt:
        pass
    finally:
        store.close()
        print(f"\nDecoded {sensor.count} frames -> {store.path}")


if __name__ == "__main__":
    main()
