#!/usr/bin/env python3
"""Read-only probe of stock UI construction/dispatch in the real image.

Heap allocation is a bounded host-side bump allocator. Hardware and project
effects are isolated explicitly; the UI constructor, maps and key dispatch
are executed, not reimplemented. No firmware file is modified.
"""
import struct
import itertools
import hashlib
from pathlib import Path

from unicorn import (Uc, UC_HOOK_CODE, UC_ARCH_ARM, UC_MODE_THUMB,
                     UC_MODE_MCLASS, UC_PROT_ALL)
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                               UC_ARM_REG_R3, UC_ARM_REG_LR, UC_ARM_REG_PC,
                               UC_ARM_REG_SP, UC_ARM_REG_MSP, UC_ARM_REG_R5,
                               UC_CPU_ARM_CORTEX_M7)
from test_patch import Machine, BASE, STOP, stub_return_const

UI = 0x2000DE54
SWITCHES = 0x20011000
SCRATCH = 0x20014000
NEW = 0x081591D4


class UiProbe:
    def __init__(self, stock=False, omit_live_reset=False, verbose=False,
                 dispatch_only=False):
        self.verbose = verbose
        self.dispatch_only = dispatch_only
        self.m = Machine.__new__(Machine)
        self.m.img = Path("build/firmware.bin" if stock else
                          "build/firmware-patched.bin").read_bytes()
        self.next_alloc = 0x30001000
        self.allocated = []
        self.trace = []
        self.all_callbacks = []
        self.project_events = []
        self.pressed = set()
        self.registrations = []
        uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        uc.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M7)
        self.m.uc = uc
        for base, length in ((0, 0x10000), (BASE, 0xE0000),
                             (0x20000000, 0x20000), (0x30000000, 0x50000),
                             (0xE0000000, 0x10000)):
            uc.mem_map(base, length, UC_PROT_ALL)
        uc.mem_write(BASE, self.m.img)
        uc.mem_write(STOP, b"\x70\x47")
        self.m.w32(0xE000ED88, 0x00F00000)
        uc.reg_write(UC_ARM_REG_MSP, 0x2001F000)
        uc.reg_write(UC_ARM_REG_SP, 0x2001F000)
        uc.hook_add(UC_HOOK_CODE, self.allocate, begin=NEW, end=NEW)
        # Actual C++ delete is irrelevant with the non-reusing test allocator.
        for address in (0x0815906C, 0x08159070):
            uc.mem_write(address, b"\x70\x47")
        uc.hook_add(UC_HOOK_CODE, self.register,
                    begin=0x081494F0, end=0x081494F0)
        # Unicorn M7 lacks the double-precision instructions in libstdc++'s
        # prime rehash policy. Keep its constructor's one-bucket hash maps:
        # collisions still run the stock equality and linked-list logic.
        # This fixture tests dispatch semantics, NOT hash-map performance.
        uc.mem_write(0x08159920, bytes.fromhex("0023036043607047"))
        self.call(0x0814A8FC, UI)
        assert len(self.registrations) == 97, len(self.registrations)
        self.call(0x0810F490, SWITCHES)
        stub_return_const(self.m, 0x0810977C, SWITCHES)
        stub_return_const(self.m, 0x081097A0, 0)  # startup key suppression off
        uc.hook_add(UC_HOOK_CODE, self.gpio_read,
                    begin=0x081029C0, end=0x081029C0)
        # UI refresh requests cannot talk to the physical LED driver here.
        uc.mem_write(0x0814FCDC, b"\x70\x47")
        uc.hook_add(UC_HOOK_CODE, self.dispatch,
                    begin=0x08155D14, end=0x08155D14)
        uc.hook_add(UC_HOOK_CODE, self.project_submit,
                    begin=0x08157578, end=0x08157578)
        # Normal sequencer/panel mode after loading a project.
        uc.mem_write(UI + 0x84, b"\x01")
        uc.mem_write(UI + 0x8B, b"\x01")
        uc.mem_write(UI + 0x92, b"\x09")  # FiltFreq / cutoff
        if omit_live_reset:
            # Causal counterfactual in emulator memory ONLY. This is not a
            # shippable fix: other shortcuts/recovery need a separate audit.
            original = bytes.fromhex("f4f718fb")
            assert bytes(uc.mem_read(0x08155CE0, 4)) == original
            uc.mem_write(0x08155CE0, bytes.fromhex("00bf00bf"))

    def dispatch(self, uc, address, size, data):
        self.trace.append(uc.reg_read(UC_ARM_REG_R3))
        self.all_callbacks.append((uc.reg_read(UC_ARM_REG_R3),
                                   uc.mem_read(uc.reg_read(UC_ARM_REG_R1), 1)[0]))
        if self.dispatch_only:
            # For state-helper tests only: still run lookup/mask/ownership,
            # but do not let an unrelated action access an absent project.
            uc.reg_write(UC_ARM_REG_PC, (address + size) | 1)

    def gpio_read(self, uc, address, size, data):
        # At the real scanner's HAL_GPIO_ReadPin call, r5 holds the matrix
        # key index. Only the external pin level is synthesized here.
        assert (uc.reg_read(UC_ARM_REG_LR) & ~1) == 0x0810F620
        key = uc.reg_read(UC_ARM_REG_R5)
        uc.reg_write(UC_ARM_REG_R0, int(key in self.pressed))
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def scan(self, pressed, drain=True):
        self.pressed = set(pressed)
        # Eight complete scans establish 0xFF/0x00 histories. Press/release
        # events are produced by the firmware at 0x0F/0xF0, not by this loop.
        for _ in range(8):
            for row in range(8):
                self.call(0x0810F5D0, SWITCHES, row)
                if drain:
                    self.call(0x081574F8, UI)

    def project_submit(self, uc, address, size, data):
        pointer_ref = uc.reg_read(UC_ARM_REG_R1)
        pointer = self.m.u32(pointer_ref)
        self.project_events.append(bytes(uc.mem_read(pointer, 16)))
        self.m.w32(pointer_ref, 0)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def key(self, key, event, drain=True):
        uc = self.m.uc
        group = uc.mem_read(SWITCHES + key * 12 + 9, 1)[0]
        if event == 0:
            uc.mem_write(SWITCHES + key * 12 + 16, b"\0\0\1")
        elif event == 1:
            self.m.w32(SWITCHES + key * 12 + 12, 200)
        elif event == 2:
            held = self.m.u32(SWITCHES + key * 12 + 12) > 199
            uc.mem_write(SWITCHES + key * 12 + 16, bytes([held]))
            self.m.w32(SWITCHES + key * 12 + 12, 0)
            uc.mem_write(SWITCHES + key * 12 + 18, b"\0")
        pointer = self.next_alloc
        self.next_alloc += 8
        uc.mem_write(pointer, struct.pack("<IBBBB", 0x0815E238,
                                          group, event, key, 0))
        self.m.w32(SCRATCH, pointer)
        self.trace.clear()
        self.call(0x08157324, UI, SCRATCH)
        if drain:
            self.call(0x081574F8, UI)
        if self.verbose:
            print("key=%2d event=%d mask=%04x mode=%d suppressed=%d/%d callbacks=%s" %
                  (key, event, self.m.u32(UI + 0x7C), self.mode(),
                   uc.mem_read(SWITCHES + 2 * 12 + 17, 1)[0],
                   uc.mem_read(SWITCHES + 3 * 12 + 17, 1)[0],
                   ["%08x" % a for a in self.trace]))

    def mode(self):
        return self.m.uc.mem_read(UI + 0x8B, 1)[0]

    def allocate(self, uc, address, size, data):
        amount = uc.reg_read(UC_ARM_REG_R0)
        pointer = self.next_alloc
        self.next_alloc += (amount + 7) & ~7
        assert self.next_alloc <= 0x3003C000, "probe heap reached reserved patch metadata"
        self.allocated.append((pointer, amount))
        uc.reg_write(UC_ARM_REG_R0, pointer)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def register(self, uc, address, size, data):
        p = uc.reg_read(UC_ARM_REG_R1)
        self.registrations.append(bytes(uc.mem_read(p, 28)))

    def call(self, address, r0=0, r1=0, r2=0, r3=0):
        uc = self.m.uc
        uc.reg_write(UC_ARM_REG_R2, r2)
        uc.reg_write(UC_ARM_REG_R3, r3)
        try:
            result = self.m.call(address, r0, r1, count=3000000)
        except Exception:
            print("failed at PC=%08x" % uc.reg_read(UC_ARM_REG_PC))
            raise
        assert uc.reg_read(UC_ARM_REG_PC) == STOP, "instruction limit reached"
        return result

    def key_lookup(self, group, event):
        self.call(0x0814E818, SCRATCH, UI, group, event)
        return bytes(self.m.uc.mem_read(SCRATCH, 8))


