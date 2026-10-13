#!/usr/bin/env python3
"""The MIDI telemetry on the whole-firmware emulator: the SysTick sampler
fills the retained PC ring with plausible addresses, the boot record and
ring are sent after a simulated watchdog reset, and the heartbeat decodes.

    build/venv/bin/python tests/test_telemetry.py [build/firmware-patched.bin]
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / 'tools'))
import sysemu                     # noqa: E402
import test_modnote_ui as T       # noqa: E402
import diagmidi                   # noqa: E402

DIAG = 0x2000E400
D_RSR, D_PCIDX, D_PCHIST, D_PCRING = 0x420, 0x424, 0x428, 0x540
D_TL_PHASE, D_TL_SEQ = 0x130, 0x13C
UWTICK = 0x20000174
IWDG_RESET = 1 << 26


def u32(uc, a):
    return struct.unpack('<I', bytes(uc.mem_read(a, 4)))[0]


class RingTap:
    """Every channel-16 control change written into the stock 4-byte output
    ring, captured as it is written (the sender can lap the 256-entry ring
    many times inside one harness slice, so reading it back is not enough)."""
    def __init__(self, m, midi):
        self.ccs = []
        ring = midi + 0x408
        def w(uc, access, addr, size, value, data):
            if (addr - ring) % 4 == 3:            # the value byte completes a slot
                b = bytes(uc.mem_read(addr - 3, 3)) + bytes([value & 0xFF])
                if b[1] == 0xBF:
                    self.ccs.append((b[2], b[3]))
        m.uc.hook_add(sysemu.UC_HOOK_MEM_WRITE, w, begin=ring, end=ring + 0x400 - 1)


def main(image):
    if not Path(image).read_bytes()[0x75205] & 0x08:          # header +5, FLAG_TL
        print('SKIP -- this image was built without --telemetry')
        return 0
    # Peripheral configuration is not present in the old RAM-only caches.
    m = sysemu.Mono(image).boot()
    m.settle(50)
    m.factory_defaults()
    uc = m.uc
    midi = m.call_preserving(0x0811A6A4)

    # 1. the sampler: after 100 ms there are ~400 new samples, thread-mode ones
    #    inside the main loop's functions, audio ones inside the CV engine
    idx0 = u32(uc, DIAG + D_PCIDX)
    m.settle(100, audio=1)
    idx1 = u32(uc, DIAG + D_PCIDX)
    assert 350 <= idx1 - idx0 <= 450, 'SysTick samples in 100 ms: %d' % (idx1 - idx0)
    ring = [u32(uc, DIAG + D_PCRING + i * 4) for i in range(256)]
    ctx = {}
    for w in ring:
        ctx[w >> 24] = ctx.get(w >> 24, 0) + 1
    assert all(0x08100000 <= (0x08000000 | (w & 0xFFFFFF)) < 0x08180000 or (w >> 16) == 0xB007
               for w in ring), 'ring holds a non-flash PC'
    assert ctx.get(0, 0) > 0, 'no thread-mode samples: %r' % ctx
    hist = struct.unpack('<4I', bytes(uc.mem_read(DIAG + D_PCHIST, 16)))
    assert hist[0] + hist[1] + hist[3] >= idx1 - 1, 'histogram %r vs samples %d' % (hist, idx1)
    print('sampler ok: %d samples/100 ms, contexts %r' % (idx1 - idx0, ctx))

    # 2. Legacy format compatibility, explicitly requested. Normal P6
    # scheduling uses tag 5 (tested separately in test_telemetry_p6.py).
    tap = RingTap(m, midi)
    uc.mem_write(UWTICK, struct.pack('<I', 20000))
    uc.mem_write(DIAG + 0x138, struct.pack('<I', 19000))    # next heartbeat due
    uc.mem_write(DIAG + D_TL_PHASE, struct.pack('<I', 3))
    uc.mem_write(DIAG + 0x134, bytes(4))
    for _ in range(600):                      # the sender paces itself: 2 CCs per ms
        uc.mem_write(UWTICK, struct.pack('<I', u32(uc, UWTICK) + 1))
        m.settle(5, audio=1, transport=False)
        if u32(uc, DIAG + D_TL_PHASE) == 0 and tap.ccs:
            break
    recs = diagmidi.records(tap.ccs)
    beats = [w for t, w in recs if t == diagmidi.HEARTBEAT]
    assert beats, 'no heartbeat: %r' % [(t, len(w)) for t, w in recs]
    words = beats[-1]
    assert len(words) == 63, 'heartbeat words: %d' % len(words)
    assert words[0] == 0x4E50544C ^ 1 and words[2] >= 20000, words[:3]
    assert words[17] & 0xFF == uc.mem_read(sysemu.UI + 0x84, 1)[0]
    assert (words[17] >> 16) & 0xFF == 1, 'view byte %#x' % words[17]
    # the LED transport: frames keep being started and none were held back
    assert words[16] == 0, 'LED frames delayed: %d' % words[16]
    assert words[24] > 0, 'no LED frames started'
    print('heartbeat ok: seq %d uptime %d ms, ui mode %d, %d CCs' % (
        words[1], words[2], words[17] & 0xFF, len(tap.ccs)))

    # 2b. a lossy link must not shift the surviving fields onto other names:
    #     drop the middle CC of words 5, 6 and 11 and re-decode.  Each loss
    #     costs exactly that word and nothing else.
    start = tap.ccs.index((diagmidi.TAG, diagmidi.HEARTBEAT))
    end = next((i for i in range(start + 1, len(tap.ccs))
                if tap.ccs[i] == (diagmidi.TAG, diagmidi.LEDS)), len(tap.ccs))
    hb = tap.ccs[start:end]
    drop = {5: None, 6: None, 11: None}
    lossy, wi, seen = [], -1, 0
    for n, v in hb:
        if n == diagmidi.DATA0:
            wi += 1
        if wi in drop and n == diagmidi.DATA0 + 2:
            seen += 1
            continue                          # the byte the wire ate
        lossy.append((n, v))
    assert seen == len(drop), 'test dropped %d CCs' % seen
    w2 = [w for t, w in diagmidi.records(lossy) if t == diagmidi.HEARTBEAT][-1]
    assert len(w2) == 63, 'resynced words: %d' % len(w2)
    for i in range(63):
        want = None if i in drop else words[i]
        assert w2[i] == want, 'word %d: %r, expected %r' % (i, w2[i], want)
    print('resync ok: 3 lost words marked, the other 60 still on their fields')

    # 2c. the UI event queue depth and the panel image
    F = diagmidi.HB_FIELDS
    depth = words[F.index('ui_queue_depth')]
    geom = words[F.index('ui_deque_map')]
    assert depth != 0xFFFFFFFF, 'deque iterators outside the map'
    assert depth < 128, 'queue depth %d' % depth
    assert (geom >> 8) & 0xFF != 0xFF and (geom >> 16) & 0xFF != 0xFF, \
        'a cursor is outside the block map: %#x' % geom
    assert words[F.index('scanner_calls')] > 0, 'the key/pot scanner never ran'
    leds = [w for t, w in recs if t == diagmidi.LEDS]
    assert leds and len(leds[-1]) == 18, 'LED image record: %r' % [
        (t, len(w)) for t, w in recs]
    image = []
    for w in leds[-1]:
        image.extend((w >> (8 * n)) & 0xFF for n in range(4))
    image = image[:70]
    assert all(v <= 63 for v in image), 'brightness out of range'
    print('ui queue depth %d, map %d blocks (start %d finish %d); '
          'LED image sum %d, %d lit'
          % (depth, geom & 0xFF, (geom >> 8) & 0xFF, (geom >> 16) & 0xFF,
             sum(image), sum(1 for v in image if v)))

    # 3. after a watchdog reset the boot record and the whole ring go out first:
    #    emulate the state np_early leaves behind on a warm boot
    tap.ccs.clear()
    uc.mem_write(DIAG + D_RSR, struct.pack('<I', IWDG_RESET))
    uc.mem_write(DIAG + D_TL_SEQ, struct.pack('<I', 0))
    uc.mem_write(DIAG + D_TL_PHASE, struct.pack('<I', 0))
    uc.mem_write(DIAG + 0x138, struct.pack('<I', 0))
    uc.mem_write(UWTICK, struct.pack('<I', 0))
    for _ in range(1500):                     # boot record + ring = 1421 CCs at 2 per ms
        uc.mem_write(UWTICK, struct.pack('<I', u32(uc, UWTICK) + 40))
        m.settle(5, audio=1, transport=False)
        if len(tap.ccs) >= 2 + 5 * (28 + 256) and u32(uc, DIAG + D_TL_PHASE) == 0:
            break
    recs = diagmidi.records(tap.ccs)
    tags = [t for t, _ in recs]
    assert tags[:2] == [diagmidi.BOOT, diagmidi.RING], 'records after warm reset: %r' % tags
    boot, ringw = recs[0][1], recs[1][1]
    assert len(boot) == 28 and boot[0] == 0x4E50544C and boot[2] == IWDG_RESET, boot[:3]
    assert len(ringw) == 256, len(ringw)
    # the frozen ring is what np_early copied: here nothing was frozen yet,
    # so it is the cold-boot zero fill plus whatever the live ring held when
    # a previous early call ran -- only its length and framing matter here
    print('boot record + ring ok: %d words, %d CCs' % (len(boot) + len(ringw), len(tap.ccs)))
    print('telemetry OK')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'build/firmware-patched.bin')
