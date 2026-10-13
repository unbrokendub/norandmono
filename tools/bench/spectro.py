#!/usr/bin/env python3
"""Spectrograms of the bench's WAVs, as PNGs (no plotting library needed).

    spectro.py build/bench/wav/p43_kick_0.wav [...]   -> build/bench/png/*.png

Log frequency 30 Hz .. 16 kHz up the image, time across (0..1.2 s), level
in dB from -80 to 0 as brightness; a thin waveform envelope strip on top.
"""
import struct
import sys
import zlib
from pathlib import Path

import numpy as np

FS = 32000
W, H, STRIP = 600, 300, 40


def png(path, img):
    h, w = img.shape
    raw = b''.join(b'\x00' + img[y].astype(np.uint8).tobytes() for y in range(h))

    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 0, 0, 0, 0)) +
                     chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def spectro(wav, out):
    x = np.fromfile(wav, dtype='<i2', offset=44).astype(float) / 32768.0
    span = int(1.2 * FS)
    x = np.pad(x[:span], (0, max(0, span - len(x))))
    n = 2048
    hop = span // W
    win = np.hanning(n)
    freqs = np.fft.rfftfreq(n, 1 / FS)
    rows = np.geomspace(30, 16000, H)
    idx = np.clip(np.searchsorted(freqs, rows), 1, len(freqs) - 1)
    img = np.zeros((H + STRIP, W))
    for c in range(W):
        seg = x[c * hop:c * hop + n]
        seg = np.pad(seg, (0, n - len(seg)))
        mag = np.abs(np.fft.rfft(seg * win)) / (n / 4)
        db = 20 * np.log10(mag[idx] + 1e-9)
        img[STRIP:, c] = np.clip((db + 80) / 80, 0, 1)[::-1] * 255
        level = np.sqrt(np.mean(x[c * hop:c * hop + hop] ** 2)) if c * hop < len(x) else 0
        bar = int(np.clip((20 * np.log10(level + 1e-9) + 60) / 60, 0, 1) * (STRIP - 2))
        img[STRIP - 1 - bar:STRIP - 1, c] = 200
    # octave guide lines (C notes) on the left edge
    for f in (65.4, 261.6, 1046.5, 4186.0):
        y = STRIP + H - 1 - int(np.searchsorted(rows, f))
        img[y, :6] = 255
    png(out, img)


def main():
    out = Path('build/bench/png')
    out.mkdir(parents=True, exist_ok=True)
    for name in sys.argv[1:]:
        p = Path(name)
        spectro(p, out / (p.stem + '.png'))
        print(out / (p.stem + '.png'))


if __name__ == '__main__':
    main()
