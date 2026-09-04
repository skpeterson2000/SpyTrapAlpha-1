"""Thermal backstop — react when the PI is in trouble, not the dongles.

WHAT THIS MEASURES, AND WHAT IT DOES NOT. The only readable sensor here is
/sys/class/thermal/thermal_zone0 = `cpu-thermal`, the BCM2712 SoC DIE. RTL-SDR
dongles expose no temperature sensor at all.

This module used to treat the SoC temperature as a proxy for dongle/enclosure
heat. That premise was WRONG and is retired. Measured on this rig:

  * With both radios released and provably idle, the die still read 82 C mean /
    85 C max — as hot as it ever got with them running. Die temperature tracks
    CPU load, not radio activity.
  * Two RTL-SDRs draw about a watt between them, on a powered hub outside the
    SoC package; they cannot meaningfully move that die.
  * A die at 82 C sits behind a heatspreader and case that are cool to the
    touch — which is exactly what the dongles feel like in hand.

The heat that triggered every pause was the dashboard API recomputing million-
row views on a 5s poll (see _ttl_cache in tmt/api.py). Parking the SDRs never
addressed it; it just stopped the rig collecting.

So this is now a narrow BACKSTOP on the computer's own health, never a claim
about the radios:
  OK    — run normally.
  SOFT  — die at soft_c: stretch the duty cycle a little.
  HARD  — the SoC reports it is ACTIVELY throttling itself (its own limit, not
          a number we invented), or the die passes hard_c. Back off briefly.

A HARD pause is BOUNDED by max_pause_seconds and resume_c sits above the rig's
idle temperature. Both exist because the old hysteresis (resume at 60 C on a box
that idles near 70) could never be satisfied: one trip parked the SDRs forever,
silently. A governor that can permanently disable a counter-surveillance sensor
is a worse failure than one that runs it warm.

All reads are best-effort: no sensor means OK, never a block.
"""

import subprocess
import time
from pathlib import Path

# vcgencmd get_throttled bit meanings (Raspberry Pi). We treat the SoC's own
# "soft temperature limit active" bit as a hard thermal signal regardless of
# our numeric threshold — if the SoC is already throttling for heat, so should
# we be.
THROTTLE_SOFT_TEMP = 1 << 3        # soft temperature limit active NOW
THROTTLE_THROTTLED = 1 << 2        # currently throttled NOW
THROTTLE_UNDERVOLT = 1 << 0        # informational; surfaced for logging
# NOTE bits 16-19 are STICKY ("has occurred since boot") and must never gate
# anything: 0xe0000 just means the box got hot at some point today.
THROTTLE_ACTIVE = THROTTLE_SOFT_TEMP | THROTTLE_THROTTLED

_THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0/temp")

OK, SOFT, HARD = "OK", "SOFT", "HARD"


def read_soc_temp_c():
    """SoC temperature in C, or None if it can't be read.

    Prefers the sysfs thermal zone (millidegrees, no subprocess); falls back to
    `vcgencmd measure_temp`.
    """
    try:
        raw = _THERMAL_ZONE.read_text().strip()
        if raw:
            return int(raw) / 1000.0
    except Exception:
        pass
    try:
        out = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True,
                             text=True, timeout=5).stdout
        # form: "temp=82.3'C"
        return float(out.split("=", 1)[1].split("'", 1)[0])
    except Exception:
        return None


