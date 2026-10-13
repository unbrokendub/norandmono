#!/usr/bin/env python3
"""Scale-randomization regression tests against the installed ARM image.

Host stubs isolate allocation and external/project delivery, not the random
handler, scale lookup, UI callbacks or injected code. Run outside macOS's
sandbox when Unicorn cannot read its host CPU cache register.
"""
import struct
import sys
import itertools
import math
import hashlib
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_PC,
                               UC_ARM_REG_LR, UC_ARM_REG_S0, UC_ARM_REG_SP,
                               UC_ARM_REG_R3, UC_ARM_REG_R4, UC_ARM_REG_R5,
                               UC_ARM_REG_R6, UC_ARM_REG_R9, UC_ARM_REG_S9,
                               UC_ARM_REG_S17)
from probe_ui_chord import UiProbe, UI, SCRATCH
from test_patch import stub_return_const
from pathlib import Path

MANAGER = 0x2000C68C
SEQUENCER = 0x2000CFF4
ENGINE = 0x200047F0


class RandomRig(UiProbe):
    def __init__(self, stock=False):
        super().__init__(stock=stock)
        self.outputs = []
        self.random_calls = 0
        self.random_value = 123456789
        self.last_normalized = None
        uc = self.m.uc
        # TEMPO pressed before FUNC legitimately taps the transport timer.
        # Isolate only its physical timer/HCLK source, not the UI action.
        uc.mem_map(0x40000000, 0x1000)
        stub_return_const(self.m, 0x08104F00, 240000000)
        uc.mem_write(UI + 0xEF, bytes([7]))
        uc.mem_write(MANAGER + 0x183, bytes([7, 19]))
        for bank in range(2):
            uc.mem_write(MANAGER + 0x18D + bank * 0x13, bytes([16]))
            for step in range(128):
                head = SEQUENCER + 0x8C + bank * 0x63C + step * 12
                uc.mem_write(head, struct.pack('<III', head, head, 0))
            uc.mem_write(SEQUENCER + 0x8C + bank * 0x63C + 0x614, struct.pack('<H', 16))
        uc.hook_add(UC_HOOK_CODE, self.random, begin=0x08147984, end=0x08147984)
        uc.hook_add(UC_HOOK_CODE, self.submit, begin=0x081576D4, end=0x081576D4)
        uc.hook_add(UC_HOOK_CODE, self.param, begin=0x0811CBBC, end=0x0811CBBC)
        self.m.w32(ENGINE + 0x24, ENGINE + 0x5000)
        for slot in (0, 4, 9):
            self.m.w32(ENGINE + 0x6074 + slot * 36, 0x20013800)
            for bank in range(2):
                self.tuning(bank, slot)
        self.f32(ENGINE + 0x5FCC, 57)
        self.f32(ENGINE + 0x5FD0, 57)
        self.f32(ENGINE + 0x6044, 1)
        self.filter_initialized = False

    def f32(self, addr, value):
        self.m.uc.mem_write(addr, struct.pack('<f', value))

    def param(self, uc, address, size, data):
        self.last_normalized = struct.unpack('<f', struct.pack('<I', uc.reg_read(UC_ARM_REG_S0)))[0]
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def tuning(self, bank, slot, base=57*256, follow=11088, transpose=0):
        self.f32(ENGINE + (0x47C0 if bank else 0x2FB4) + slot * 36, base)
        self.f32(ENGINE + (0x551C if bank else 0x3D10) + slot * 36, follow)
        self.f32(ENGINE + (0x6020 if bank else 0x6004), transpose)

    def scale(self, bank=0, index=1, root=0, degrees=(0, 2, 4, 5, 7, 9, 11)):
        self.m.uc.mem_write(MANAGER + 0x18D + bank * 19 + 4, bytes([index, root]))
        self.m.uc.mem_write(MANAGER + 0x6C + index * 14, bytes([len(degrees), *degrees]))

    def note(self, step, pitch, bank=0, voice=0, modnote=255):
        event = self.next_alloc
        node = event + 24
        self.next_alloc += 40
        # Independently recovered from stock RandNote and note getter.
        self.m.uc.mem_write(event, struct.pack('<IIBBBBHHHBB', 0x081642A4,
            0x081642C0, bank, 0, 0, voice, round(pitch*255), 255, 40000, modnote, 0))
        head = SEQUENCER + 0x8C + bank * 0x63C + step * 12
        last = self.m.u32(head + 4)
        self.m.uc.mem_write(node, struct.pack('<IIB3xI', head, last, 128, event + 4))
        self.m.w32(last, node)
        self.m.w32(head + 4, node)
        self.m.w32(head + 8, self.m.u32(head + 8) + 1)
        return event

    def evaluate(self, offset, note, slot=0, bank=0):
        self.last_normalized = None
        self.f32(ENGINE + 0x604C, bank)
        self.f32(ENGINE + (0x5FD0 if slot == 4 else 0x5FCC), note)
        self.f32(ENGINE + (0x57C8 if bank else 0x3FBC) + slot * 36, offset)
        self.call(0x081156C4, ENGINE, slot, 0)
        assert self.last_normalized is not None, 'stock evaluator was not reached'
        return self.last_normalized * 256

    def section(self, start, end):
        """Execute a bounded stock DSP section with explicit boundary state."""
        uc = self.m.uc
        sp = uc.reg_read(UC_ARM_REG_SP)
        uc.emu_start(start | 1, end, count=10000)
        assert uc.reg_read(UC_ARM_REG_PC) == end, hex(uc.reg_read(UC_ARM_REG_PC))
        assert uc.reg_read(UC_ARM_REG_SP) == sp

    def filter_coordinate(self):
        """Actual stock cutoff render section, after parameter smoothing.

        Not a full audio IRQ: use a settled input smoother, neutral X-Mod,
        X-Env and main filter envelope. Run the factory constructor CALL
        SITE so the crucial range argument is not supplied by our model.
        Return the pre-calibration coordinate written by the DSP itself.
        """
        uc = self.m.uc
        arg = ENGINE + 0x44
        if not self.filter_initialized:
            uc.reg_write(UC_ARM_REG_R9, arg + 0x17A8)
            self.section(0x081474E8, 0x081474F4)
            assert struct.unpack('<f', uc.mem_read(arg + 0x1A40, 4))[0] == 149
            def neutral_envelope(uc, address, size, data):
                assert uc.reg_read(UC_ARM_REG_R0) == arg + 0x1F74
                uc.reg_write(UC_ARM_REG_S0, 0x3F000000)
                uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            uc.hook_add(UC_HOOK_CODE, neutral_envelope, begin=0x0811108C, end=0x0811108C)
            self.filter_initialized = True
        assert self.last_normalized is not None
        self.f32(arg + 0x18CC, self.last_normalized)
        self.m.w32(arg + 0x18DC, 0)  # CParam already at its target
        self.f32(arg + 0x190C, 0.5)  # X-Mod amount neutral
        self.f32(arg + 0x1A2C, 0.5)  # X-Env amount neutral
        uc.reg_write(UC_ARM_REG_R3, arg + 0x2F2C)
        uc.reg_write(UC_ARM_REG_R4, arg)
        uc.reg_write(UC_ARM_REG_R5, arg + 0x2000)
        uc.reg_write(UC_ARM_REG_R6, arg + 0x1000)
        uc.reg_write(UC_ARM_REG_S17, 0)  # main envelope contribution zero
        self.section(0x08112DE2, 0x08112E22)
        # The one-pole smoother must be settled, not bypassed. Its input
        # comes from the genuine /range instructions just executed.
        uc.mem_write(arg + 0x1984, struct.pack('<I', uc.reg_read(UC_ARM_REG_S9)))
        self.section(0x08112E22, 0x081131C6)
        return struct.unpack('<f', uc.mem_read(arg + 0x2EE4, 4))[0]

    def random(self, uc, address, size, data):
        self.random_calls += 1
        uc.reg_write(UC_ARM_REG_R0, self.random_value)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def submit(self, uc, address, size, data):
        ref = uc.reg_read(UC_ARM_REG_R1)
        event = self.m.u32(ref)
        if self.m.u32(event) == 0x081700E0:
            # Delivery boundary only. Run the real per-step handler called
            # by the real range handler, on its own stack/LR/owned event.
            uc.reg_write(UC_ARM_REG_PC, 0x08143B85)
            return
        self.outputs.append(bytes(uc.mem_read(event, 20)))
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def pot(self, group, kind, knob, value):
        event = self.next_alloc
        self.next_alloc += 16
        self.m.uc.mem_write(event, struct.pack('<IBBBxHxx', 0x0815E0C0,
                                               group, kind, knob, value))
        self.m.w32(SCRATCH, event)
        try:
            self.call(0x08155F1C, UI, SCRATCH)
        except AssertionError:
            print('pot PC', hex(self.m.uc.reg_read(UC_ARM_REG_PC)))
            raise

    def generate(self, slot=0, local=0, lower=100, upper=150, step=0, pattern=7):
        self.outputs.clear()
        event = self.next_alloc
        self.next_alloc += 16
        self.m.uc.mem_write(event, struct.pack('<IHBxHBBBBxx', 0x081700E0,
                                               42, pattern, step, slot, local, lower, upper))
        self.m.w32(SCRATCH, event)
        self.call(0x08143B84, MANAGER, SCRATCH)
        assert self.m.u32(SCRATCH) == 0, 'stock handler must consume its input'
        return self.outputs[-1] if self.outputs else None

    def replay_range(self, event):
        self.outputs.clear()
        assert struct.unpack_from('<I', event)[0] == 0x081701D0
        ptr = self.next_alloc
        self.next_alloc += 16
        self.m.uc.mem_write(ptr, bytes(event))
        self.m.w32(SCRATCH, ptr)
        self.call(0x08144E10, MANAGER, SCRATCH)
        assert self.m.u32(SCRATCH) == 0
        return self.outputs


