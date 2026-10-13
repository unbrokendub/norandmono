#!/usr/bin/env python3
"""T1: the morph pattern's unlinked X-Env trigs follow the morph knob.

Seen on the unit, 2026-10-02: on a fresh pattern with one note, the cutoff
X-Env fired on the note "and a couple more times per pattern", cutoff only.
The X-Env event log (p32) showed every extra one as a TRIG-from-PARAM on the
steps of pattern 1 -- the default "morphing pattern" -- which had unlinked
cutoff trigs.  The step processor ticks the events of both tracks; the
morph track's notes are dropped below morph 0.02 (0x08117A44), its ParamTrig
events never were.

Scenario: pattern 1 gets notes on steps 2 and 6 and unlinked cutoff trigs
on steps 3, 7 and 10, and is loaded as the morph partner (layer 1); pattern
9, clean but for one note on step 1, plays.

  1. stock image: the cutoff X-Env starts on the note and on 3, 7, 10
     (watched at the stock X-Env trigger: the stock has no event log);
  2. this image, morph 0: only on the note -- the morph pattern is silent;
  3. this image, morph 0.5: the morph pattern is in, its trigs fire and its
     notes play, as in the stock;
  4. this image with the stock call restored: the trigs come back -- the
     check in 2 is the patch, not the scenario.
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import diagmidi
import sysemu
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R2
from test_modnote_ui import booted

IMAGE = 'build/firmware-patched.bin'
STOCK = 'build/firmware.bin'
D = 0x2000E400
CUTOFF_OWNER = 0x20004834 + 0x17A8
MORPH = 0x200047F0 + 0x604C
SITE = 0x08146804
ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print('  %-66s ok' % label)
    else:
        fail += 1
        print('  %-66s FAIL got %r want %r' % (label, got, want))


def scenario(image):
    m = booted(image)
    uc = m.uc
    mgr = m.call_preserving(0x0811F854)

    def set_pattern(pattern, layer):
        m.run_unmasked()
        saved = [uc.reg_read(r) for r in m.REGS]
        uc.reg_write(UC_ARM_REG_R2, layer)
        try:
            m.call(0x08125A90, mgr, pattern)
        finally:
            for r, v in zip(m.REGS, saved):
                uc.reg_write(r, v)
        m.settle(300)

    set_pattern(1, 0)                                      # edit pattern 1
    m.tap(5); m.tap(9); m.settle(40)                       # notes, steps 2 and 6
    m.pot_values[9] = 0x6000; m.settle(150, audio=1)       # the cutoff...
    m.pot_values[21] = 0x1000; m.settle(150, audio=1)      # ...with an X-Env
    m.press(2); m.settle(30); m.press(3); m.settle(60)     # TYPE + WAVE held
    for k in (6, 10, 13):                                  # trigs, steps 3, 7, 10
        m.tap(k, hold_ms=30, gap_ms=40)
    m.release(3); m.settle(30); m.release(2); m.settle(60)
    set_pattern(9, 0)                                      # a clean pattern 9...
    set_pattern(1, 1)                                      # ...pattern 1 its morph partner
    m.tap(4); m.settle(60)                                 # one note, step 1
    return m


def play(m, ms=2100):
    """Steps (1-based, 16-step pattern) on which the cutoff X-Env was
    triggered -- every entry of the stock trigger 0x08156074 with the
    cutoff's envelope (owner + 0x1F0), whoever calls it: a note, a param
    trig -- and the steps of every note-on (0x081170E8 is reached for every
    played note, either track).  A trigger during an attack does not change
    the stage, so stage transitions would miss some."""
    uc = m.uc
    trigs, notes = [], []
    pos = lambda: struct.unpack('<I', bytes(uc.mem_read(0x2000AF84, 4)))[0]

    def trig(u, a, s, d):
        if u.reg_read(UC_ARM_REG_R0) == CUTOFF_OWNER + 0x1F0:
            trigs.append((pos() // 256) % 16 + 1)

    def note(u, a, s, d):
        notes.append((pos() // 256) % 16 + 1)
    hs = [uc.hook_add(UC_HOOK_CODE, trig, begin=0x08156074, end=0x08156074),
          uc.hook_add(UC_HOOK_CODE, note, begin=0x081170E8, end=0x081170E8)]
    uc.ctl_flush_tb()
    m.tap(27, hold_ms=20, gap_ms=10)
    m.settle(ms, audio=1)
    for h in hs:
        uc.hook_del(h)
    m.tap(27, hold_ms=20, gap_ms=10)
    m.settle(60)
    return sorted(set(trigs)), sorted(set(notes))


def main():
    print('1. stock image')
    m = scenario(STOCK)
    starts, notes = play(m)
    check('stock, morph 0: the morph pattern is mute (its notes do not play)', notes, [1])
    check('stock, morph 0: ...but the cutoff X-Env fires on its trigs, 3/7/10', starts, [1, 3, 7, 10])

    print('2..4. this image')
    m = scenario(IMAGE)
    snap = m.snapshot()
    check('the step processor\'s event tick is routed through np_morph_trig',
          bytes(m.uc.mem_read(SITE, 4)) != bytes.fromhex('9047aee7'), True)
    starts, notes = play(m)
    check('morph 0: only the note plays', notes, [1])
    check('morph 0: the cutoff X-Env fires on the note alone', starts, [1])

    m.restore(snap)
    m.uc.mem_write(MORPH, struct.pack('<f', 0.5))
    starts, notes = play(m)
    check('morph 0.5: the morph pattern\'s notes play too (2, 6)', notes, [1, 2, 6])
    check('morph 0.5: its trigs fire again, 3/7/10 (stock behaviour)', starts, [3, 7, 10])

    m.restore(snap)
    m.uc.mem_write(SITE, bytes.fromhex('9047aee7'))        # the stock blx r2; b
    m.uc.ctl_flush_tb()
    starts, notes = play(m)
    check('stock call restored, morph 0: the ghost trigs are back',
          starts, [1, 3, 7, 10])
    print('%s -- the morph pattern\'s trigs follow the morph knob (%d checks)'
          % ('PASS' if fail == 0 else 'FAIL', ok + fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