def read_throttle_flags():
    """`vcgencmd get_throttled` as an int, or None if unavailable."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=5).stdout
        return int(out.split("=", 1)[1].strip(), 16)   # "throttled=0x80008"
    except Exception:
        return None


class Governor:
    """Decides OK/SOFT/HARD from SoC temperature + throttle flags."""

    def __init__(self, enabled=True, soft_c=80.0, hard_c=85.0, resume_c=78.0,
                 poll_seconds=15.0, max_interval_factor=4.0,
                 max_pause_seconds=300.0, on_state=None):
        self.enabled = enabled
        self.soft_c = float(soft_c)
        self.hard_c = float(hard_c)
        self.resume_c = float(resume_c)
        self.poll_seconds = float(poll_seconds)
        self.max_interval_factor = float(max_interval_factor)
        # Hard cap on a pause so the governor can never latch. 0 = unbounded.
        self.max_pause_seconds = float(max_pause_seconds or 0)
        # on_state(event, temp_c, flags):
        #   event in {"pause","waiting","resume","timeout"}
        self.on_state = on_state or (lambda *a, **k: None)

    @classmethod
    def from_config(cls, cfg, on_state=None):
        t = (cfg or {}).get("thermal", {}) if isinstance(cfg, dict) else {}
        return cls(
            enabled=t.get("enabled", True),
            soft_c=t.get("soft_c", 80.0),
            hard_c=t.get("hard_c", 85.0),
            resume_c=t.get("resume_c", 78.0),
            poll_seconds=t.get("poll_seconds", 15.0),
            max_interval_factor=t.get("max_interval_factor", 4.0),
            max_pause_seconds=t.get("max_pause_seconds", 300.0),
            on_state=on_state,
        )

    def assess(self):
        """Return (level, temp_c). temp_c may be None if unreadable."""
        if not self.enabled:
            return OK, None
        t = read_soc_temp_c()
        flags = read_throttle_flags()
        soc_throttling = flags is not None and bool(flags & THROTTLE_ACTIVE)
        # Can't read anything and the SoC isn't reporting a thermal limit:
        # fail open so a missing sensor never blocks the scanner.
        if t is None and not soc_throttling:
            return OK, None
        if soc_throttling or (t is not None and t >= self.hard_c):
            return HARD, t
        if t is not None and t >= self.soft_c:
            return SOFT, t
        return OK, t

    def adjust_interval(self, base_interval, level):
        """Stretch the sweep interval when warm; unchanged when OK.

        Scales linearly from 1x at soft_c to max_interval_factor at hard_c.
        """
        if level != SOFT or base_interval <= 0:
            return base_interval
        t = read_soc_temp_c()
        if t is None or self.hard_c <= self.soft_c:
            return base_interval * self.max_interval_factor
        frac = (t - self.soft_c) / (self.hard_c - self.soft_c)
        frac = max(0.0, min(1.0, frac))
        factor = 1.0 + frac * (self.max_interval_factor - 1.0)
        return base_interval * factor

    def wait_until_safe(self, sleep_fn):
        """If HARD, block (polling every poll_seconds) until cooled to resume_c
        — but NEVER for longer than max_pause_seconds.

        The cap is not a nicety. With the old unreachable hysteresis this loop
        parked both SDR loops indefinitely and silently: a 25-minute soak found
        0% radio duty, 0 sweeps, 0 decodes, and no crash to explain it. Sensing
        nothing is the one outcome a counter-surveillance rig must not fail
        into, so when the box will not cool we return SOFT and let the caller
        run at a stretched interval instead of not at all.

        `sleep_fn` is injected (time.sleep in production) so this stays testable
        and interruptible. Returns (level, temp_c) once it is safe to proceed.
        """
        level, t = self.assess()
        if level != HARD:
            return level, t
        flags = read_throttle_flags()
        self.on_state("pause", t, flags)
        deadline = (time.monotonic() + self.max_pause_seconds
                    if self.max_pause_seconds > 0 else None)
        while True:
            sleep_fn(self.poll_seconds)
            t = read_soc_temp_c()
            flags = read_throttle_flags()
            soc_throttling = flags is not None and bool(flags & THROTTLE_ACTIVE)
            # Both reads failing: we can no longer tell — fail open rather than
            # pause forever.
            if t is None and flags is None:
                self.on_state("resume", t, flags)
                return OK, t
            if not soc_throttling and (t is None or t <= self.resume_c):
                self.on_state("resume", t, flags)
                return OK, t
            if deadline is not None and time.monotonic() >= deadline:
                self.on_state("timeout", t, flags)
                return SOFT, t
            self.on_state("waiting", t, flags)