def probe():
    p = RandomRig()
    p.key(26, 0)
    p.key(25, 0)
    print('mask', hex(p.m.u32(UI + 0x7C)), 'mode', p.mode())
    for value in (24000, 38000):
        p.pot(5, 3, 0, value)
        print('pot state', p.m.uc.mem_read(UI + 0xEF, 9).hex())
    p.key(26, 2)
    print('project events', [e.hex() for e in p.project_events])
    p.generate()
    print('random output', [e.hex() for e in p.outputs])
    p.scale()
    p.note(0, 62)
    out = p.generate()
    offset = struct.unpack_from('<h', out, 16)[0]
    print('quantized', offset, 'actual stock evaluator pitch', p.evaluate(offset, 62))


def main():
    # Reach the installed BL by entering the genuine RTTI random handler.
    total = 0
    p = RandomRig()
    for bank, slot, root, degrees in itertools.product((0, 1), (0, 4), range(12),
            ((0, 2, 4, 5, 7, 9, 11), (0, 2, 3, 5, 7, 8, 10), (0, 3, 5, 7, 10), (0, 7))):
        p.scale(bank, 16 if degrees == (0, 7) else 1, root, degrees)
        head = SEQUENCER + 0x8C + bank * 0x63C
        p.m.uc.mem_write(head, struct.pack('<III', head, head, 0))
        note = 48 + (root * 7) % 24
        p.note(0, note, bank)
        for rng in (0, 1, 0x12345678, 0xFFFFFFFF):
            p.random_value = rng
            before = p.random_calls
            out = p.generate(slot=slot, pattern=(7, 19)[bank], lower=80, upper=180)
            assert out is not None
            assert p.random_calls == before + 1
            offset = struct.unpack_from('<h', out, 16)[0]
            assert (80-128)*127 <= offset <= (180-128)*127
            pitch = p.evaluate(offset, note, slot, bank)
            rounded = round(pitch)
            assert abs(pitch-rounded) <= 1.5/256, (pitch, note, root, bank, slot)
            assert (rounded-root) % 12 in degrees, (pitch, root, degrees)
            total += 1
    print('Scale/root/OSC/bank vectors through stock evaluator:', total)
    p = RandomRig()
    # Use the genuine 16 factory records, not a hand-written list of scale
    # names/intervals. 0x0811F85C copies these 224 bytes to manager+0x6B.
    factory = Path('build/firmware.bin').read_bytes()[0x643A0:0x64480]
    p.m.uc.mem_write(MANAGER + 0x6B, factory)
    p.note(0, 59)
    for index, root in itertools.product(range(1, 16), range(12)):
        p.m.uc.mem_write(MANAGER + 0x191, bytes([index, root]))
        out = p.generate()
        assert out is not None
        pitch = p.evaluate(struct.unpack_from('<h', out, 16)[0], 59)
        record = factory[index*14:(index+1)*14]
        assert abs(pitch-round(pitch)) <= 1.5/256
        assert (round(pitch)-root) % 12 in record[2:2+record[1]]
    print('Actual stock factory scale records / every root: 180 OK')
    # An independent stock reader proves our synthetic notes have the real
    # layout (a self-consistent wrong node layout would otherwise pass).
    p = RandomRig()
    p.note(3, 65)
    p.m.w32(p.m.uc.reg_read(UC_ARM_REG_SP), 0)  # fifth ABI arg: note voice
    p.call(0x081203D0, SCRATCH + 0x100, MANAGER, 7, 3)
    payload = bytes(p.m.uc.mem_read(SCRATCH + 0x100, 13))
    assert payload[12] == 1 and struct.unpack_from('<H', payload, 4)[0] == 65*255, payload.hex()
    print('Stock note getter independently recognizes test notes: OK')

    for note in (0, 1, 126, 127):
        p = RandomRig()
        p.note(0, note)
        p.scale(degrees=tuple(range(12)), index=19)
        out = p.generate(lower=0, upper=255)
        assert out is not None, ('MIDI endpoint dropped', note)
        pitch = p.evaluate(struct.unpack_from('<h', out, 16)[0], note)
        assert 0 <= pitch <= 127 and abs(pitch-round(pitch)) <= 1.5/256
    print('MIDI endpoints (including stock float rounding at 127): 4 OK')

    # Stock evaluator is the oracle, including fractional and reversed
    # FOLLOW, non-default coarse tuning, and pattern transpose.
    p = RandomRig()
    p.scale(root=3)
    p.note(0, 65)
    n = 0
    for slot, bank, tuning, tracking, transpose in itertools.product((0, 4), (0, 1),
            (54*256, 57*256+101), (0, 5500, 8250, 11088, -11088, 14000), (-7, 0, 5)):
        p.scale(bank, root=3)
        head = SEQUENCER + 0x8C + bank * 0x63C
        p.m.uc.mem_write(head, struct.pack('<III', head, head, 0))
        p.note(0, 65, bank)
        p.tuning(bank, slot, tuning, tracking, transpose)
        out = p.generate(slot=slot, pattern=(7, 19)[bank], lower=0, upper=255)
        assert out is not None, (slot, bank, tuning, tracking, transpose)
        pitch = p.evaluate(struct.unpack_from('<h', out, 16)[0], 65, slot, bank)
        assert abs(pitch-round(pitch)) <= 1.5/256, (pitch, tracking, bank)
        assert (round(pitch)-3) % 12 in (0, 2, 4, 5, 7, 9, 11)
        n += 1
    print('Non-default tuning/FOLLOW/transpose against stock evaluator:', n)

    # The default chromatic mode and EVERY other slot/local preserve stock
    # bytes and exactly one RNG call and one 20-byte output allocation.
    stock, patched = RandomRig(True), RandomRig()
    count = 0
    for slot, local, rng in itertools.product(range(19), range(9), (0, 0xFFFFFFFF)):
        for rig in (stock, patched):
            rig.scale(index=0 if (slot in (0, 4, 9) and local == 0) else 1)
            rig.random_value = rng
        before = patched.random_calls
        allocated = len(patched.allocated)
        a, b = stock.generate(slot, local), patched.generate(slot, local)
        assert a == b, (slot, local, a, b)
        assert patched.random_calls == before + 1
        assert [size for _, size in patched.allocated[allocated:]] == [20]
        count += 1
    print('Unchanged parameter/chromatic A/B vectors:', count)

    p = RandomRig()
    p.scale(root=2, index=19, degrees=(0, 3, 7))
    p.note(2, 62)
    p.note(12, 67)
    p.note(13, 71, modnote=0)  # a mod note must not masquerade as a pitch
    for step, note in ((2, 62), (3, 62), (12, 67), (15, 67), (0, 67)):
        out = p.generate(step=step)
        pitch = p.evaluate(struct.unpack_from('<h', out, 16)[0], note)
        assert abs(pitch-round(pitch)) <= 1.5/256
        assert (round(pitch)-2) % 12 in (0, 3, 7)
    print('Custom scale, rests, cyclic wrap, mod-note exclusion: OK')

    # Narrow/empty/malformed inputs cannot silently emit an out-of-scale
    # value, escape the knob range, add allocations, or get stuck in a list.
    for kind in ('narrow', 'empty', 'count', 'degree', 'root', 'index', 'step',
                 'length', 'nan', 'pointer', 'cycle', 'reversed'):
        p = RandomRig()
        p.scale(degrees=(0,))
        lower, upper, step = 100, 150, 0
        if kind == 'narrow': lower = upper = 128  # held A3, but C-only scale
        elif kind == 'empty': p.scale(degrees=())
        elif kind == 'count': p.m.uc.mem_write(MANAGER + 0x6C + 14, b'\x0d')
        elif kind == 'degree': p.scale(degrees=(12,))
        elif kind == 'root': p.scale(root=255)
        elif kind == 'index': p.m.uc.mem_write(MANAGER + 0x191, b'\xff')
        elif kind == 'step': step = 128
        elif kind == 'length': p.m.uc.mem_write(MANAGER + 0x18D, b'\x00')
        elif kind == 'nan': p.f32(ENGINE + 0x2FB4, math.nan)
        elif kind == 'pointer': p.m.w32(SEQUENCER + 0x8C, 0xFFFFFFFC)
        elif kind == 'cycle':
            event = p.note(0, 57)
            p.m.w32(event + 24, event + 24)
        elif kind == 'reversed': lower, upper = 150, 100
        n = len(p.allocated)
        assert p.generate(lower=lower, upper=upper, step=step) is None, kind
        assert [size for _, size in p.allocated[n:]] == [20], kind
    print('No-legal-note and malformed-input guards: 12 OK')

    # Real UI press/pot/release -> captured queue event -> real range loop
    # -> real per-step handler -> installed patch. Both oscillators and
    # both initial press orders; control values are actual 16-bit pot data.
    for slot, order in itertools.product((0, 4), ((26, 25), (25, 26))):
        p = RandomRig()
        p.scale(root=1)
        for step in range(16): p.note(step, 48 + step)
        for key in order: p.key(key, 0)
        p.pot(5, 3, slot, 24000)
        p.pot(5, 3, slot, 38000)
        p.key(26, 2)
        assert len(p.project_events) == 1
        event = p.project_events[0]
        assert event[6:13] == bytes([7, 0, 15, slot, 0, 93, 148]), event.hex()
        before = p.random_calls
        outputs = p.replay_range(event)
        assert len(outputs) == 16 and p.random_calls == before + 16
        for step, out in enumerate(outputs):
            assert out[9] == step and out[13] == slot and out[14] == 0
            pitch = p.evaluate(struct.unpack_from('<h', out, 16)[0], 48+step, slot)
            assert abs(pitch-round(pitch)) <= 1.5/256 and (round(pitch)-1) % 12 in (0, 2, 4, 5, 7, 9, 11)
        p.key(25, 2)
        assert p.m.u32(UI + 0x7C) == 0
    print('Real FUNC+TEMPO + pot + release -> full 16-step randomizer: 4 OK')
    # Selected subrange uses the very same stock range loop.
    event = bytearray(event)
    event[7:9] = bytes([4, 9])
    out = p.replay_range(event)
    assert [e[9] for e in out] == list(range(4, 10))
    assert all(e[13] == 4 and e[14] == 0 for e in out)
    print('Selected step subrange remains inclusive: OK')

    # Independent unit check: let the actual SequencerEvent playback emit
    # its ControllerEvent; do not assume the serialized offset's scaling.
    controls = []
    def controller(uc, address, size, data):
        ref = uc.reg_read(UC_ARM_REG_R1)
        ptr = p.m.u32(ref)
        controls.append(bytes(uc.mem_read(ptr, 12)))
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
    p.m.uc.hook_add(UC_HOOK_CODE, controller, begin=0x0815748C, end=0x0815748C)
    for output in out:
        ptr = p.next_alloc
        p.next_alloc += 16
        p.m.uc.mem_write(ptr, struct.pack('<II', 0x081642D8, 0x081642F4) + output[10:18])
        p.call(0x0811DCD0, ptr, 0)
        ctrl = controls[-1]
        assert ctrl[5:8] == bytes([6, 4, 0])
        assert abs(struct.unpack_from('<f', ctrl, 8)[0] - struct.unpack_from('<h', output, 16)[0]) < 0.005
    print('Actual sequencer playback confirms raw automation units: OK')

    test_cutoff()


