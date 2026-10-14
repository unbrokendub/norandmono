#!/usr/bin/env python3
"""PJ, PB (p60): a project slot cut short -- a save that lost power -- at
power-on and when loaded.

A slot is a 0x100-byte header and an event journal ([id][payload] from
SOF to EOF).  A save erases the slot and programs it from the start, so a
power loss leaves a journal that stops in erased flash (0xFF), or a slot
erased outright.  The stock's reader called an empty std::function for an
id with no factory (abort -> b .), before any of its checks; main loads
slot 0 at power-on, so the unit stopped there on every power-on.

  0. control, the stock: slot 0 cut short -> the power-on load ends in the
     abort loop 0x0815A6E0, main is never reached;
  1. slot 0 cut short: main runs, the check that failed is 0xA1 (no EOF),
     an empty project whose 64 patterns have 16 steps, each layer on its
     own pattern, no error screen, slot 0 in flash untouched;
  2. slot 0 erased: 0xA4 (no first event), the same empty project;
  3. a sound slot 0: no fallback, D_PBOOT all zero;
  4. after 1, FUNC+SAVE writes slot 0 again and the next power-on loads it;
  5. a damaged slot loaded after power-on: the stock's error screen with
     0xA1 (the watchdog restarts the unit from it on hardware), no abort.

Each power-on is a fresh boot (no snapshot cache): ~15-60 s each.

    build/venv/bin/python tests/test_project_journal.py
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sysemu                                        # noqa: E402
from unicorn import UC_HOOK_CODE                     # noqa: E402
from unicorn.arm_const import UC_ARM_REG_R1          # noqa: E402

IMAGE = 'build/firmware-patched.bin'
STOCK = 'build/firmware.bin'
MGR = 0x2000C68C
SEQ = 0x2000CFF4
D_PBOOT = 0x2000E400 + 0x13F0
ABORT_LOOP = 0x0815A6E0
ERROR_SCREEN = 0x0814FA54
SLOT = 0x80000
ok = fail = 0

# a journal as a save leaves it: SOF, a few ProjectParamData (id 10, two
# bytes), then the erased rest -- no EOF
CUT = b'\0' * 0x100 + bytes([254]) + bytes([10, 0, 0, 10, 1, 0, 10, 2, 1, 10, 3, 2])
EMPTY = b'\0' * 0x100 + bytes([254, 9])


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print('  %-72s ok' % label)
    else:
        fail += 1
        print('  %-72s FAIL got %r want %r' % (label, got, want))


class Unit:
    """A fresh power-on with the given QSPI contents."""

    def __init__(self, image, slots, header_fix=False, async_qspi=False):
        self.m = m = sysemu.Mono(image=image)
        self.uc = uc = m.uc
        for n, data in slots.items():
            uc.mem_write(sysemu.QSPI_MEM + n * SLOT, data + b'\xFF' * (SLOT - len(data)))
        self.log = []
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: (self.log.append(('abort', a)), u.emu_stop()),
                    begin=ABORT_LOOP, end=ABORT_LOOP)
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: self.log.append(('screen', u.reg_read(UC_ARM_REG_R1))),
                    begin=ERROR_SCREEN, end=ERROR_SCREEN)
        if header_fix:
            # the emulator's save leaves the pattern table the stock's own
            # check (0xA2) refuses (see test_sync_load.py); give the image
            # the table the replay built, as a unit's laid-out slot has
            uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: uc.mem_write(0x24000000, bytes(uc.mem_read(MGR + 0x228, 0x100))),
                        begin=0x0812414C, end=0x0812414C)
        self.booted = True
        try:
            m.boot(limit=3_000_000_000)
        except AssertionError:
            self.booted = False
        if self.booted:
            m.settle(50)
            if async_qspi:
                m.enable_qspi_async(delay_slices=4)

    def pboot(self):
        return tuple(self.uc.mem_read(D_PBOOT, 4))

    def lengths(self):
        pattern = self.m.call_preserving(0x08145C38)
        return (struct.unpack('<H', bytes(self.uc.mem_read(pattern + 0x60, 2)))[0] >> 8,
                struct.unpack('<H', bytes(self.uc.mem_read(SEQ + 0x60, 2)))[0] >> 8)

    def slot(self, n, size):
        return bytes(self.uc.mem_read(sysemu.QSPI_MEM + n * SLOT, size))

    def layers(self):
        """(pattern, steps) each layer plays: mgr+0x183 + layer, bank byte 0"""
        return [(self.uc.mem_read(MGR + 0x183 + b, 1)[0], self.uc.mem_read(MGR + 0x18D + b * 0x13, 1)[0])
                for b in (0, 1)]

    def visit(self, patterns):
        """select each pattern on layer 0 (PATTERN, PAGE, SEQ, PATTERN) and
        read its length; back on pattern 1 (index 0) after"""
        m, seen = self.m, {}
        for p in tuple(patterns) + (0,):
            m.tap(24); m.settle(30); m.tap(20 + p // 16); m.settle(30)
            m.tap(4 + p % 16); m.settle(30); m.tap(24); m.settle(80)
            seen[p] = self.layers()[0][1]
        del seen[0]
        return seen


def stock_control():
    print('0. control: the stock, slot 0 cut short')
    u = Unit(STOCK, {0: CUT})
    check('the power-on load ends in the abort loop, main never runs',
          (u.booted, [k for k, _ in u.log]), (False, ['abort']))


def cut_short():
    print('1. slot 0 cut short at power-on')
    u = Unit(IMAGE, {0: CUT})
    check('main runs: no abort, no error screen %s' % u.log, (u.booted, u.log), (True, []))
    check('D_PBOOT %s: done, 0xA1 first, an empty project' % (u.pboot(),),
          (u.pboot()[0], u.pboot()[1], u.pboot()[3]), (0, 0xA1, 1))
    check('both patterns 16 steps, the sequencer\'s too: %s' % (u.lengths(),), u.lengths(), (16, 16))
    check('each layer back on its own pattern: %s' % (u.layers(),), u.layers(), [(0, 16), (1, 16)])
    seen = u.visit((7, 16, 33, 48, 63))
    check('every pattern 16 steps (PATTERN, PAGE, SEQ): %s' % seen, set(seen.values()), {16})
    check('slot 0 in flash untouched', u.slot(0, len(CUT) + 16), CUT + b'\xFF' * 16)
    u.m.tap(27); u.m.settle(300); u.m.tap(27); u.m.settle(20)
    check('it plays and stops, nothing faults: errors %s' % u.m.errors, (u.m.errors, u.log), ([], []))
    return u


def erased():
    print('2. slot 0 erased at power-on')
    u = Unit(IMAGE, {0: b''})
    check('main runs: no abort, no error screen %s' % u.log, (u.booted, u.log), (True, []))
    check('D_PBOOT %s: 0xA4, an empty project' % (u.pboot(),), (u.pboot()[0], u.pboot()[1], u.pboot()[3]), (0, 0xA4, 1))
    check('both patterns 16 steps: %s' % (u.lengths(),), u.lengths(), (16, 16))


def sound():
    print('3. a sound slot 0')
    u = Unit(IMAGE, {0: EMPTY})
    check('main runs, D_PBOOT %s' % (u.pboot(),), (u.booted, u.pboot(), u.log), (True, (0, 0, 0, 0), []))


def recovered():
    print('4. after the fallback, a save writes slot 0 again')
    u = Unit(IMAGE, {0: CUT}, async_qspi=True)
    m = u.m
    m.tap(36); m.settle(15); m.tap(4); m.settle(15)            # a D on step 1
    m.press(26); m.settle(20); m.tap(32, hold_ms=40, gap_ms=10); m.release(26); m.settle(20)
    for _ in range(80):
        m.settle(10)
        if m.qspi_queue_len() == 0:
            break
    m.settle(50)
    saved = u.slot(0, SLOT)
    end = saved.find(b'\xFF' * 64, 0x100)
    check('slot 0 rewritten: %d journal bytes, SOF first' % (end - 0x100), (saved[0x100], end > 0x110), (254, True))
    v = Unit(IMAGE, {0: saved[:end]}, header_fix=True)
    check('the next power-on loads it: D_PBOOT %s, no screen' % (v.pboot(),), (v.booted, v.pboot(), v.log),
          (True, (0, 0, 0, 0), []))
    check('with its 16-step patterns: %s' % (v.lengths(),), v.lengths(), (16, 16))


def later_load():
    print('5. a damaged slot loaded after power-on')
    u = Unit(IMAGE, {0: EMPTY, 1: CUT}, async_qspi=True)
    m = u.m
    m.factory_defaults()          # 16-step patterns: with none, LOAD + SEQ asked no load here
    m.press(26); m.settle(10); m.tap(33, hold_ms=30, gap_ms=10); m.release(26); m.settle(10)
    m.tap(5, hold_ms=30, gap_ms=10)                            # SEQ 2: slot 1
    m.settle(600)
    check('the stock\'s error screen with 0xA1, no abort: %s' % u.log, [x for x in u.log], [('screen', 0xA1)])


def main():
    stock_control()
    cut_short()
    erased()
    sound()
    recovered()
    later_load()
    print('%s -- a slot cut short neither hangs the power-on nor a load (%d checks)'
          % ('PASS' if fail == 0 else 'FAIL', ok + fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
