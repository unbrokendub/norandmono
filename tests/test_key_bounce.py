#!/usr/bin/env python3
"""KB (p67): a bouncing key contact no longer loses a press or a release.

The DMA1_Stream0 handler scans one row of the switch matrix every 250 us, so
each key is sampled every 2 ms into an 8-bit shift register.  The stock takes
a press only on the exact pattern 0x0F and a release only on 0xF0; one bounce
sample breaks the pattern and the edge is never sent.  A lost release leaves
the key held for the UI and its modifier bit in the mask, and then every
chord fails -- UP+DOWN no longer leaves mod-note mode, TEMPO+FUNC+note no
longer generates -- until that key is pressed again.  KB takes an edge on the
last four samples and the key's debounced flag.

sysemu, the real scanner, dispatcher and UI:

  0. control, the stock: some release bounces leave the key held (mask bit
     set), and a bounced note release then blocks UP+DOWN;
  1. here: no release bounce of 90 (0.5-16 ms after opening, closed again
     for 0.5-12 ms) leaves the key held; presses and releases reach the UI
     strictly alternating, as many of each;
  2. a press that bounces (closed, open for 2 ms, closed) is delivered once
     (the stock drops some of these);
  3. a clean tap is one press and one release, a 700 ms hold also sends
     the long-hold event, both as before;
  4. after a bounced release of a note key, UP+DOWN still leaves mod-note
     mode.

    build/venv/bin/python tests/test_key_bounce.py
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sysemu                                        # noqa: E402
from unicorn import UC_HOOK_CODE                     # noqa: E402
from unicorn.arm_const import UC_ARM_REG_R1          # noqa: E402
from test_modnote_ui import booted                   # noqa: E402

IMAGE = 'build/firmware-patched.bin'
STOCK = 'build/firmware.bin'
SWITCH_ENTRY = 0x08155C7C          # the stock switch handler (spliced to np_ui_switch here)
SWITCH_VT = 0x0815E238
KEY = 41                           # the mini keyboard's E
ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print('  %-72s ok' % label)
    else:
        fail += 1
        print('  %-72s FAIL got %r want %r' % (label, got, want))


class Rig:
    def __init__(self, image):
        self.m = m = booted(image)
        self.uc = uc = m.uc
        self.events = []
        rd = lambda a: struct.unpack('<I', bytes(uc.mem_read(a, 4)))[0]

        def hook(u, a, s, d):
            ev = rd(u.reg_read(UC_ARM_REG_R1))
            if ev and rd(ev) == SWITCH_VT:
                self.events.append((u.mem_read(ev + 6, 1)[0], u.mem_read(ev + 5, 1)[0]))
        uc.hook_add(UC_HOOK_CODE, hook, begin=SWITCH_ENTRY, end=SWITCH_ENTRY)
        uc.ctl_flush_tb()
        m.settle(40)
        self.base = m.snapshot()

    def mask(self):
        return struct.unpack('<I', bytes(self.uc.mem_read(sysemu.UI + 0x7C, 4)))[0]

    def kinds(self, key=KEY):
        return [k for kk, k in self.events if kk == key]

    def bounce_release(self, gap_ms, width_ms):
        m = self.m
        m.restore(self.base)
        self.events.clear()
        m.press(KEY); m.settle(120)
        m.release(KEY); m.step(slices=int(gap_ms * 4))
        m.press(KEY); m.step(slices=max(1, int(width_ms * 4)))
        m.release(KEY); m.settle(200)

    def mode(self):
        return self.uc.mem_read(sysemu.UI + 0x84, 1)[0]

    def updown(self):
        m = self.m
        m.press(28); m.settle(40); m.press(29); m.settle(60)
        m.release(29); m.settle(40); m.release(28); m.settle(80)


GAPS = (0.5, 1, 2, 3, 4, 6, 8, 10, 12, 16)
WIDTHS = (0.5, 1, 2, 3, 4, 6, 8, 10, 12)


def alternating(kinds):
    """press (0) and release (2) strictly alternate, starting with a press,
    as many of each; long-hold events (1) may sit between"""
    edges = [k for k in kinds if k != 1]
    return edges == [0, 2] * (len(edges) // 2) and len(edges) % 2 == 0


def stuck_after_bounces(r):
    stuck = []
    order_bad = []
    for gap in GAPS:
        for width in WIDTHS:
            r.bounce_release(gap, width)
            if r.mask() & 0x4000:
                stuck.append((gap, width))
            if not alternating(r.kinds()):
                order_bad.append((gap, width, r.kinds()))
    return stuck, order_bad


def main():
    print('0. control, the stock')
    s = Rig(STOCK)
    stuck, _ = stuck_after_bounces(s)
    stock_stuck = stuck
    check('stock: release bounces that leave the key held: %d of %d (e.g. %s)'
          % (len(stuck), len(GAPS) * len(WIDTHS), stuck[:2]), len(stuck) > 0, True)
    gap, width = stuck[0]
    s.bounce_release(gap, width)
    s.m.press(28); s.m.settle(40); s.m.press(29); s.m.settle(40); s.m.release(29); s.m.release(28); s.m.settle(80)
    before = s.mode()
    s.updown()
    check('stock: after one, UP+DOWN changes nothing (mode %d -> %d)' % (before, s.mode()), s.mode(), before)

    print('1. here: release bounces')
    r = Rig(IMAGE)
    stuck, order_bad = stuck_after_bounces(r)
    check('release bounces that leave the key held: %d of %d' % (len(stuck), len(GAPS) * len(WIDTHS)), stuck, [])
    check('presses and releases alternate, as many of each: %d patterns break it %s'
          % (len(order_bad), order_bad[:2]), order_bad, [])

    print('2. a press that bounces')
    for image, rig in ((STOCK, s), (IMAGE, r)):
        lost = 0
        for open_ms in (1, 2, 3):
            for first_ms in (1, 2, 3, 4):
                m = rig.m
                m.restore(rig.base)
                rig.events.clear()
                m.press(KEY); m.step(slices=first_ms * 4)
                m.release(KEY); m.step(slices=open_ms * 4)
                m.press(KEY); m.settle(120)
                m.release(KEY); m.settle(120)
                if rig.kinds().count(0) != 1:
                    lost += 1
        tag = 'stock' if image == STOCK else 'here '
        if image == STOCK:
            check('%s: press bounces not delivered exactly once: %d of 12' % (tag, lost), lost > 0, True)
        else:
            check('%s: press bounces not delivered exactly once: %d of 12' % (tag, lost), lost, 0)

    print('3. clean taps and holds, as before')
    for image, rig in ((STOCK, s), (IMAGE, r)):
        m = rig.m
        m.restore(rig.base)
        rig.events.clear()
        m.press(KEY); m.settle(60); m.release(KEY); m.settle(60)
        tap = rig.kinds()
        rig.events.clear()
        m.press(KEY); m.settle(700); m.release(KEY); m.settle(60)
        hold = rig.kinds()
        tag = 'stock' if image == STOCK else 'here '
        check('%s: tap -> %s, 700 ms hold -> %s' % (tag, tap, hold), (tap, hold), ([0, 2], [0, 1, 2]))

    print('4. the chord after a bounced release')
    gap, width = stock_stuck[0]
    r.bounce_release(gap, width)
    r.updown()
    entered = r.mode()
    r.updown()
    check('bounce (%.1f ms, %.1f ms), then UP+DOWN: mode %d, again: %d' % (gap, width, entered, r.mode()),
          (entered, r.mode()), (2, 1))

    print('%s -- key edges survive contact bounce (%d checks)' % ('PASS' if fail == 0 else 'FAIL', ok + fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
