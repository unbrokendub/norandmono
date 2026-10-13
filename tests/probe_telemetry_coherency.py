#!/usr/bin/env python3
"""Whether a live counter can tear across MIDI telemetry pump calls.

Runs the built np_main_service and actual MIDI ring writer. Starts at a
reachable partially transmitted word, then changes one live counter at a
carry boundary. A word is latched whole when its first fragment goes out, so
the tail must reconstruct the latched head, never a mixture of the two live
values. This is a codec diagnostic, not a hardware freeze test.
"""
import hashlib
import struct
import sys
from pathlib import Path
import sysemu

DIAG = 0x2000E400
MAIN_COUNT = DIAG + 0xB8 + 4 * 24
TL_POS = DIAG + 0x134
TL_WORD = DIAG + 0x164


def u32(uc, address):
    return struct.unpack('<I', uc.mem_read(address, 4))[0]


def main(path):
    data = Path(path).read_bytes()
    print('image', path, 'SHA256', hashlib.sha256(data).hexdigest(), flush=True)
    assert data[0x75200:0x75204] == b'NPAT'
    service = struct.unpack_from('<I', data, 0x752A4)[0]
    m = sysemu.Mono(path).boot()
    m.settle(10)
    midi = m.call_preserving(0x0811A6A4)
    uc = m.uc
    m.run_unmasked()
    def put(a, v): uc.mem_write(a, struct.pack('<I', v))
    put(DIAG + 0x90, 0)                     # no unrelated deferred USB
    put(DIAG + 0xA0, 0)                     # no synthetic DWT period
    put(0xE0001004, 0)
    ccs = []
    def tap(uc, access, a, size, value, _):
        if (a - midi - 0x408) % 4 == 3:
            prefix = bytes(uc.mem_read(a - 3, 3))
            assert prefix[1] == 0xBF
            ccs.append((prefix[2], value & 0x7F))
    uc.hook_add(sysemu.UC_HOOK_MEM_WRITE, tap, begin=midi + 0x408, end=midi + 0x807)
    for before, after in ((0x3FFF, 0x3FFF), (0x4000, 0x4000), (0x3FFF, 0x4000)):
        # Enough room: no result can be explained by MIDI queue overflow.
        put(midi + 0x808, 0)
        put(midi + 0x80C, 0)
        put(DIAG + 0x130, 3)                # heartbeat phase
        put(DIAG + 0x134, 0x80000000 | 9 * 8 | 2)
        ccs.clear()
        put(MAIN_COUNT, before)
        m.call_preserving(service)
        assert u32(uc, TL_POS) == (0x80000000 | 10 * 8 | 3)
        latched = u32(uc, TL_WORD)
        put(MAIN_COUNT, after)
        m.call_preserving(service)
        fragments = ccs[3:8]
        assert [cc for cc, value in fragments] == list(range(102, 107)), ccs
        # The first fragment carries the word's MSBs in its low four bits and
        # the word index mod 8 in bits 4..6 (the receiver's resync key).
        decoded = 0
        for cc, value in fragments:
            decoded = (decoded << 7) | (value & 0x0F if cc == 102 else value)
        assert (fragments[0][1] >> 4) & 7 == 10 % 8, 'word index stamp'
        # The service profiles itself, so the live counter moves on its own;
        # what must hold is that the five fragments reconstruct the word that
        # was latched when the first of them went out.
        assert decoded == latched, (decoded, latched)
        print('live counter: %d -> %d; latched %d; emitted %d; fragments=%r' % (
            before, after, latched, decoded, fragments), flush=True)
    assert Path(path).read_bytes() == data
    print('word coherency holds: a split word\'s tail comes from its latched head')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'build/firmware-patched.bin')
