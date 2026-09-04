"""RTL-SDR sweep sensor: detect recurrent RF energy without decoding it.

We don't demodulate. We sweep an ISM band with `rtl_power`, find frequency bins
whose power sits well above the local noise floor, and log each peak as a
sighting keyed by frequency. A covert tracker that beacons its location on, say,
433.92 MHz will show up as the *same frequency peak across multiple sessions* —
which the recurrence scorer treats exactly like a recurring BLE tracker.

Bands default to the unlicensed ISM segments where cheap GPS/GSM/LoRa telemetry
trackers, key fobs, and TPMS live. 433 (worldwide) + 915 (US/ITU-2). Add 868 for
EU/LoRa. The R820T2 tuner in the NESDR covers all of them.

The routine bands are NARROWED to the slices this rig actually sees traffic in.
That buys sensitivity (more dwell per bin), not cooling — on-air time is driven
by integration time and invocation count, not span. See BANDS / RECON_BANDS and
DEFAULT_INTEGRATION below for the measurements behind that.
"""

import csv
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

# band -> [(low_hz, high_hz, bin_step_hz), ...]   (a band may be several slices)
#
# WHAT ACTUALLY COSTS HEAT (measured on this rig, rtl_power -1):
#   span        902-928 MHz -> 3.2s | 915.5-917.5 -> 2.4s | 921.0-922.2 -> 2.0s
#   integration -i 1 -> 1.3s | -i 2 -> 1.9s | -i 4 -> 3.9s   (same span)
# So on-air time is set by the INTEGRATION TIME and the NUMBER of rtl_power
# invocations (each carries a ~1.3s dongle-init floor) — NOT by how much
# spectrum a single invocation covers. Widening a span is nearly free; adding a
# separate slice costs a whole extra invocation. Keep each band to ONE
# contiguous slice unless you genuinely need the gap excluded.
#
# Narrowing the 900 band therefore buys sensitivity, not cooling: the same
# integration time spread over 320 bins instead of 1040 is ~3x the dwell per
# bin. Sized from 7 days of this rig's own peaks: 915-923 MHz carries 99.9% of
# them (7661 of 7672 hits), so one span covers effectively everything we have
# ever seen in a single invocation. Because span is nearly free, err WIDE here
# — a tight window costs coverage and saves almost no heat.
#
# Sizing caveat: _detect_peaks estimates the noise floor as the MEDIAN of a
# slice's bins, so a slice needs enough bins for that median to still be noise
# rather than the signal you are hunting. Keep slices to a few dozen bins min.
#
# The full spans live in RECON_BANDS and are still swept every recon_every
# cycles, so an emitter appearing OUTSIDE these slices is still found — on a
# slower cadence rather than never.
BANDS = {
    "433": [(433_050_000, 434_790_000, 5_000)],
    # One span covering the whole live part of the 900 band (mesh cluster,
    # 918-921 activity, and the persistent 921.6 carrier).
    "915": [(915_000_000, 923_000_000, 25_000)],
}

# Full-span definitions, swept periodically as a wide "recon" pass so narrowing
# the routine bands above costs coverage in cadence, not in blind spots.
RECON_BANDS = {
    "433": [(433_050_000, 434_790_000, 5_000)],
    "915": [(902_000_000, 928_000_000, 25_000)],
    # "868": [(868_000_000, 868_600_000, 5_000)],   # enable for EU / LoRa
}

# rtl_power integration seconds. This is the sweep's main thermal dial: on-air
# time scales linearly with it. 1s over the narrowed routine spans still gives
# more dwell per bin than the old 2s over the full 26 MHz did.
DEFAULT_INTEGRATION = 1
# The recon pass covers ~6x the spectrum in one invocation, so give it more
# integration to keep per-bin dwell comparable. It runs 1 sweep in N, so the
# extra second is negligible against the routine cadence.
DEFAULT_RECON_INTEGRATION = 2


class DongleBusy(Exception):
    """rtl_power could not open the dongle (in use by OP25/another process)."""


def _run_rtl_power(low, high, step, *, device=0, gain=None,
                   integration=DEFAULT_INTEGRATION, timeout=60):
    """Single-shot sweep; return list of (freq_hz, power_db) for every bin.

    Raises DongleBusy if the dongle can't be opened, so the caller can skip
    this cycle and keep running — that's what lets us coexist with OP25, which
    may claim the radio at any time.
    """
    out = Path(tempfile.mkstemp(prefix="tmt_rtlpower_", suffix=".csv")[1])
    cmd = ["rtl_power", "-f", f"{low}:{high}:{step}",
           "-i", str(integration), "-1", "-d", str(device)]
    if gain is not None:
        cmd += ["-g", str(gain)]
    cmd += [str(out)]
    try:
        p = subprocess.run(cmd, timeout=timeout, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, text=True)
        if p.returncode != 0:
            err = (p.stderr or "").strip().splitlines()
            raise DongleBusy(err[-1] if err else f"rtl_power exit {p.returncode}")
        bins = []
        with out.open() as fh:
            for row in csv.reader(fh):
                if len(row) < 7:
                    continue
                f_low = float(row[2]); f_step = float(row[4])
                for i, val in enumerate(row[6:]):
                    val = val.strip()
                    if not val:
                        continue
                    try:
                        bins.append((f_low + i * f_step, float(val)))
                    except ValueError:
                        continue
        return bins
    except subprocess.TimeoutExpired:
        raise DongleBusy(f"rtl_power timed out after {timeout}s")
    finally:
        out.unlink(missing_ok=True)


