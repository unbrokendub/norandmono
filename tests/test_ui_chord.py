#!/usr/bin/env python3
"""UI unlink regression: run the actual patched callback through its deque.

See probe_ui_chord.py for explicit fixture boundaries. Project events are
captured, not applied: these tests do not emulate project storage or LEDs.
"""
import itertools
import struct

from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_SP, UC_ARM_REG_R4, UC_ARM_REG_R5, \
    UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9, \
    UC_ARM_REG_R10, UC_ARM_REG_R11

from probe_ui_chord import UiProbe, UI, SWITCHES


def drain(p):
    p.call(0x081574F8, UI)


def normal(p):
    assert p.mode() == 1, (p.mode(), p.project_events)
    assert p.m.u32(UI + 0x7C) == 0
    assert not p.project_events, p.project_events


def main():
    total = 0
    # Delayed release followed by another press BEFORE the consumer catches
    # up. A producer-side suppression flag cannot represent both generations.
    for press, release, finish, batch_all in itertools.product(
            ((2, 3), (3, 2)), ((2, 3), (3, 2)), ((2, 3), (3, 2)),
            (False, True)):
        p = UiProbe()
        for key in press:
            p.key(key, 0)
        assert p.mode() == 5
        for _ in range(3):
            for key in release:
                p.key(key, 2, drain=False)
            for key in press:
                p.key(key, 0, drain=False)
            if not batch_all:
                drain(p)
                assert p.mode() == 5
        drain(p)
        assert p.mode() == 5
        for key in finish:
            p.key(key, 2, drain=False)
        drain(p)
        normal(p)
        total += 1

    # Overlapping release/repress while the other chord button remains held.
    for first, last, batched in itertools.product((2, 3), (2, 3), (False, True)):
        p = UiProbe()
        p.key(2, 0)
        p.key(3, 0)
        p.key(first, 2, drain=not batched)
        p.key(first, 0, drain=not batched)
        drain(p)
        assert p.mode() == 5
        p.key(last, 2, drain=False)
        p.key(5 - last, 2, drain=False)
        drain(p)
        normal(p)
        total += 1

    # All release orders with a SEQ button still held; verify the actual
    # cutoff trigger event before testing that release does not cycle TYPE.
    for press, release, batch in itertools.product(
            ((2, 3), (3, 2)), itertools.permutations((2, 3, 4)), (False, True)):
        p = UiProbe()
        for key in (*press, 4):
            p.key(key, 0)
        assert len(p.project_events) == 1
        event = p.project_events.pop()
        assert struct.unpack_from('<I', event)[0] == 0x08164244
        assert event[13] == 9
        for key in release:
            p.key(key, 2, drain=not batch)
        drain(p)
        normal(p)
        total += 1

    # Recovery from the already-stale mode. The entry must be installed at
    # the real callback's BL, not merely pass a direct helper unit test.
    for press, release in itertools.product(((2, 3), (3, 2)), repeat=2):
        p = UiProbe()
        p.m.uc.mem_write(UI + 0x8B, b'\x05')
        for key in press:
            p.key(key, 0)
        assert p.mode() == 5
        for key in release:
            p.key(key, 2, drain=False)
        drain(p)
        normal(p)
        total += 1

    # Ordinary short clicks must still generate the same parameter action
    # as factory firmware, including a click following a completed chord.
    for key, after_chord, batch in itertools.product((2, 3), (False, True),
                                                    (False, True)):
        p = UiProbe()
        if after_chord:
            p.key(2, 0)
            p.key(3, 0)
            p.key(2, 2, drain=False)
            p.key(3, 2, drain=False)
            drain(p)
            normal(p)
        for kind in (0, 2):
            p.key(key, kind, drain=not batch)
        drain(p)
        stock = UiProbe(stock=True)
        stock.key(key, 0)
        stock.key(key, 2)
        assert p.project_events == stock.project_events
        assert len(p.project_events) == 1
        assert p.project_events[0][11] == (7 if key == 2 else 6)
        total += 1

    # The spliced function follows AAPCS and returns through the original
    # handler's destructor. Check SP and every integer callee-saved register.
    p = UiProbe()
    for key in (2, 3):
        p.key(key, 0)
    for key in (2, 3):
        p.key(key, 2, drain=False)
    registers = (UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
                 UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11)
    for i, reg in enumerate(registers):
        p.m.uc.reg_write(reg, 0x12340000 + i)
    sp = p.m.uc.reg_read(UC_ARM_REG_SP)
    drain(p)
    normal(p)
    assert p.m.uc.reg_read(UC_ARM_REG_SP) == sp
    assert [p.m.uc.reg_read(reg) for reg in registers] == [
        0x12340000 + i for i in range(len(registers))]
    total += 1

    # Adversarial ISR boundary: a new physical TYPE press is scanned after
    # the wrapper prepares the old release but before stock reads +0x11.
    # Isolate the producer's field writes here, not a second nested emulator.
    p = UiProbe()
    p.key(2, 0)
    p.key(3, 0)
    p.key(2, 2, drain=False)
    fired = []
    def newer_press(uc, address, size, data):
        if not fired:
            fired.append(True)
            uc.mem_write(SWITCHES + 2 * 12 + 16, b'\0\0\1')
    hook = p.m.uc.hook_add(UC_HOOK_CODE, newer_press,
                          begin=0x08155C80, end=0x08155C80)
    drain(p)
    p.m.uc.hook_del(hook)
    assert fired
    assert p.mode() == 1, 'live ISR suppression reset re-entered unlink'
    assert not p.project_events
    p.key(2, 0)
    assert p.mode() == 5
    p.key(3, 2)
    p.key(2, 2)
    normal(p)
    total += 1

    # Every FUNC/TYPE/WAVE press/release order. Compare actual project
    # events with the stock promptly serviced sequence, not another run of
    # the replacement. Some stock orders leave mode 5; those must recover.
    for press, release, batch in itertools.product(
            itertools.permutations((26, 2, 3)),
            itertools.permutations((26, 2, 3)), (False, True)):
        expected = UiProbe(stock=True)
        for key in press:
            expected.key(key, 0)
        for key in release:
            expected.key(key, 2)
        p = UiProbe()
        for key in press:
            p.key(key, 0, drain=not batch)
        for key in release:
            p.key(key, 2, drain=not batch)
        drain(p)
        assert p.mode() == 1, (press, release, batch, p.mode())
        assert p.project_events == expected.project_events, (
            press, release, batch, p.project_events, expected.project_events)
        assert p.m.u32(UI + 0x7C) == 0
        total += 1

    # PATTERN + all 16 SEQ keys. This fixture executes UI callbacks and
    # compares dispatch; it does NOT model loading a project from flash.
    for step, release, batch in itertools.product(
            range(4, 20), (False, True), (False, True)):
        order = (24, step) if release else (step, 24)
        expected = UiProbe(stock=True)
        p = UiProbe()
        for q, delayed in ((expected, False), (p, batch)):
            for key in (24, step):
                q.key(key, 0, drain=not delayed)
            for key in order:
                q.key(key, 2, drain=not delayed)
            drain(q)
        assert p.all_callbacks == expected.all_callbacks, (step, order, batch)
        assert p.project_events == expected.project_events
        assert p.mode() == expected.mode()
        assert p.m.u32(UI + 0x7C) == 0
        total += 1

    # All 48 keys: run the actual handler and installed helper entries.
    # UI callbacks alone are skipped so the fixture needs no fake project.
    p = UiProbe(dispatch_only=True)
    for key in range(48):
        p.key(key, 0)
        assert p.call(0x0814A390, UI, key) == 1
        assert p.call(0x0814A33C, UI, key) == 0
        assert p.call(0x0814A374, UI, key) == 0
        p.call(0x0814A358, UI, key)
        # Producer writes must not cancel a consumer's suppression.
        p.m.uc.mem_write(SWITCHES + key * 12 + 16, b'\0\0\0')
        assert p.call(0x0814A33C, UI, key) == 1
        assert p.call(0x0814A390, UI, key) == 1
        p.key(key, 1)
        p.key(key, 2)
        assert p.call(0x0814A390, UI, key) == 0
        assert p.call(0x0814A374, UI, key) == 1
        assert p.call(0x0814A33C, UI, key) == 1
        p.call(0x0814A3AC, UI, key)
        assert p.call(0x0814A33C, UI, key) == 0
        p.key(key, 0, drain=False)
        p.key(key, 2, drain=False)
        drain(p)
        assert p.call(0x0814A374, UI, key) == 0
        assert p.m.u32(UI + 0x7C) == 0
        total += 1

    # The actual all-up helper must keep a delivered modifier even if the
    # scanner's entire physical matrix already says "up". Last release is
    # still responsible for cleaning stale/consumed modifier bits.
    p = UiProbe(dispatch_only=True)
    p.key(26, 0)
    p.key(24, 0)
    p.key(24, 2, drain=False)
    p.key(26, 2, drain=False)
    # Drain normally; event-time helper effects are checked inside handler.
    seen = []
    def after_reconcile(uc, address, size, data):
        seen.append(p.m.u32(UI + 0x7C))
    p.m.uc.hook_add(UC_HOOK_CODE, after_reconcile,
                   begin=0x08155CE4, end=0x08155CE4)
    drain(p)
    assert seen == [1, 0], seen
    total += 1
    # A separate stock helper consumes complete modifier groups by writing
    # driver flags directly. Every one of its 17 TBB/default cases must
    # still have exactly the original per-key effects in the new shadow.
    for bit in range(17):
        stock = UiProbe(stock=True)
        p = UiProbe()
        stock.call(0x0814A734, UI, bit)
        p.call(0x0814A734, UI, bit)
        expected = [stock.m.uc.mem_read(SWITCHES + key * 12 + 17, 1)[0]
                    for key in range(48)]
        got = [p.call(0x0814A33C, UI, key) for key in range(48)]
        assert got == expected, (bit, got, expected)
        total += 1
    print('PASS: %d additional UI regression scenarios' % total)


if __name__ == '__main__':
    main()
