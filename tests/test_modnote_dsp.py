"""Decoded mod-note parameter -> real evaluator -> complete DSP regression.

Not a full project loader, interrupt scheduler, or cycle-accurate hardware
test. Tests the numeric compatibility boundary with real stock constructors,
all 171 CParams and the entire FP64 waveform/CV mapper left live.
"""
import argparse
import random
import struct
from collections import deque
from pathlib import Path
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_BLOCK, UC_HOOK_MEM_INVALID
from unicorn.arm_const import *
from test_patch import Machine, BASE, BLOB, STOP, stub_return_const

ENGINE = 0x200047F0
ARG = ENGINE + 0x44


class Rig:
    def __init__(self, image=None):
        self.m = Machine()
        if image:
            self.m.img = Path(image).read_bytes()
            self.m.reset()
        # Unicorn's M7 omits FP64. MAX executes this DSP's FPv5-D16,
        # including the real waveform-to-CV mapper and VRINTA.
        old = self.m.uc
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.uc.ctl_set_cpu_model(UC_CPU_ARM_MAX)
        for begin, end, perms in old.mem_regions():
            self.uc.mem_map(begin, end-begin+1, perms)
            self.uc.mem_write(begin, bytes(old.mem_read(begin, end-begin+1)))
        self.m.uc = self.uc
        self.uc.reg_write(UC_ARM_REG_FPEXC, 0x40000000)
        self.uc.reg_write(UC_ARM_REG_SP, 0x2001F000)
        self.trace = deque(maxlen=20)
        self.uc.hook_add(UC_HOOK_BLOCK, self.track)
        self.uc.hook_add(UC_HOOK_MEM_INVALID, self.invalid)
        master = self.m.u32(BLOB+0x120)
        self.uc.mem_write(0x2000E540, bytes(self.uc.mem_read(master, 36)))
        self.m.call(0x081473F4, ARG, count=200000)
        assert self.uc.reg_read(UC_ARM_REG_PC) == STOP
        self.m.w32(ENGINE+0x24, ENGINE+0x5000)
        # Restore System::instance (Machine isolates it for boot tests).
        self.uc.mem_write(0x08109614, self.m.img[0x9614:0x961C])
        self.m.w32(0x2000077C + (0x690+10)*4 + 4, 0x2001C400)
        self.m.w32(0x2001C41C, 123456789)  # real xorshift state
        stub_return_const(self.m, 0x081029CC, 0)  # GPIO output only
        self.f32(ENGINE+0x5FCC, 57)
        self.f32(ENGINE+0x5FD0, 57)
        self.f32(ENGINE+0x6044, 1)

    def track(self, uc, addr, size, data):
        self.trace.append(addr)

    def invalid(self, uc, access, address, size, value, data):
        print('invalid memory', access, hex(address), size,
              'PC', hex(uc.reg_read(UC_ARM_REG_PC)), flush=True)
        return False

    def f32(self, addr, v):
        self.uc.mem_write(addr, struct.pack('<f', v))

    def call(self, addr, r0=0, r1=0, r2=0):
        self.uc.reg_write(UC_ARM_REG_R2, r2)
        try:
            self.m.call(addr, r0, r1, count=100000)
            assert self.uc.reg_read(UC_ARM_REG_PC) == STOP
        except Exception:
            print('trace', [hex(a) for a in self.trace], flush=True)
            raise

    def audio(self):
        sp = self.uc.reg_read(UC_ARM_REG_SP)
        self.call(0x08112488, ARG)
        assert self.uc.reg_read(UC_ARM_REG_SP) == sp

    def modnote(self, rng):
        # Decoded live mod-note banks, not the ordinary patch arrays.
        for bank, offset in enumerate((0x4268, 0x5A74)):
            self.uc.mem_write(ENGINE+(0x600D if bank else 0x5FF1), b'\x01')
            for slot in range(19):
                for local in range(9):
                    value = rng.randrange((5, 4, 2)[local-6]) if local >= 6 else rng.choice((0, 1, 32767, 32768, 65534, 65535, rng.randrange(65536)))
                    self.f32(ENGINE+offset+slot*36+local*4, value)
        self.f32(ENGINE+0x604C, rng.choice((0, 0.5, 1)))
        for slot in range(19):
            for local in range(9):
                self.call(0x081156C4, ENGINE, slot, local)
        self.uc.mem_write(ARG+0x2D, b'\x01')
        self.uc.mem_write(ARG+0x33, b'\x01')
        self.call(0x08116CB8, ENGINE)
        for slot in (*range(13), 17):
            owner = self.m.u32(ARG+0x2E68+slot*4)
            self.uc.mem_write(owner+0x21D, b'\x01')


def run(compare=None):
    rigs = [Rig()]
    if compare: rigs.append(Rig(compare))
    rngs = [random.Random(140926) for _ in rigs]
    for p in rigs:
        for _ in range(2): p.audio()
    for case in range(100):
        for p, rng in zip(rigs, rngs): p.modnote(rng)
        for tick in range(100):
            for p in rigs: p.audio()
            if compare:
                for start, size in ((ARG, 0x2F70), (0x2000E580, 21*32)):
                    assert rigs[0].uc.mem_read(start, size) == rigs[1].uc.mem_read(start, size), (case, tick, hex(start))
    print('Decoded mod-note banks, 171 parameters, endpoints/morph/19 slots, complete DSP: 100 x 100 frames OK')
    if compare: print('DSP outputs/state and all 21 RC entries are bit-identical to', compare)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--compare')
    args = parser.parse_args()
    run(args.compare)