def main():
    paths = [Path("build/firmware.bin"), Path("build/firmware-patched.bin"),
             Path("build/mono_V2.2.0r-patched.syx")]
    before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    total = 0
    for stock, omit_reset in itertools.product((True, False), repeat=2):
        outcomes = {False: set(), True: set()}
        for press_order, release_order, long_hold, with_step, batch in itertools.product(
                ((2, 3), (3, 2)), ((2, 3), (3, 2)), (False, True),
                (False, True), (False, True)):
            p = UiProbe(stock, omit_live_reset=omit_reset)
            for key in press_order:
                p.key(key, 0)
            assert p.mode() == 5, "chord must actually enter unlink"
            if long_hold:
                for key in press_order:
                    p.key(key, 1)
            if with_step:
                p.key(4, 0)
                assert len(p.project_events) == 1
                event = p.project_events[0]
                assert struct.unpack_from("<I", event)[0] == 0x08164244
                assert event[13] == 9, "the trigger must target cutoff"
                p.key(4, 2)
            p.project_events.clear()
            for key in release_order:
                p.key(key, 2, drain=not batch)
            if batch:
                p.call(0x081574F8, UI)
            stuck = stock and batch and not omit_reset
            assert p.mode() == (5 if stuck else 1)
            assert p.m.u32(UI + 0x7C) == 0
            assert len(p.project_events) == (2 if stuck else 0)
            if stuck:
                assert {event[11] for event in p.project_events} == {6, 7}
                assert all(struct.unpack_from("<I", event)[0] == 0x0815EE7C
                           for event in p.project_events)
            outcomes[batch].add((p.mode(), len(p.project_events)))
            total += 1
        print("%s / %s: separated releases %s; batched releases %s" %
              ("stock" if stock else "patched",
               "emulator-only reset omission" if omit_reset else "image dispatch",
               outcomes[False], outcomes[True]))
    for stock, batch in itertools.product((True, False), repeat=2):
        p = UiProbe(stock)
        p.scan({2})
        p.scan({2, 3})
        assert p.mode() == 5
        p.scan({2, 3, 4})
        p.scan({2, 3})
        assert len(p.project_events) == 1
        p.project_events.clear()
        if batch:
            p.scan(set(), drain=False)
            p.call(0x081574F8, UI)
        else:
            p.scan({3})
            p.scan(set())
        stuck = stock and batch
        assert p.mode() == (5 if stuck else 1)
        assert len(p.project_events) == (2 if stuck else 0)
        print("real GPIO scanner, %s, %s release: mode=%d unwanted actions=%d" %
              ("stock" if stock else "patched", "batched" if batch else "separate",
               p.mode(), len(p.project_events)))
        total += 1
    assert before == [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    print("PASS: %d scenarios; stock bug reproduced, patched release fixed; "
          "firmware files unchanged" % total)


if __name__ == "__main__":
    main()
