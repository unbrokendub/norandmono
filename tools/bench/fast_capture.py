"""Host-only ARM MAX replay of the real audio IRQ, with native FP64.

Copies a fully initialized sysemu state. Does not simulate foreground/UI
or advance the sequencer while rendering. Gate/UI preparation remains in
sysemu. The trace trampoline saves ABI registers/flags and copies CVs in
emulated ARM instead of calling Python for every sample. Nothing here is
part of the flashable firmware; do not use its time as MCU cycle timing.
"""
from pathlib import Path
import struct
import subprocess
import sys

import numpy as np
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB
from unicorn.arm_const import (UC_CPU_ARM_MAX, UC_ARM_REG_FPEXC, UC_ARM_REG_FPSCR,
                               UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_R0, UC_ARM_REG_PC)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'tools'))
from patch import thumb_b_w

RAM = ((0, 0x10000), (0x20000000, 0x20000), (0x24000000, 0x80000),
       (0x30000000, 0x50000), (0x38000000, 0x10000))
TRACE, DATA, STUB = 0x10000000, 0x10001000, 0x081C0000


def build_trace():
    out = ROOT/'build/bench/fast'; out.mkdir(parents=True, exist_ok=True)
    src = Path(__file__).with_suffix('.S')
    elf, binary = out/'capture.elf', out/'capture.bin'
    if not binary.exists() or binary.stat().st_mtime_ns < src.stat().st_mtime_ns:
        subprocess.run(['arm-none-eabi-as', str(src), '-o', str(out/'capture.o')], check=True)
        subprocess.run(['arm-none-eabi-ld', '-Ttext=0x081c0000', '-e', 'run_blocks',
                        str(out/'capture.o'), '-o', str(elf)], check=True)
        subprocess.run(['arm-none-eabi-objcopy', '-O', 'binary', str(elf), str(binary)], check=True)
    lines = subprocess.check_output(['arm-none-eabi-nm', str(elf)], text=True).splitlines()
    return binary.read_bytes(), {l.split()[2]: int(l.split()[0], 16) for l in lines if len(l.split()) == 3}


class AudioReplay:
    def __init__(self, mono):
        self.uc = u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        u.ctl_set_cpu_model(UC_CPU_ARM_MAX)
        for a, n in ((0x08000000, 0x200000), *RAM,
                     (0x40000000, 0x10000000), (0x50000000, 0x10000000),
                     (0xE0000000, 0x100000), (TRACE, 0x1000000)):
            u.mem_map(a, n)
        u.mem_write(0x08000000, bytes(mono.uc.mem_read(0x08000000, 0x200000)))
        code, self.sym = build_trace()
        u.mem_write(STUB, code)
        u.mem_write(0x08115040, thumb_b_w(0x08115040, self.sym['capture']))
        u.mem_write(0x081131A2, thumb_b_w(0x081131A2, self.sym['cutoff']))
        u.mem_write(0x1000, b'\x70\x47')
        u.reg_write(UC_ARM_REG_FPEXC, 0x40000000)
        u.reg_write(UC_ARM_REG_SP, 0x10FFF000)
        self.load(mono)

    def load(self, mono):
        for a, n in RAM:
            self.uc.mem_write(a, bytes(mono.uc.mem_read(a, n)))
        self.uc.reg_write(UC_ARM_REG_FPSCR, mono.uc.reg_read(UC_ARM_REG_FPSCR))

    def store(self, mono):
        for a, n in RAM:
            mono.uc.mem_write(a, bytes(self.uc.mem_read(a, n)))

    def render(self, ms):
        blocks = round(ms*4)
        if blocks <= 0 or blocks*8*128 > 0xDFF000:
            raise ValueError('capture duration outside debug buffer')
        u = self.uc
        u.mem_write(TRACE, struct.pack('<II', DATA, 0))
        u.reg_write(UC_ARM_REG_R0, blocks)
        u.reg_write(UC_ARM_REG_LR, 0x1001)
        u.emu_start(self.sym['run_blocks'] | 1, 0x1000, count=blocks*100000)
        assert u.reg_read(UC_ARM_REG_PC) == 0x1000, 'audio replay did not return'
        end = struct.unpack('<I', u.mem_read(TRACE, 4))[0]
        assert end-DATA == blocks*8*128, 'wrong number of DSP samples'
        frames = np.frombuffer(bytes(u.mem_read(DATA, end-DATA)), dtype='<f4').reshape(-1, 32)
        np.testing.assert_allclose(frames[:, 12], np.clip(frames[:, 31], 0, 149), atol=2e-5, rtol=0)
        assert np.isfinite(frames[:, list(range(16))+[30, 31]]).all()
        return frames
