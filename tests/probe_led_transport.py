#!/usr/bin/env python3
"""Read-only firmware audit: LED SPI status handling and dirty publication.

This executes the actual stock LED sender in a freshly booted image. The
status cases are fault injection, NOT a timed model of SPI or reproduction
of a physical stall. The dirty race injects the full SysTick handler between
two main-thread stores; peripheral timing is still not modelled.
"""
import hashlib
import struct
import sys
from pathlib import Path

from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_LR
import sysemu

SPI = 0x40013400
SEND = 0x08109FD0
SET = 0x0810A14C
INDEX = 60


def word(m, a):
    return struct.unpack('<I', m.uc.mem_read(a, 4))[0]


def main(path):
    original = Path(path).read_bytes()
    print('image', path, 'SHA256', hashlib.sha256(original).hexdigest(), flush=True)
    m = sysemu.Mono(path).boot()
    m.settle(10)
    system = m.call_preserving(0x08109614)
    led = m.call_preserving(0x0810977C, system, 4)
    uc = m.uc
    print('system=%08x led=%08x' % (system, led), flush=True)
    for base, offsets in ((SPI, (0, 4, 8, 12, 16, 20)),
                          (sysemu.RCC, (0, 0x10, 0x18, 0x1C, 0x20, 0x28, 0x2C,
                                       0x30, 0x34, 0x38, 0x3C, 0x40, 0x44, 0x50)),
                          (0xE000ED00, (0x18, 0x1C, 0x20))):
        print('registers', ' '.join('%08x=%08x' % (base + off, word(m, base + off))
                                    for off in offsets), flush=True)
    print('NVIC priorities EXTI2=%02x DMA1_S0=%02x TIM5=%02x SysTick=%02x' % (
        uc.mem_read(0xE000E400 + 8, 1)[0], uc.mem_read(0xE000E400 + 11, 1)[0],
        uc.mem_read(0xE000E400 + 50, 1)[0], uc.mem_read(0xE000ED23, 1)[0]), flush=True)
    samples = []
    h = uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: samples.append(a),
                    begin=0x08112488, end=0x08112488)
    m.run_unmasked()
    assert m.inject(sysemu.VEC_EXTI2)
    uc.hook_del(h)
    assert len(samples) == 8
    print('one actual EXTI2 handler invokes the DSP update %d times' % len(samples), flush=True)

    # Keep the fixture's LED isolated from the normal 1 kHz UI renderer.
    uc.mem_write(sysemu.UI + 0x8F, b'\x02')
    uc.mem_write(sysemu.UI + 0x91, b'\x00')
    cell = led + 0x94 + INDEX * 20

    def seed(dirty):
        for i in range(70):
            uc.mem_write(led + 0x94 + i * 20, bytes(20))
        # Zero is exactly stable under the stock float smoothing; 10 can
        # round to 9.999999 and legitimately retain dirty for another scan.
        uc.mem_write(cell, bytes([0, 0, 0, dirty]) + bytes(16))
        uc.mem_write(led + 0x60C, bytes([INDEX - 1]))
        uc.mem_write(led + 0x90, b'\x80')

    # Does the driver inspect EOT/TXC/TXP/MODF, or re-enable/reset SPI?
    # Leave the post-boot configuration in place except for the declared
    # SR/CR1 fault inputs. MMIO is RAM; no wire-level success is asserted.
    accesses = []
    def trace(uc, access, a, size, value, data):
        accesses.append((access, a, size, value))
    h = uc.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, trace, begin=SPI, end=SPI + 0x23)
    boot_cr1 = word(m, SPI) & ~0x200
    p6 = word(m, SPI + 12) == 0x07420060
    for name, status, enabled in (('complete', 0x100A, True), ('busy', 0, True),
                                   ('mode_fault', 0x200, False)):
        m.run_unmasked()
        seed(1)
        m.rules[SPI + 0x14] = status
        uc.mem_write(SPI, struct.pack('<I', boot_cr1 if enabled else boot_cr1 & ~1))
        if p6:
            uc.mem_write(0x2000E400 + 0xD48, bytes(4))
            uc.mem_write(0x2000E400 + 0xD50, struct.pack('<I', 0x80000000 | INDEX))
        accesses.clear()
        old_bytes = len(m.led_bytes)
        m.call_preserving(SEND, led)
        sr_reads = [x for x in accesses if x[0] == sysemu.UC_MEM_READ and x[1] == SPI + 0x14]
        commands = m.led_bytes[old_bytes:]
        assert bool(sr_reads) == p6
        denied = p6 and name != 'complete'
        assert len(commands) == (0 if denied else 3)
        if commands: assert commands[0] == 0x80
        assert uc.mem_read(cell + 3, 1)[0] == int(denied)
        assert bool(word(m, SPI) & 0x200) == (not denied)
        assert bool(word(m, SPI) & 1) == enabled
        print('%s: status_reads=%d, TXDR=%s, SPE=%d, dirty=%d' % (
            name, len(sr_reads), bytes(commands).hex(), enabled, denied), flush=True)
    uc.hook_del(h)
    uc.mem_write(SPI, struct.pack('<I', boot_cr1))
    m.rules[SPI + 0x14] = 0x101A

    # Patch M reorders the setter's two stores so the brightness is published
    # before the dirty flag.  On a stock image the middle boundary loses the
    # update; on a patched one no boundary may.
    patched = bytes(uc.mem_read(0x0810A166, 8)) == bytes.fromhex('80f8942080f89730')
    print('brightness setter: %s' % ('value then dirty (patch M)' if patched
                                     else 'dirty then value (stock order)'), flush=True)

    # Actual main setter interrupted by actual SysTick. Stop before each
    # boundary; injected ISR uses its own stacked frame in m.inject().
    for boundary in (0x0810A166, 0x0810A16A, 0x0810A16E):
        m.run_unmasked()
        seed(0)
        saved = uc.context_save()  # process-local only; never persisted
        uc.reg_write(UC_ARM_REG_R0, led)
        uc.reg_write(UC_ARM_REG_R1, INDEX)
        uc.reg_write(UC_ARM_REG_R2, 42)
        uc.reg_write(UC_ARM_REG_LR, sysemu.STOP | 1)
        stop = uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: u.emu_stop(), begin=boundary, end=boundary)
        uc.emu_start(SET | 1, sysemu.STOP, count=1000)
        uc.hook_del(stop)
        assert uc.reg_read(UC_ARM_REG_PC) == boundary
        assert m.inject(sysemu.VEC_SYSTICK)
        uc.emu_start(boundary | 1, sysemu.STOP, count=1000)
        assert uc.reg_read(UC_ARM_REG_PC) == sysemu.STOP
        brightness, dirty = uc.mem_read(cell, 1)[0], uc.mem_read(cell + 3, 1)[0]
        assert brightness == 42
        assert dirty == (0 if boundary == 0x0810A16A and not patched else 1)
        if boundary == 0x0810A16A and not patched:
            # An identical subsequent setter does not repair the lost flag.
            uc.reg_write(UC_ARM_REG_R2, 42)
            m.call(SET, led, INDEX)
            assert uc.mem_read(cell + 3, 1)[0] == 0
            assert struct.unpack('<f', uc.mem_read(cell + 12, 4))[0] == 0.0
        uc.context_restore(saved)
        print('SysTick before %08x: brightness=%d dirty=%d %s' % (
            boundary, brightness, dirty, 'LOST UPDATE' if not dirty else 'control OK'), flush=True)
    assert Path(path).read_bytes() == original
    print('Expectations passed for this image; no hardware stall reproduced.', flush=True)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'build/firmware-patched.bin')
