#!/usr/bin/env python3
"""Real firmware FIFO load, project starvation and ISR map relocation.

Peripheral time is emulated; these checks establish event preservation and
scheduling bounds, not the cause of the hardware capture's 139 ms interval.
The legacy control restores two instruction sites in emulator memory only.
"""
import struct
from collections import Counter
from pathlib import Path
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_WRITE, UC_HOOK_MEM_READ
from unicorn.arm_const import (UC_ARM_REG_SP, UC_ARM_REG_R0, UC_ARM_REG_R1,
    UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_IPSR, UC_ARM_REG_PRIMASK)
from sysemu import Mono, UI, STOP

MANAGER = 0x2000C68C
POT, SWITCH, RECORD_AUTOMATION = 0x0815E0C0, 0x0815E238, 0x0816FF60


class Fixture:
    def __init__(self, legacy=False):
        self.m = Mono('build/firmware-patched.bin').boot()
        self.m.settle(50)
        self.m.factory_defaults()
        self.uc = self.m.uc
        self.uc.reg_write(UC_ARM_REG_SP, 0x2001E000)
        self.ref = self.call(0x081591D4, 8)
        self.service = 0x081574F8 if legacy else self.get(0x08175350)
        if legacy:
            self.uc.mem_write(0x0815766C, bytes.fromhex('4ff00008'))
            self.uc.ctl_remove_cache(0x08157650, 0x081576D4)
        self.uc.mem_write(UI + 0x84, b'\x01')
        self.uc.mem_write(UI + 0x8B, b'\x01')
        system = self.call(0x08109614)
        self.scan = self.call(0x0810977C, system, 2)
        self.delivered, self.submitted = [], []
        self.calls = Counter()
        self.uc.hook_add(UC_HOOK_CODE, self.dispatch, begin=0x0815748C, end=0x0815748C)
        self.uc.hook_add(UC_HOOK_CODE, self.submit, begin=0x08157578, end=0x08157578)
        self.uc.hook_add(UC_HOOK_CODE, self.flash, begin=0x08143378, end=0x08143378)
        self.qspi_before = self.m.qspi.log.copy()

    def get(self, a): return struct.unpack('<I', self.uc.mem_read(a, 4))[0]
    def put(self, a, v): self.uc.mem_write(a, struct.pack('<I', v))
    def call(self, a, r0=0, r1=0): return self.m.call(a, r0, r1, count=20_000_000)
    def empty(self): return self.get(UI + 0x24) == self.get(UI + 0x34)
    def pending(self): return self.get(MANAGER + 0x24)
    def dispatch(self, uc, a, s, d):
        if uc.reg_read(UC_ARM_REG_R0) != UI: return
        ev = self.get(uc.reg_read(UC_ARM_REG_R1))
        assert ev, 'null event reached dispatcher'
        self.delivered.append(bytes(uc.mem_read(ev, 12 if self.get(ev) == POT else 8)))
    def submit(self, uc, a, s, d):
        if uc.reg_read(UC_ARM_REG_R0) != MANAGER: return
        ev = self.get(uc.reg_read(UC_ARM_REG_R1))
        self.submitted.append(self.get(ev))
    def flash(self, uc, a, s, d): self.calls['internal_flash'] += 1
    def push(self, data):
        event = self.call(0x081591D4, len(data))
        self.uc.mem_write(event, data)
        self.put(self.ref, event)
        self.call(0x08157324, UI, self.ref)
        assert self.get(self.ref) == 0
    def pot(self, pot, value):
        group = self.uc.mem_read(self.scan + pot * 104 + 0x71, 1)[0]
        return struct.pack('<IBBBxHxx', POT, group, 3, pot, value)
    def drain(self):
        peak, batch = 0, 0
        for _ in range(2000):
            before = len(self.delivered)
            self.call(self.service, UI)
            batch = max(batch, len(self.delivered) - before)
            peak = max(peak, self.pending())
            self.call(0x08157650, MANAGER)
            if self.empty() and not self.pending(): return peak, batch
        raise AssertionError('queues did not drain')


