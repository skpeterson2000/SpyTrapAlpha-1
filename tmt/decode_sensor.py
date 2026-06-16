"""Decode UNENCRYPTED ISM device frames via rtl_433.

This decodes only clear, publicly-broadcast device telemetry (TPMS, weather
stations, remotes, sensors, asset tags) — never encrypted, voice, or cellular
traffic. Each decoded frame carries a device id that is broadcast in the open;
a recurring id (e.g. a tire-pressure sensor that keeps appearing as you move) is
exactly the "specific thing following me" signal the scorer is built to surface.

Gating lives in decode.py (must be enabled AND authorized). This module just
runs the decoder and stores frames.
"""

import json
import subprocess
import time

# Fields rtl_433 uses for a device's stable identifier, in preference order.
_ID_FIELDS = ("id", "address", "sensor_id", "sn", "serial", "unit", "channel")


def frame_identity(frame):
    model = str(frame.get("model", "unknown"))
    for f in _ID_FIELDS:
        if f in frame and frame[f] not in (None, ""):
            return model, str(frame[f]), f"{model}:{frame[f]}"
    return model, "", model


class DecodeSensor:
    def __init__(self, store, session, device=None, frequencies=None,
                 hop_seconds=30, on_event=None):
        self.store = store
        self.session = session
        self.device = device
        self.frequencies = frequencies or ["433.92M"]
        self.hop_seconds = hop_seconds
        self.on_event = on_event or (lambda *a, **k: None)
        self.count = 0

    def _cmd(self, duration):
        cmd = ["rtl_433", "-F", "json", "-M", "level"]
        if self.device:
            cmd += ["-d", f":{self.device}"]      # select dongle by serial
        for f in self.frequencies:
            cmd += ["-f", f]
        if len(self.frequencies) > 1:
            cmd += ["-H", str(self.hop_seconds)]
        if duration and duration > 0:
            cmd += ["-T", str(int(duration))]     # rtl_433 self-stops
        return cmd

    def run(self, duration=0):
        proc = subprocess.Popen(self._cmd(duration), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                # rtl_433 also emits status/metadata lines (center_frequency,
                # hop_times, …) with no "model"; only real device decodes have
                # a model. Skip everything else.
                if "model" not in frame:
                    continue
                self._handle(frame)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def _handle(self, frame):
        model, dev_id, identity = frame_identity(frame)
        session = self.session() if callable(self.session) else self.session
        freq = frame.get("freq")              # MHz when frequency-hopping
        rssi = frame.get("rssi")
        ts = time.time()
        self.store.add_decode(ts=ts, model=model, dev_id=dev_id,
                              identity=identity, freq_mhz=freq, rssi=rssi,
                              frame=frame, session=session)
        self.count += 1
        self.on_event(ts=ts, model=model, identity=identity, freq=freq,
                      rssi=rssi, frame=frame)
