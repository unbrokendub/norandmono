#!/usr/bin/env python3
"""V: the dim SEQ-key state is dimmer; the bright states are untouched.

The same panel session on the stock image and on this one, reading the
brightness the UI requests for the sixteen SEQ keys (LED driver 4, entry i at
+0x94 + 20 i, SEQ 1 = LED 25):

  * step view -- a note on step 1, a note from step 5 to step 8: the note
    steps are bright (16) in both, the long note's tail is 5 in the stock and
    the configured dim level here;
  * pattern mode ([PATTERN] = key 24) with patterns 0 and 2 holding notes:
    the current pattern is 24 in both, the other one 5 / dim.
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sysemu
from unicorn.arm_const import UC_ARM_REG_R2
from test_modnote_ui import booted

IMAGE = 'build/firmware-patched.bin'
STOCK = 'build/firmware.bin'
ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print('  %-62s ok' % label)
    else:
        fail += 1
        print('  %-62s FAIL got %r want %r' % (label, got, want))


def session(image):
    m = booted(image)
    uc = m.uc
    drv = m.call_preserving(0x0810977D, m.call_preserving(0x08109615), 4)
    seq = lambda: [uc.mem_read(drv + 0x94 + i * 20, 1)[0] for i in range(25, 41)]
    mgr = m.call_preserving(0x0811F854)

    def set_pattern(pattern):
        m.run_unmasked()
        saved = [uc.reg_read(r) for r in m.REGS]
        uc.reg_write(UC_ARM_REG_R2, 0)
        try:
            m.call(0x08125A90, mgr, pattern)
        finally:
            for r, v in zip(m.REGS, saved):
                uc.reg_write(r, v)
        m.settle(200)

    m.tap(4); m.settle(40)                                  # a note, step 1
    m.press(8); m.settle(40)                                # a note from step 5...
    m.tap(11, hold_ms=30, gap_ms=30)                        # ...to step 8
    m.release(8); m.settle(100)
    steps = seq()
    set_pattern(2); m.tap(6); m.settle(60)                  # pattern 2 gets a note
    set_pattern(0)
    m.tap(24, hold_ms=30, gap_ms=100)                       # PATTERN
    patterns = seq()
    return steps, patterns


def main():
    dim = Path(IMAGE).read_bytes()[0x0814F05E - 0x08100000]
    stock_steps, stock_patterns = session(STOCK)
    steps, patterns = session(IMAGE)
    print('  stock: steps %s  patterns %s' % (stock_steps, stock_patterns))
    print('  here:  steps %s  patterns %s' % (steps, patterns))
    check('stock: note steps 16, long-note tail 5',
          (stock_steps[0], stock_steps[4], stock_steps[5:8]), (16, 16, [5, 5, 5]))
    check('here: note steps still 16', (steps[0], steps[4]), (16, 16))
    check('here: the long-note tail at the dim level (%d)' % dim, steps[5:8], [dim] * 3)
    check('here: empty steps dark', [steps[i] for i in (1, 2, 3, 8, 15)], [0] * 5)
    check('stock: current pattern 24, a pattern with content 5',
          (stock_patterns[0], stock_patterns[2]), (24, 5))
    check('here: current pattern still 24, a pattern with content %d' % dim,
          (patterns[0], patterns[2]), (24, dim))
    check('here: nothing else changed', [p for i, p in enumerate(patterns) if i not in (0, 2)],
          [p for i, p in enumerate(stock_patterns) if i not in (0, 2)])
    print('%s -- the dim SEQ-key state at %d (%d checks)' % ('PASS' if fail == 0 else 'FAIL', dim, ok + fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