def burst(legacy=False, recording=False):
    f = Fixture(legacy)
    if recording:
        f.uc.mem_write(UI + 0xED, b'\x01')
        transport = f.call(0x08118380)
        f.uc.mem_write(transport + 0x6C, b'\x01')
    expected = []
    # Multiple deque blocks, allocator overflow and reversals of direction.
    for i in range(685):
        data = f.pot(9, (i * 401) % 65536)
        f.push(data); expected.append(data)
        if not recording and i in (100, 200, 300, 400):
            data = struct.pack('<IBBBB', SWITCH, 0, 0 if i % 200 else 2, 2, 0)
            f.push(data); expected.append(data)
    peak, batch = f.drain()
    assert f.delivered == expected, 'FIFO loss, duplication, or key reordering'
    assert not f.m.qspi.log - f.qspi_before and not f.calls, 'unexpected flash access'
    assert f.get(0x2000E478) == 0 and f.get(0x2000E47C) == 0, 'bad free or failed malloc'
    assert not f.uc.reg_read(UC_ARM_REG_PRIMASK), 'interrupt mask leaked'
    if legacy:
        assert batch == len(expected) and peak >= 685
    else:
        assert 1 <= batch <= 8 and peak <= 32, (batch, peak)
    if recording:
        assert f.submitted.count(RECORD_AUTOMATION) == 685, Counter(f.submitted)
    print('burst legacy=%s recording=%s: %d FIFO events, max batch %d, '
          'project peak %d, flash operations 0' % (legacy, recording, len(expected), batch, peak), flush=True)
    return f.submitted


def starvation():
    f = Fixture()
    for i in range(8): f.push(f.pot(9, 10000 + 1000*i))
    f.call(f.service, UI)
    pending = f.pending()
    assert pending >= 2
    clock = [0]
    def expired(uc, a, s, d):
        clock[0] += 300  # first check is after the entire 250 us budget
        uc.reg_write(UC_ARM_REG_R0, clock[0])
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
    h = f.uc.hook_add(UC_HOOK_CODE, expired, begin=0x081479A4, end=0x081479A4)
    patched = bytes(f.uc.mem_read(0x0815766C, 4))
    f.uc.mem_write(0x0815766C, bytes.fromhex('4ff00008'))
    f.uc.ctl_remove_cache(0x08157650, 0x081576D4)
    f.call(0x08157650, MANAGER)
    assert f.pending() == pending, 'legacy control should starve'
    f.uc.mem_write(0x0815766C, patched)
    f.uc.ctl_remove_cache(0x08157650, 0x081576D4)
    f.call(0x08157650, MANAGER)
    assert f.pending() == pending - 1, 'must apply exactly one before expired budget'
    f.put(MANAGER + 0x54, 0)
    f.call(0x08157650, MANAGER)
    assert f.pending() == pending - 1, 'zero budget must remain disabled'
    # Other manager objects retain their original first-check semantics.
    other = 0x20018000
    f.uc.mem_write(other, bytes(f.uc.mem_read(MANAGER, 0x60)))
    f.put(other + 0x54, 250)
    f.call(0x08157650, other)
    assert f.get(other + 0x24) == pending - 1
    f.uc.hook_del(h)
    print('IRQ-consumed budget: legacy starves, patched advances; zero/other manager unchanged', flush=True)


def backpressure():
    f = Fixture()
    for i in range(80): f.push(f.pot(9, 10000 + 401*i))
    for _ in range(40): f.call(f.service, UI)
    assert f.pending() == 32 and len(f.delivered) == 32 and not f.empty()
    f.drain()
    assert len(f.delivered) == 80
    print('backpressure: pauses at 32 pending, then resumes with all 80 events', flush=True)


