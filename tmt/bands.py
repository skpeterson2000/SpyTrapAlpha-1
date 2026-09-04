"""Frequency -> band-plan classification, incl. LoRa / Meshtastic channel grids.

The SDR sweep (sdr_sensor) only sees *energy* at a frequency — it can't say what
the emitter is. This module adds cheap, offline intelligence on top of a bare
frequency: which ISM band it sits in, and — the part that matters for mesh
discovery — whether it lands on a Meshtastic LoRa channel slot.

Why this is the realistic mesh-detection path here: Meshtastic is LoRa (CSS
chirp). rtl_433 and a plain RTL-SDR cannot demodulate LoRa, so we can never read
node ids/names without a LoRa radio. But Meshtastic transmits on a *deterministic
frequency grid* derived from the region + modem preset. A recurring narrowband
peak that lands on that grid AND that rtl_433 fails to decode as OOK/FSK is a
strong LoRa/Meshtastic candidate — which is exactly what we can detect.

Frequency model (Meshtastic firmware RadioInterface): for a region with
[freq_start, freq_end] MHz and a preset bandwidth bw (MHz),
    num_slots   = floor((freq_end - freq_start) / bw)
    slot_center = freq_start + bw/2 + slot * bw      # slot in 0..num_slots-1
The default channel ("LongFast", US) lands on slot 19 -> 906.875 MHz.
"""

# ISM / LoRa regions we care about (mobile US rig, but EU kept for travel).
# name -> (low_mhz, high_mhz)
ISM_BANDS = [
    ("ISM 315", 314.8, 315.2),
    ("ISM 433", 433.05, 434.79),
    ("LoRa EU868", 863.0, 870.0),
    ("ISM/LoRa 900 (US)", 902.0, 928.0),
]

# Meshtastic region channel plans we check. Each: region -> (start, end, presets)
# presets: name -> bandwidth in kHz. We only grid-check the bandwidths Meshtastic
# actually uses; 62.5 kHz (VeryLongSlow, deprecated) is omitted — its grid is
# finer than a typical sweep step, so it would only manufacture false matches.
MESH_REGIONS = {
    "US": (902.0, 928.0, {"ShortTurbo": 500, "Short/Medium/LongFast": 250,
                          "LongModerate/Slow": 125}),
    "EU868": (869.4, 869.65, {"All presets": 250}),
}


def _ism_band(mhz):
    for name, lo, hi in ISM_BANDS:
        if lo <= mhz <= hi:
            return name
    return None


def meshtastic_slots(mhz, tol_khz=15.0):
    """Return Meshtastic grid slots this frequency could be, best match first.

    tol_khz defaults to ~15 kHz to absorb the SDR sweep's bin quantisation
    (sdr_sensor steps the 900 band at 25 kHz). Each hit: dict(region, preset,
    bandwidth_khz, slot, slot_mhz, offset_khz).
    """
    hits = []
    for region, (start, end, presets) in MESH_REGIONS.items():
        for preset, bw_khz in presets.items():
            bw = bw_khz / 1000.0
            num = int((end - start) / bw)
            if num <= 0:
                continue
            # nearest slot to the measured frequency
            slot = round((mhz - start - bw / 2) / bw)
            if not (0 <= slot < num):
                continue
            center = start + bw / 2 + slot * bw
            off_khz = abs(mhz - center) * 1000.0
            if off_khz <= tol_khz:
                hits.append({
                    "region": region, "preset": preset, "bandwidth_khz": bw_khz,
                    "slot": slot, "slot_mhz": round(center, 4),
                    "offset_khz": round(off_khz, 1),
                })
    hits.sort(key=lambda h: h["offset_khz"])
    return hits


def classify(mhz, tol_khz=15.0):
    """Full band classification for a frequency in MHz.

    Returns dict: band (str|None), mesh_candidate (bool), mesh_slots (list),
    note (human one-liner). 'mesh_candidate' means it lands on a Meshtastic grid
    — NOT that it's confirmed Meshtastic (only a LoRa demod could confirm), but
    a recurring un-decodable peak here is the signal worth watching.
    """
    band = _ism_band(mhz)
    slots = meshtastic_slots(mhz, tol_khz)
    out = {"mhz": round(mhz, 4), "band": band,
           "mesh_candidate": bool(slots), "mesh_slots": slots, "note": ""}
    if slots:
        best = slots[0]
        out["note"] = (f"on Meshtastic {best['region']} grid "
                       f"(slot {best['slot']} @ {best['slot_mhz']} MHz, "
                       f"{best['preset']} {best['bandwidth_khz']}kHz, "
                       f"±{best['offset_khz']}kHz) — LoRa/Meshtastic candidate")
    elif band:
        out["note"] = f"in {band}"
    else:
        out["note"] = "outside known ISM bands"
    return out


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    for a in sys.argv[1:]:
        try:
            c = classify(float(a))
        except ValueError:
            print(f"{a}: not a number"); continue
        print(f"{c['mhz']} MHz: {c['note']}")
