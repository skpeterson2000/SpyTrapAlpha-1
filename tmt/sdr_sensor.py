"""RTL-SDR sweep sensor: detect recurrent RF energy without decoding it.

We don't demodulate. We sweep an ISM band with `rtl_power`, find frequency bins
whose power sits well above the local noise floor, and log each peak as a
sighting keyed by frequency. A covert tracker that beacons its location on, say,
433.92 MHz will show up as the *same frequency peak across multiple sessions* —
which the recurrence scorer treats exactly like a recurring BLE tracker.

Bands default to the unlicensed ISM segments where cheap GPS/GSM/LoRa telemetry
trackers, key fobs, and TPMS live. 433 (worldwide) + 915 (US/ITU-2). Add 868 for
EU/LoRa. The R820T2 tuner in the NESDR covers all of them.
"""

import csv
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

# band -> (low_hz, high_hz, bin_step_hz)
BANDS = {
    "433": (433_050_000, 434_790_000, 5_000),
    "915": (902_000_000, 928_000_000, 25_000),
    # "868": (868_000_000, 868_600_000, 5_000),   # enable for EU / LoRa
}


class DongleBusy(Exception):
    """rtl_power could not open the dongle (in use by OP25/another process)."""


def _run_rtl_power(low, high, step, *, device=0, gain=None,
                   integration=2, timeout=60):
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
                 gain=None, threshold_db=12.0, on_event=None, on_skip=None):
        self.store = store
        self.session = session
        self.bands = bands or BANDS
        self.device = device              # serial (preferred) or index
        self.gain = gain
        self.threshold_db = threshold_db
        self.on_event = on_event or (lambda *a, **k: None)
        self.on_skip = on_skip or (lambda *a, **k: None)

    def sweep_once(self):
        """Run one full sweep of all bands; log + return (peaks, skips).

        A band whose dongle is busy is skipped (not fatal) so the service keeps
        running alongside OP25.
        """
        session = self.session() if callable(self.session) else self.session
        found, skips = [], []
        for band, (low, high, step) in self.bands.items():
            now = time.time()
            try:
                bins = _run_rtl_power(low, high, step, device=self.device,
                                      gain=self.gain)
            except DongleBusy as e:
                skips.append((band, str(e)))
                self.on_skip(band=band, reason=str(e))
                continue
            for freq, power, snr in _detect_peaks(bins, self.threshold_db):
                mhz = freq / 1e6
                self.store.add_sighting(
                    radio="sdr",
                    address=f"{mhz:.3f}MHz",      # frequency is the identity
                    name=f"ism-{band}",
                    rssi=round(power),            # peak power, dB (rtl_power units)
                    tracker_type=None,
                    session=session,
                    ts=now,
                )
                found.append((band, mhz, power, snr))
                self.on_event(ts=now, band=band, mhz=mhz, power=power, snr=snr)
            self.store.commit()
        return found, skips