def time_budget():
    f = Fixture()
    for i in range(10): f.push(f.pot(9, 10000 + 401*i))
    clock = [0xFFFF0000]
    def advance(uc, access, a, size, value, data):
        clock[0] = (clock[0] + 200000) & 0xFFFFFFFF
        f.put(a, clock[0])
    h = f.uc.hook_add(UC_HOOK_MEM_READ, advance, begin=0xE0001004, end=0xE0001007)
    f.call(f.service, UI)
    assert len(f.delivered) == 1, 'expired UI budget must yield after its first event'
    f.uc.hook_del(h)
    f.drain()
    assert len(f.delivered) == 10
    print('UI time budget: yields after one expensive callback, all 10 eventually delivered', flush=True)


def relocation():
    f = Fixture()
    # Reach the dangerous deque boundary through ordinary pushes and pops.
    for n in range(3000):
        if (f.get(UI + 0x30) == f.get(UI + 0x1C) + (f.get(UI + 0x20) - 2) * 4
                and f.get(UI + 0x24) == f.get(UI + 0x2C) - 4): break
        f.push(f.pot(9, 10000)); f.drain()
    else: raise AssertionError('deque boundary not reached')
    expected = [f.pot(9, 15000 + i) for i in range(128)]
    for event in expected: f.push(event)
    f.delivered.clear()
    writes = []
    def observe(uc, access, a, size, value, data):
        writes.append((a, uc.reg_read(UC_ARM_REG_PRIMASK)))
    watch = f.uc.hook_add(UC_HOOK_MEM_WRITE, observe, begin=UI+0x24, end=UI+0x33)
    pause = f.get(0x08175344) & ~1  # np_diag_dispatch, after the atomic pop
    reached = []
    def stop(uc, a, s, d): reached.append(a); uc.emu_stop()
    h = f.uc.hook_add(UC_HOOK_CODE, stop, begin=pause, end=pause)
    f.uc.reg_write(UC_ARM_REG_R0, UI)
    f.uc.reg_write(UC_ARM_REG_LR, STOP | 1)
    f.uc.emu_start(f.service | 1, STOP, count=100000)
    f.uc.hook_del(h); f.uc.hook_del(watch)
    assert reached and writes and all(mask for _, mask in writes)
    assert {UI+0x24, UI+0x28, UI+0x2C, UI+0x30} <= {a for a, _ in writes}
    assert not f.uc.reg_read(UC_ARM_REG_PRIMASK)
    saved = f.uc.context_save()
    old_map, old_node = f.get(UI+0x1C), f.get(UI+0x30)
    f.uc.reg_write(UC_ARM_REG_IPSR, 27)
    f.uc.reg_write(UC_ARM_REG_SP, (f.uc.reg_read(UC_ARM_REG_SP) & ~7) - 104)
    for i in range(129):
        event = f.pot(9, 25000 + i)
        expected.append(event); f.push(event)
    assert (f.get(UI+0x1C), f.get(UI+0x30)) != (old_map, old_node), 'producer did not relocate map'
    f.uc.context_restore(saved)
    f.uc.emu_start(pause | 1, STOP, count=3_000_000)
    assert f.uc.reg_read(UC_ARM_REG_PC) == STOP
    f.drain()
    assert f.delivered == expected and f.empty()
    assert f.get(0x2000E478) == 0
    print('ISR map relocation after atomic block pop: 257 FIFO events, no corruption', flush=True)


if __name__ == '__main__':
    before = Path('build/firmware-patched.bin').read_bytes()
    burst(legacy=True)
    burst()
    old = burst(legacy=True, recording=True)
    new = burst(recording=True)
    assert old == new, 'recording must emit the same sequence of project event types'
    starvation()
    backpressure()
    time_budget()
    relocation()
    assert Path('build/firmware-patched.bin').read_bytes() == before
    print('UI burst regression PASS (hardware timing still requires a capture)')
