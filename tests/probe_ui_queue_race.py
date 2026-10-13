#!/usr/bin/env python3
"""Diagnostic: ISR deque map relocation during main's block pop.

Runs installed firmware queue code. UI actions/allocator are isolated by
UiProbe; no firmware file is changed. The producer is paused/resumed around
an explicit instruction boundary, not run recursively from a Unicorn hook.
This proves a possible interleaving, not its frequency on real hardware.
"""
import hashlib
import struct
import sys
from types import SimpleNamespace
from pathlib import Path
from unicorn import UC_HOOK_CODE, UcError
from unicorn.arm_const import (UC_ARM_REG_PC, UC_ARM_REG_R0, UC_ARM_REG_R1,
                               UC_ARM_REG_LR, UC_ARM_REG_SP, UC_ARM_REG_IPSR,
                               UC_ARM_REG_PRIMASK)
from probe_ui_chord import UiProbe, UI, STOP


class LiveProbe:
    """Booted full firmware: original heap/pools, real dispatch and callbacks."""
    def __init__(self, stock=False, **ignored):
        from sysemu import Mono
        # Raw uc_context bytes saved in a previous host process are not a
        # portable boot cache. Fresh boot avoids host crashes on restore.
        self.mono = Mono('build/firmware.bin' if stock else 'build/firmware-patched.bin').boot()
        self.mono.settle(50)
        self.mono.factory_defaults()
        uc = self.mono.uc
        self.m = SimpleNamespace(uc=uc,
            u32=lambda a: struct.unpack('<I', uc.mem_read(a, 4))[0])
        self.all_callbacks = self.mono.callbacks
        # The boot is complete. We call functions with a dedicated stack;
        # the main context itself is not resumed in this focused test.
        uc.reg_write(UC_ARM_REG_SP, 0x2001E000)
        self.ref = self.call(0x081591D4, 8)

    def call(self, address, r0=0, r1=0):
        return self.mono.call(address, r0, r1, count=3_000_000)

    def key(self, key, kind, drain=True):
        ev = self.call(0x081591D4, 8)
        self.m.uc.mem_write(ev, struct.pack('<IBBBB', 0x0815E238, 0, kind, key, 0))
        self.m.uc.mem_write(self.ref, struct.pack('<I', ev))
        self.call(0x08157324, UI, self.ref)
        if drain:
            self.call(0x081574F8, UI)
            self.call(0x08157650, 0x2000C68C)


def state(p):
    return {name: p.m.u32(UI + off) for name, off in (
        ('map', 0x1C), ('map_size', 0x20), ('start', 0x24),
        ('first', 0x28), ('last', 0x2C), ('start_node', 0x30),
        ('finish', 0x34), ('finish_first', 0x38), ('finish_last', 0x3C),
        ('finish_node', 0x40))}


def stub_address():
    img = Path('build/firmware-patched.bin').read_bytes()
    return struct.unpack_from('<I', img, 0x75200 + 0x134)[0] & ~1


def guarded_instructions():
    """The instructions of np_ui_queue_pop that must run with interrupts off:
    from the `cpsid i` up to and including the last store before `msr`.

    Walked from the raw halfwords rather than with capstone, whose build here
    stops at `mrs r0, primask`."""
    img = Path('build/firmware-patched.bin').read_bytes()
    stub = stub_address()
    code = img[stub - 0x08100000:stub - 0x08100000 + 48]
    addrs, a = [], 0
    while a < len(code) - 1:
        hw = struct.unpack_from('<H', code, a)[0]
        addrs.append((stub + a, hw))
        a += 4 if (hw & 0xF800) in (0xE800, 0xF000, 0xF800) else 2
    cpsid = next(i for i, (_, hw) in enumerate(addrs) if hw == 0xB672)
    msr = next(i for i, (_, hw) in enumerate(addrs) if hw == 0xF380)
    return [x for x, _ in addrs[cpsid + 1:msr]], addrs[:msr + 1]


