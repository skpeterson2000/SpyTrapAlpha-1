"""BLE sensor: log every advertisement, classify known trackers, store sightings.

Uses an active scan so we get scan-response data (names, more service UUIDs).
Each callback is one advertisement; we persist all of them (recurrence is a
query over the full log) but only print a live line when a device first appears
in this run or its tracker classification is interesting.
"""

import asyncio
import time

from bleak import BleakScanner

from . import signatures
from .db import Store


class BLESensor:
    def __init__(self, store: Store, session, on_event=None,
                 commit_interval: float = 5.0):
        self.store = store
        # `session` may be a str (fixed) or a callable returning the current
        # session label (used for time-bucketed continuous logging).
        self.session = session
        self.on_event = on_event or (lambda *a, **k: None)
        self.commit_interval = commit_interval
        self._seen = {}            # address -> last-seen monotonic time
        self._last_commit = 0.0
        self.count = 0

    def _callback(self, device, adv):
        now = time.time()
        session = self.session() if callable(self.session) else self.session
        tracker = signatures.classify(
            adv.manufacturer_data, adv.service_uuids, adv.service_data
        )
        self.store.add_sighting(
            radio="ble",
            address=device.address,
            name=adv.local_name,
            rssi=adv.rssi,
            tx_power=adv.tx_power,
            service_uuids=adv.service_uuids,
            manufacturer_data=adv.manufacturer_data,
            service_data=adv.service_data,
            tracker_type=tracker,
            session=session,
            ts=now,
        )
        self.count += 1

        first_seen = device.address not in self._seen
        self._seen[device.address] = now
        if first_seen or tracker in signatures.TRACKER_LABELS:
            self.on_event(
                ts=now, address=device.address, name=adv.local_name,
                rssi=adv.rssi, tracker=tracker, first_seen=first_seen,
            )

        if now - self._last_commit >= self.commit_interval:
            self.store.commit()
            self._last_commit = now

    async def run(self, duration: float = 0.0):
        """Scan for `duration` seconds (0 = until cancelled)."""
        scanner = BleakScanner(detection_callback=self._callback,
                               scanning_mode="active")
        await scanner.start()
        try:
            if duration > 0:
                await asyncio.sleep(duration)
            else:
                while True:
                    await asyncio.sleep(3600)
        finally:
            await scanner.stop()
            self.store.commit()