def test_cutoff():
    factory = Path('build/firmware.bin').read_bytes()[0x643A0:0x64480]
    p = RandomRig()
    p.m.uc.mem_write(MANAGER + 0x6B, factory)
    for bank in (0, 1):
        p.note(0, 59, bank)
        p.tuning(bank, 9, base=69*256, follow=12900)
    count = 0
    for bank, index, root, master in itertools.product((0, 1), range(1, 16),
                                                     range(12), (0, 0.37, 0.5, 1)):
        p.m.uc.mem_write(MANAGER + 0x191 + bank*19, bytes([index, root]))
        p.f32(ENGINE + 0x6044, master)
        p.random_value = (index * 65537 + root * 19) & 0xFFFFFFFF
        before = p.random_calls
        allocated = len(p.allocated)
        out = p.generate(slot=9, pattern=(7, 19)[bank], lower=40, upper=210)
        assert out is not None, (bank, index, root, master)
        assert p.random_calls == before + 1
        assert [size for _, size in p.allocated[allocated:]] == [20]
        offset = struct.unpack_from('<h', out, 16)[0]
        assert (40-128)*150 <= offset <= (210-128)*150
        coarse = p.evaluate(offset, 59, 9, bank)
        coordinate = p.filter_coordinate()
        assert abs(coordinate-coarse) < 0.001, (coordinate, coarse)
        assert abs(coordinate-round(coordinate)) <= 1.5/256
        record = factory[index*14:(index+1)*14]
        assert (round(coordinate)-12-root) % 12 in record[2:2+record[1]]
        count += 1
    print('Cutoff: real factory scales/root/bank/master FOLLOW -> stock DSP:', count)

    # Independent measured-frequency coordinate anchors, read from stock
    # calibration: filter adds 81, OSC adds 69 to 12*ln(hz/440)/ln(2).
    image = Path('build/firmware.bin').read_bytes()
    assert struct.unpack_from('<d', image, 0x10768)[0] == 440
    assert struct.unpack_from('<d', image, 0x10770)[0] == 0.6931471806
    assert struct.unpack_from('<d', image, 0x10778)[0] == 81
    assert struct.unpack_from('<d', image, 0x105C0)[0] == 69
    # Include the entire stock cutoff coordinate range, not just MIDI
    # 0..127. This verifies the /149 constructor argument and *149 render.
    p = RandomRig()
    for coordinate in (0, 6, 12, 81, 127, 137, 149):
        p.scale(index=19, root=coordinate % 12, degrees=(0,))
        p.tuning(0, 9, base=coordinate*256, follow=0)
        out = p.generate(slot=9, lower=128, upper=128)
        assert out is not None, coordinate
        assert struct.unpack_from('<h', out, 16)[0] == 0
        p.evaluate(0, 57, 9)
        actual = p.filter_coordinate()
        assert abs(actual-coordinate) < 0.001, (actual, coordinate)
    print('Cutoff: stock calibration anchors and full-range endpoints: 7 OK')

    p = RandomRig()
    for bank in (0, 1):
        p.note(0, 65, bank)
        p.scale(bank, root=3, index=19, degrees=(0, 3, 7))
    count = 0
    for bank, base, tracking, master, transpose in itertools.product((0, 1),
            (57*256+101, 89*256+7, 129*256), (0, 6450, 9675, 12900, -12900, 16125),
            (0, 0.37, 1), (-7, 0, 5)):
        p.tuning(bank, 9, base, tracking, transpose)
        p.f32(ENGINE + 0x6044, master)
        out = p.generate(slot=9, pattern=(7, 19)[bank], lower=0, upper=255)
        assert out is not None, (bank, base, tracking, master, transpose)
        p.evaluate(struct.unpack_from('<h', out, 16)[0], 65, 9, bank)
        coordinate = p.filter_coordinate()
        assert abs(coordinate-round(coordinate)) <= 1.5/256, (
            coordinate, bank, base, tracking, master, transpose)
        assert (round(coordinate)-3) % 12 in (0, 3, 7)
        count += 1
    print('Cutoff: varied base/local FOLLOW/master FOLLOW/transpose -> stock DSP:', count)

    # Invalid filter state must not produce an unquantized replacement.
    p = RandomRig()
    p.scale(index=19, degrees=(0,))
    for master in (math.nan, math.inf, -0.1, 1.1):
        p.f32(ENGINE + 0x6044, master)
        assert p.generate(slot=9) is None
    p.f32(ENGINE + 0x6044, 0)
    p.tuning(0, 9, base=81*256, follow=12900)
    assert p.generate(slot=9, lower=128, upper=128) is None  # A but C-only
    p.scale(index=19, degrees=())
    assert p.generate(slot=9) is None
    print('Cutoff: invalid master, empty scale, no legal value guards: 6 OK')

    for order in ((26, 25), (25, 26)):
        p = RandomRig()
        p.scale(root=1)
        p.tuning(0, 9, base=69*256, follow=12900)
        p.f32(ENGINE + 0x6044, 0.37)
        for step in range(16): p.note(step, 48 + step)
        for key in order: p.key(key, 0)
        p.pot(5, 3, 9, 24000)
        p.pot(5, 3, 9, 38000)
        p.key(26, 2)
        assert len(p.project_events) == 1
        event = p.project_events[0]
        assert event[6:13] == bytes([7, 0, 15, 9, 0, 93, 148]), event.hex()
        outputs = p.replay_range(event)
        assert len(outputs) == 16
        for step, out in enumerate(outputs):
            assert out[9] == step and out[13:15] == bytes([9, 0])
            p.evaluate(struct.unpack_from('<h', out, 16)[0], 48+step, 9)
            coordinate = p.filter_coordinate()
            assert abs(coordinate-round(coordinate)) <= 1.5/256
            assert (round(coordinate)-1) % 12 in (0, 2, 4, 5, 7, 9, 11)
        p.key(25, 2)
        assert p.m.u32(UI + 0x7C) == 0
        subset = bytearray(event)
        subset[7:9] = bytes([4, 9])
        assert [e[9] for e in p.replay_range(subset)] == list(range(4, 10))
    print('Cutoff: real FUNC+TEMPO+knob UI, 16 steps and subrange, both orders: 2 OK')

    # Compare OSC output with the preserved oscillator-only release, not
    # stock or our own formula. Keep host allocation/hardware stubs, load
    # that release's injected code and actual BL redirect into a second VM.
    # A clean build may not have the historical release in build/. The
    # frozen output digest below is independently captured from that exact
    # release; it keeps the regression meaningful without a binary fixture.
    reference_path = Path('build/firmware-scale-random.bin')
    previous, current = None, RandomRig()
    if reference_path.exists():
        reference = reference_path.read_bytes()
        assert hashlib.sha256(reference).hexdigest() == (
            '6bf40f60ce8b997a508462d90308b21e7438490bb1d7370f55603663721bc404')
        previous = RandomRig()
        previous.m.uc.mem_write(0x08175200, reference[0x75200:])
        previous.m.uc.mem_write(0x08143C14, reference[0x43C14:0x43C18])
    rigs = (previous, current) if previous else (current,)
    for rig in rigs:
        for bank in (0, 1): rig.note(0, 65, bank)
    count = 0
    digest = hashlib.sha256()
    for bank, slot, root, tracking, rng in itertools.product((0, 1), (0, 4),
            range(12), (0, 5500, 11088, -11088), (0, 0xFFFFFFFF)):
        for rig in rigs:
            rig.scale(bank, root=root)
            rig.tuning(bank, slot, 57*256+101, tracking, -7)
            rig.f32(ENGINE + 0x6044, math.nan)  # OSC must ignore master FOLLOW
            rig.random_value = rng
        b = current.generate(slot=slot, pattern=(7, 19)[bank])
        if previous:
            a = previous.generate(slot=slot, pattern=(7, 19)[bank])
            assert a == b, (bank, slot, root, tracking, rng, a, b)
        digest.update(b'\x00' if b is None else b'\x01' + b)
        count += 1
    assert digest.hexdigest() == '29d84c65af09038c3142cf9bd012e1c5790c3ca02454f35df05c0287bcbb2f85'
    print('OSC outputs byte-identical to preserved oscillator-only release:', count)


if __name__ == '__main__':
    if '--probe' in sys.argv:
        probe()
    else:
        main()
