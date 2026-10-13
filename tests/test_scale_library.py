#!/usr/bin/env python3
"""SL (p57): the scale library.  In the scale setting, PAGE A-D held + SEQ
1..16 loads library scale page*16+seq into that page's custom scale, by the
stock SetCustomScaleNoteData toggles; the stock built-in pick of that SEQ
key does not happen; a plain SEQ key still picks a built-in scale; hold
PAGE + a NOTE key still toggles on top; and a save carries the result.

    build/venv/bin/python tests/test_scale_library.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sysemu                       # noqa: E402
import test_modnote_random as T     # noqa: E402

LIB = json.loads((Path(__file__).parent.parent / 'docs/scale-library.json').read_text())
IDS = {int(k): v for k, v in json.loads(Path('build/event-ids.json').read_text()).items()}
UI = sysemu.UI
SCALE = T.MGR + 0x18D + 4


def main():
    r = T.Rig()
    m, uc = r.m, r.uc
    deg = lambda page: (lambda d: d[1:1 + d[0]])(list(uc.mem_read(T.MGR + 0x6C + (16 + page) * 14, 14)))
    state = lambda: (uc.mem_read(UI + 0x84, 1)[0], uc.mem_read(UI + 0xEC, 1)[0], uc.mem_read(SCALE, 1)[0])
    tap = lambda key: (m.press(key), m.settle(30), m.release(key), m.settle(40))

    def held(page, key):
        m.press(20 + page); m.settle(30)
        tap(key)
        m.release(20 + page); m.settle(40)

    # normal mode, then the scale setting (UI mode 3, setting 1): the gesture
    # that opens it is not reproduced in sysemu, so its state is set
    m.press(28); m.settle(30); m.press(29); m.settle(30); m.release(28); m.settle(30); m.release(29); m.settle(60)
    uc.mem_write(UI + 0x86, bytes([1])); uc.mem_write(UI + 0x84, bytes([3])); uc.mem_write(UI + 0xEC, bytes([1]))
    m.settle(40)

    bad = []
    for pi, page in enumerate('ABCD'):
        for seq in range(16):
            held(pi, 4 + seq)
            if deg(pi) != LIB[page][seq][1] or state() != (3, 1, 16 + pi):
                bad.append((page, seq + 1, deg(pi), state()))
    T.check('64 library scales load exactly; mode, setting and the custom pick stay', bad, [])

    # the mini keyboard shows a loaded scale at once (LED 51 + n, dim level
    # 2 for a scale tone), including when tones go away: D7 Messiaen 5,
    # then D10 a major triad
    led = lambda n: uc.mem_read(0x2000190C + 0x94 + 20 * (51 + n), 1)[0]
    lit = lambda: [n for n in range(1, 12) if led(n)]
    held(3, 4 + 6)
    first = lit()
    m.press(23); m.settle(30)
    tap(4 + 9)
    second = lit()
    m.release(23); m.settle(40)
    T.check('the keyboard shows each load while PAGE is held', (first, second), ([1, 5, 6, 7, 11], [4, 7]))

    held(1, 4)                                   # B1 minor pentatonic
    held(1, 38)                                  # + E by hand
    T.check('hold PAGE B + E toggles on top of B1', (deg(1), state()[2]), ([0, 3, 4, 5, 7, 10], 17))
    tap(6)
    T.check('a plain SEQ 3 still picks built-in scale 3', state()[2], 2)

    held(0, 4)                                   # A1 Hijaz into custom 1
    uc.mem_write(UI + 0x84, bytes([1])); m.settle(40)
    uc.mem_write(0x24000000, bytes(0x2000))
    m.press(26); m.settle(30); tap(32); m.release(26); m.settle(400)   # FUNC + SAVE
    buf = bytes(uc.mem_read(0x24000000, 0x8000))
    p, saved = 0x100, []
    while p < len(buf) and buf[p] in IDS and buf[p] != 9:
        if buf[p] == 22:
            saved.append(tuple(buf[p + 1:p + 3]))
        p += 1 + IDS[buf[p]]['payload']
    T.check('the save carries custom 1 = Hijaz', [n for c, n in saved if c == 0], [1, 4, 5, 7, 8, 10])
    seq = 0x2000CFF4
    trigs = []
    for st in range(64):
        head = seq + 0x8C + st * 12
        node, n = r.u32(head), 0
        while node != head and n < 50:
            if r.u32(r.u32(node + 12)) == 0x081642C0:
                trigs.append(st + 1)
            node, n = r.u32(node), n + 1
    T.check('library loads leave no trigs on the pattern (p59)', trigs, [])
    T.check('no emulator errors', len(m.errors), 0)
    print('%s -- the scale library (%d checks)' % ('PASS' if not T.fail else 'FAIL', T.ok + T.fail))
    return 1 if T.fail else 0


if __name__ == '__main__':
    sys.exit(main())