def scenario(stock, pause=None, live=False, watch=None):
    p = (LiveProbe if live else UiProbe)(stock=stock, dispatch_only=True)
    uc = p.m.uc
    # Reach this layout by ordinary pushes/pops, not invented pointers:
    # empty at last element of the penultimate map block.
    n = 0
    while True:
        s = state(p)
        if (s['start_node'] == s['map'] + (s['map_size'] - 2) * 4 and
                s['start'] == s['last'] - 4):
            break
        p.key(2, n & 1 and 2 or 0)
        n += 1
        assert n < 3000, s
    for i in range(128):
        p.key(2, i & 1 and 2 or 0, drain=False)
    before = state(p)
    assert before['finish_node'] == before['map'] + 4 * (before['map_size'] - 1)
    assert before['finish'] == before['finish_last'] - 4
    p.all_callbacks.clear()
    delivered = []
    def delivery(uc, a, size, data):
        delivered.append(p.m.u32(uc.reg_read(UC_ARM_REG_R1)))
    uc.hook_add(UC_HOOK_CODE, delivery, begin=0x0815748C, end=0x0815748C)
    if watch is not None:
        stub = stub_address()
        uc.hook_add(UC_HOOK_CODE,
                    lambda uc, a, size, data: watch.__setitem__(
                        a, uc.reg_read(UC_ARM_REG_PRIMASK)),
                    begin=stub, end=stub + 48)
    if pause is None:
        p.key(2, 0, drain=False)
        p.call(0x081574F8, UI)
    else:
        reached = []
        def stop(uc, a, size, data):
            reached.append(a)
            uc.emu_stop()
        h = uc.hook_add(UC_HOOK_CODE, stop, begin=pause, end=pause)
        uc.reg_write(UC_ARM_REG_R0, UI)
        uc.reg_write(UC_ARM_REG_LR, STOP | 1)
        uc.emu_start(0x081574F9, STOP, count=100_000)
        uc.hook_del(h)
        assert reached, ('pop boundary not reached', state(p))
        saved = uc.context_save()
        # ISR produces one more event. The real push recentres the map.
        # Registers survive IRQ return; shared memory intentionally does not.
        assert not uc.reg_read(UC_ARM_REG_PRIMASK), 'IRQ would be masked'
        uc.reg_write(UC_ARM_REG_IPSR, 27)  # DMA1_Stream0, real producer context
        uc.reg_write(UC_ARM_REG_SP, (uc.reg_read(UC_ARM_REG_SP) & ~7) - 104)
        p.key(2, 0, drain=False)
        relocated = state(p)
        uc.context_restore(saved)
        try:
            uc.emu_start(pause | 1, STOP, count=3_000_000)
        except UcError as e:
            print('  target exception', e, 'pc=%08x' % uc.reg_read(UC_ARM_REG_PC))
        print('  map node before=%08x after ISR=%08x after pop=%08x' % (
            before['start_node'], relocated['start_node'], state(p)['start_node']))
    after = state(p)
    ok = (uc.reg_read(UC_ARM_REG_PC) == STOP and
          after['start'] == after['finish'] and len(delivered) == 129 and
          (live or len(set(delivered)) == 129) and 0 not in delivered)
    print('%s pause=%s warmup=%d: events=%d/129 returned=%s empty=%s healthy=%s' % (
        ('live ' if live else 'isolated ') + ('stock' if stock else 'patched'), hex(pause) if pause else 'none', n,
        len(delivered), uc.reg_read(UC_ARM_REG_PC) == STOP,
        after['start'] == after['finish'], ok), flush=True)
    if not ok:
        print('  final PC=%08x, last event=%08x, start=%08x finish=%08x' % (
            uc.reg_read(UC_ARM_REG_PC), delivered[-1], after['start'], after['finish']))
    return ok


if __name__ == '__main__':
    files = [Path('build/firmware.bin'), Path('build/firmware-patched.bin')]
    hashes = [hashlib.sha256(f.read_bytes()).digest() for f in files]
    live = '--live' in sys.argv
    # Stock: the two hazardous boundaries are reachable and corrupt the deque.
    assert scenario(True, live=live), 'serial control failed'
    for pause in (0x08157558, 0x0815755A, 0x0815755C, 0x0815755E, 0x08157560):
        expected = pause not in (0x0815755A, 0x0815755C)
        assert scenario(True, pause, live=live) == expected, 'unexpected interleaving result'

    # Patched (N): those boundaries no longer exist -- the transition is
    # replayed inside np_ui_queue_pop with interrupts masked, so the producer
    # cannot interleave with any part of it.  Assert that directly instead of
    # injecting a push the hardware could never deliver there.
    watch = {}
    assert scenario(False, live=live, watch=watch), 'serial control failed'
    guarded, ins = guarded_instructions()
    print('\npatched: np_ui_queue_pop at 0x%08X' % stub_address())
    for a, _hw in ins:
        print('  0x%08X  PRIMASK=%s%s' % (
            a, 'unvisited' if a not in watch else watch[a],
            '   <- guarded' if a in guarded else ''))
    assert set(guarded) <= set(watch), 'the guarded run was never executed'
    unmasked = [a for a in guarded if not watch[a]]
    assert not unmasked, ['0x%08X' % a for a in unmasked]
    assert watch.get(ins[0][0]) == 0, "entry inherits the caller's PRIMASK"
    assert watch[ins[-1][0]] == 1, 'PRIMASK must still be set at the restore'
    print('  -> all %d instructions of the start-iterator transition run masked'
          % len(guarded))

    assert hashes == [hashlib.sha256(f.read_bytes()).digest() for f in files]
    print('\nqueue race: reproduced on stock, closed on the patched image')
