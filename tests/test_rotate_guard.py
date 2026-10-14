#!/usr/bin/env python3
"""RG (p60): a pattern rotate stays inside the step buckets.

FUNC+UP/DOWN rotates the selected pattern.  The RotatePatternData handler
(0x08129410) hands its range to 0x081583AC through 0x08145EC0; the range is
built from the pattern's length - 1, so for a pattern of length 0 it is
0..255 against a store of 128 buckets, and the rotate pass (0x0815888C)
swaps buckets 255..0 through the memory past the store until a list node
faults (found by a UI fuzz in mod-note mode: PATTERN + PAGE D + SEQ 1, then
FUNC+UP).  sysemu's projects leave unused patterns at length 0.

  0. control, the stock: FUNC+UP on a 0-step pattern faults in the swap;
  1. RG: the same rotate is dropped, the step lists stay intact, it plays;
  2. a 16-step pattern still rotates: its notes move, none is lost;
  3. a reversed range sent as an event (start 20 > end 5): the pass walks
     its byte index past 0 and round -- the stock breaks the lists, RG
     drops it.

    build/venv/bin/python tests/test_rotate_guard.py
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sysemu                                        # noqa: E402
from test_modnote_ui import booted                   # noqa: E402
from unicorn import UcError                          # noqa: E402

IMAGE = 'build/firmware-patched.bin'
STOCK = 'build/firmware.bin'
MGR = 0x2000C68C
SEQ = 0x2000CFF4
ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print('  %-72s ok' % label)
    else:
        fail += 1
        print('  %-72s FAIL got %r want %r' % (label, got, want))


class Panel:
    def __init__(self, image):
        self.m = booted(image)
        self.uc = self.m.uc

    def u32(self, a):
        return struct.unpack('<I', bytes(self.uc.mem_read(a, 4)))[0]

    def select(self, p):
        m = self.m
        m.tap(24); m.settle(30); m.tap(20 + p // 16); m.settle(30)
        m.tap(4 + p % 16); m.settle(30); m.tap(24); m.settle(80)
        return self.uc.mem_read(MGR + 0x183, 1)[0], self.uc.mem_read(MGR + 0x18D, 1)[0]

    def rotate(self, key):
        m = self.m
        m.press(26); m.settle(20); m.tap(key, hold_ms=30, gap_ms=20); m.release(26); m.settle(60)

    def buckets(self):
        """{bucket: nodes} of track 0's store, or a string naming the damage"""
        out = {}
        for b in range(128):
            h = SEQ + 0x8C + b * 12
            node, prev, n = self.u32(h), h, 0
            while node != h:
                if not (0x20000000 <= node < 0x20020000 or 0x24000000 <= node < 0x24080000
                        or 0x30000000 <= node < 0x30050000):
                    return 'bucket %d: node %08x' % (b, node)
                if self.u32(node + 4) != prev:
                    return 'bucket %d: prev link' % b
                prev, node, n = node, self.u32(node), n + 1
                if n > 500:
                    return 'bucket %d: loop' % b
            if n:
                out[b] = n
        return out


def stock_control():
    print('0. control: the stock, FUNC+UP on a 0-step pattern')
    p = Panel(STOCK)
    check('pattern 6 selected, 0 steps', p.select(5), (5, 0))
    try:
        p.rotate(28)
        p.m.settle(200)
        result = p.buckets()
        result = 'no fault' if isinstance(result, dict) else result
    except UcError as e:
        result = 'fault: %s' % e
    check('the rotate faults or breaks a list: %s' % result, result != 'no fault', True)


def guarded():
    print('1. RG: FUNC+UP/DOWN on a 0-step pattern')
    p = Panel(IMAGE)
    check('pattern 6 selected, 0 steps', p.select(5), (5, 0))
    try:
        for key in (28, 29, 28, 29, 29):
            p.rotate(key)
        p.m.tap(27); p.m.settle(400); p.m.tap(27); p.m.settle(30)
        result = p.buckets()
    except UcError as e:
        result = 'fault: %s' % e
    check('no fault, every list intact: %s' % (result if not isinstance(result, dict) else 'ok'),
          isinstance(result, dict), True)
    check('no Error_Handler: %s' % p.m.errors, p.m.errors, [])


def still_rotates():
    print('2. a 16-step pattern still rotates')
    p = Panel(IMAGE)
    m = p.m
    check('pattern 1 selected, 16 steps', p.select(0), (0, 16))
    for step in (1, 5):
        m.tap(36); m.settle(15); m.tap(3 + step); m.settle(15)
    before = p.buckets()
    p.rotate(28)
    m.settle(200)
    after = p.buckets()
    check('notes moved: %s -> %s' % (before, after),
          isinstance(after, dict) and after != before and sum(after.values()) == sum(before.values()), True)


ROTATE_VT = 0x0816498C              # ProjectEvent<RotatePatternData>


def reversed_range():
    print('3. a reversed range (start 20, end 5), as an event')
    for label, image, want_broken in (('stock', STOCK, True), ('RG', IMAGE, False)):
        p = Panel(image)
        m = p.m
        check('%s: pattern 1, 16 steps' % label, p.select(0), (0, 16))
        for step in (1, 5):
            m.tap(36); m.settle(15); m.tap(3 + step); m.settle(15)
        before = p.buckets()
        try:
            # tag, pattern, start, end, direction (0x08129410 reads +6..+9)
            m.submit_event(ROTATE_VT, struct.pack('<HBBBB', 0, 0, 20, 5, 0))
            m.settle(300)
            after = p.buckets()
            broken = not isinstance(after, dict) or sum(after.values()) != sum(before.values())
            result = after if isinstance(after, dict) else after
        except UcError as e:
            broken, result = True, 'fault: %s' % e
        check('%s: lists %s (%s)' % (label, 'broken' if broken else 'intact', result), broken, want_broken)


def main():
    stock_control()
    guarded()
    still_rotates()
    reversed_range()
    print('%s -- a pattern rotate stays inside the step buckets (%d checks)'
          % ('PASS' if fail == 0 else 'FAIL', ok + fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
