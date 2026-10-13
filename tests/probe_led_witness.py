#!/usr/bin/env python3
"""Counterexample: identical REAL tag-4 telemetry, different LED SPI payload.

Fresh boot, actual np_main_service and actual note-indicator/LED sender.
This proves a diagnostic blind spot, not the physical freeze seen on the unit.
"""
import hashlib
import struct
import sys
from pathlib import Path
import sysemu
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import diagmidi

DIAG = 0x2000E400


def main(path):
    original = Path(path).read_bytes()
    print('image', path, hashlib.sha256(original).hexdigest(), flush=True)
    service = struct.unpack_from('<I', original, 0x752A4)[0]
    m = sysemu.Mono(path).boot()
    uc = m.uc
    def put(a, v): uc.mem_write(a, struct.pack('<I', v))
    def get(a): return struct.unpack('<I', uc.mem_read(a, 4))[0]
    system = m.call_preserving(0x08109614)
    led = m.call_preserving(0x0810977C, system, 4)
    midi = m.call_preserving(0x0811A6A4)
    put(DIAG + 0x90, 0)  # no unrelated deferred USB
    put(0xE0001004, 400000)
    ccs = []
    def tap(uc, access, addr, size, value, data):
        if (addr - midi - 0x408) % 4 == 3:
            b = bytes(uc.mem_read(addr - 3, 3))
            assert b[1] == 0xBF
            ccs.append((b[2], value & 127))
    uc.hook_add(sysemu.UC_HOOK_MEM_WRITE, tap,
                begin=midi + 0x408, end=midi + 0x807)

    def panel_record():
        ccs.clear()
        put(DIAG + 0x130, 4)   # TL_LEDS, exact phase the live sender uses
        put(DIAG + 0x134, 0)
        for _ in range(20):
            # Simulate a fully drained output queue each service. We test
            # encoded content, not UART/USB backpressure here.
            put(midi + 0x808, get(midi + 0x80C))
            m.call_preserving(service)
            if get(DIAG + 0x130) == 0:
                break
        assert len(ccs) == 91 and ccs[0] == (119, 4), ccs
        return tuple(ccs)

    records, packets, submitted = [], [], []
    for pulse in (False, True):
        for i in range(70):
            uc.mem_write(led + 0x94 + 20 * i, bytes(20))
        cell = led + 0x94 + 20 * 51
        uc.mem_write(cell, bytes([2, 0, 0, 1]) + bytes(8) + struct.pack('<f', 2) + bytes(4))
        uc.mem_write(led + 0x60C, bytes([50]))
        uc.mem_write(led + 0x90, b'\x80')
        put(DIAG + 0xD48, 0)  # previous SPI transfer long finished
        if pulse:
            # Same note indicator used by the real UI caller 0x0814A53C;
            # note 60 -> keyboard LED 51, 750 ms pulse. No LED state mock.
            m.call_preserving(0x0814F974, sysemu.UI, 60)
            assert struct.unpack('<HH', uc.mem_read(cell + 4, 4)) == (750, 750)
        records.append(panel_record())
        old = len(m.led_bytes)
        m.call_preserving(0x08109FD0, led)
        packets.append(bytes(m.led_bytes[old:]))
        print('note pulse=%s actual TXDR=%s base=%d remaining=%d' % (
            pulse, packets[-1].hex(), uc.mem_read(cell, 1)[0],
            struct.unpack('<H', uc.mem_read(cell + 4, 2))[0]), flush=True)
        if get(0x4001340C) == 0x07420060:
            ccs.clear()
            put(DIAG + 0x130, 0); put(DIAG + 0x134, 0)
            put(DIAG + 0x138, 1); put(DIAG + 0x13C, 1)
            put(0x20000174, 20000)
            m.call_preserving(service)  # actual snapshot scheduling
            for _ in range(100):
                put(midi + 0x808, get(midi + 0x80C))
                m.call_preserving(service)
                if get(DIAG + 0x130) == 0: break
            rec = diagmidi.records(ccs)
            assert len(rec) == 1 and rec[0][0] == 5 and diagmidi.p6_valid(rec[0][1])
            w = rec[0][1][52 + 51 // 4]
            submitted.append((w >> ((51 % 4) * 8)) & 255)
            assert submitted[-1] == packets[-1][2]
    assert records[0] == records[1], 'tag-4 unexpectedly sees pulse state'
    assert len(packets[0]) == len(packets[1]) == 3
    assert packets[0][:2] == packets[1][:2]
    assert packets[0][2] != packets[1][2]
    if submitted:
        assert len(submitted) == 2 and submitted[0] != submitted[1]
        print('P6 CRC-verified MIDI records see actual pulse-dependent TX brightness:', submitted)
    assert Path(path).read_bytes() == original
    print('Identical 91-CC tag-4 records; different physical-command brightness. '
          'Static tag 4 does NOT establish a stopped renderer.')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'build/firmware-patched.bin')
