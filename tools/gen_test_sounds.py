#!/usr/bin/env python3
"""Generate placeholder alert tones so the audio channel is testable.

Replace these with your own files and update config['audio']['sounds'].
HIGH = urgent triple beep (higher pitch); MED = double beep; default = single.
Pure stdlib (wave + math) — no numpy.
"""

import math
import struct
import wave
from pathlib import Path

SOUNDS = Path(__file__).resolve().parent.parent / "sounds"
RATE = 44100


def tone(freq, ms, vol=0.6):
    n = int(RATE * ms / 1000)
    out = bytearray()
    for i in range(n):
        # simple envelope to avoid clicks
        env = min(1.0, i / 400, (n - i) / 400)
        s = vol * env * math.sin(2 * math.pi * freq * i / RATE)
        out += struct.pack("<h", int(s * 32767))
    return bytes(out)


def silence(ms):
    return b"\x00\x00" * int(RATE * ms / 1000)


def write(name, data):
    SOUNDS.mkdir(exist_ok=True)
    path = SOUNDS / name
    with wave.open(str(path), "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(data)
    print("wrote", path)


def main():
    write("alert-high.wav",
          tone(1320, 120) + silence(60) + tone(1320, 120) + silence(60)
          + tone(1320, 200))
    write("alert-med.wav", tone(880, 150) + silence(80) + tone(880, 220))
    write("alert.wav", tone(660, 250))


if __name__ == "__main__":
    main()