def _detect_peaks(bins, threshold_db=12.0, min_gap_hz=50_000):
    """Cluster adjacent above-floor bins; return one peak per cluster.

    Floor is the median power across the band (robust to a few strong
    signals). A peak is the strongest bin in a run of bins that each exceed
    floor + threshold_db; clusters are split when bins are >min_gap_hz apart.
    """
    bins = sorted(bins)
    if not bins:
        return []
    floor = statistics.median(p for _, p in bins)
    cutoff = floor + threshold_db
    peaks, cluster = [], []

    def flush():
        if cluster:
            f, p = max(cluster, key=lambda fp: fp[1])
            peaks.append((f, p, p - floor))

    last_f = None
    for f, p in bins:
        if p < cutoff:
            flush(); cluster = []; last_f = None
            continue
        if last_f is not None and f - last_f > min_gap_hz:
            flush(); cluster = []
        cluster.append((f, p)); last_f = f
    flush()
    return peaks


class SDRSensor:
    def __init__(self, store, session, bands=None, device=0,
                 gain=None, threshold_db=12.0, recon_bands=None, recon_every=0,
                 integration=DEFAULT_INTEGRATION,
                 recon_integration=DEFAULT_RECON_INTEGRATION,
                 on_event=None, on_skip=None, on_recon=None):
        self.store = store
        self.session = session
        self.bands = bands or BANDS
        self.device = device              # serial (preferred) or index
        self.gain = gain
        self.threshold_db = threshold_db
        # Every recon_every-th sweep uses the full spans instead of the narrow
        # routine slices. 0 (or no recon_bands) disables the wide pass.
        self.recon_bands = recon_bands
        self.recon_every = int(recon_every or 0)
        # Seconds of integration per rtl_power call — the sweep's thermal dial.
        self.integration = integration
        self.recon_integration = recon_integration
        self.on_event = on_event or (lambda *a, **k: None)
        self.on_skip = on_skip or (lambda *a, **k: None)
        self.on_recon = on_recon or (lambda *a, **k: None)
        self.sweeps = 0

    def sweep_once(self, abort=None):
        """Run one sweep of every band slice; log + return (peaks, skips).

        Normally sweeps the narrowed routine bands. Every recon_every-th call
        sweeps RECON_BANDS (the full spans) instead, so an emitter outside the
        narrow slices still surfaces. Peaks are stored identically either way —
        the frequency is the identity, so recon and routine data interleave
        cleanly in the recurrence scorer.

        A slice whose dongle is busy is skipped (not fatal) so the service keeps
        running alongside OP25.

        `abort` is an optional predicate checked BETWEEN slices. A sweep covers
        several rtl_power invocations, so without it a dongle release mid-sweep
        would still open the radio again for the remaining slices.
        """
        session = self.session() if callable(self.session) else self.session
        self.sweeps += 1
        recon = (bool(self.recon_bands) and self.recon_every > 0
                 and self.sweeps % self.recon_every == 0)
        bands = self.recon_bands if recon else self.bands
        integration = self.recon_integration if recon else self.integration
        if recon:
            self.on_recon(sweep=self.sweeps, bands=sorted(bands))

        found, skips = [], []
        for band, segments in bands.items():
            for low, high, step in segments:
                if abort is not None and abort():
                    return found, skips     # give the radio up immediately
                now = time.time()
                try:
                    bins = _run_rtl_power(low, high, step, device=self.device,
                                          gain=self.gain,
                                          integration=integration)
                except DongleBusy as e:
                    skips.append((band, str(e)))
                    self.on_skip(band=band, reason=str(e))
                    continue
                for freq, power, snr in _detect_peaks(bins, self.threshold_db):
                    mhz = freq / 1e6
                    self.store.add_sighting(
                        radio="sdr",
                        address=f"{mhz:.3f}MHz",  # frequency is the identity
                        name=f"ism-{band}",
                        rssi=round(power),        # peak power, dB (rtl_power units)
                        tracker_type=None,
                        session=session,
                        ts=now,
                    )
                    found.append((band, mhz, power, snr))
                    self.on_event(ts=now, band=band, mhz=mhz, power=power,
                                  snr=snr)
                self.store.commit()
        return found, skips
