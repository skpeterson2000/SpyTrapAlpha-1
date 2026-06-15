"""Known BLE signatures for commercial/covert trackers.

Classification is best-effort and intentionally data-driven: add a row here and
the scanner picks it up. Matching is done on the stable parts of an
advertisement (assigned company IDs and service UUIDs) because trackers
randomize their MAC, so the MAC itself is useless for identity.

References: Bluetooth SIG assigned numbers (company IDs, 16-bit UUIDs) plus
published findings on the Apple Find My / Tile / Samsung SmartTag protocols.
These are the populations that matter for anti-stalking: small, cheap, mass
-market location beacons someone can slip into a bag or car.
"""

# 16-bit company identifiers (manufacturer_data keys).
APPLE = 0x004C
SAMSUNG = 0x0075

# 16-bit service UUIDs, normalized to the full 128-bit string BlueZ reports.
def _uuid16(v: int) -> str:
    return f"0000{v:04x}-0000-1000-8000-00805f9b34fb"

TILE_UUID = _uuid16(0xFEED)        # Tile, Inc.
TILE_UUID_ALT = _uuid16(0xFEEC)
SAMSUNG_TAG_UUID = _uuid16(0xFD5A)  # Samsung SmartThings / SmartTag
GOOGLE_FMDN_UUID = _uuid16(0xFEAA)  # Eddystone / used by Google Find My Device network beacons
EXPOSURE_UUID = _uuid16(0xFD6F)     # Exposure notifications — benign, flagged to suppress noise


def classify(manufacturer_data: dict, service_uuids: list, service_data: dict) -> str | None:
    """Return a tracker-type label, or None if nothing known matches.

    Order matters: most specific / highest-confidence checks first.
    """
    uuids = {u.lower() for u in (service_uuids or [])}
    uuids |= {u.lower() for u in (service_data or {}).keys()}

    if TILE_UUID in uuids or TILE_UUID_ALT in uuids:
        return "tile"
    if SAMSUNG_TAG_UUID in uuids:
        return "samsung_smarttag"

    # Apple Find My: Apple company ID with the offline-finding payload type.
    # 0x12 = "separated" (the dangerous case: a tag away from its owner,
    # i.e. potentially traveling with a victim); 0x07 = nearby-owner pairing.
    apple = manufacturer_data.get(APPLE)
    if apple:
        ptype = apple[0] if len(apple) else None
        if ptype == 0x12:
            return "apple_findmy_separated"
        if ptype == 0x07:
            return "apple_findmy_nearby"
        return "apple_device"

    # Google Find My Device network beacon (incl. some third-party tags).
    if GOOGLE_FMDN_UUID in uuids:
        return "google_fmdn"

    return None


# Labels we treat as genuine tracker candidates for scoring (vs. benign/general).
TRACKER_LABELS = {
    "tile",
    "samsung_smarttag",
    "apple_findmy_separated",
    "apple_findmy_nearby",
    "google_fmdn",
}
