#!/usr/bin/env python3
"""Boot the real image, fingerprint LED init, replay actual sender on timed SPI.

No saved Unicorn contexts, image edits, physical I/O, or erratum injection.
CPU instruction-to-time conversion is a declared sweep, NOT measured cycles.
Wire bit/byte timing derives from the image's actual RCC/SPI configuration.
"""
import hashlib
import struct
import sys
from pathlib import Path
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_MEM_WRITE
from unicorn.arm_const import UC_ARM_REG_PC
import sysemu
from spi_led_model import SPI, CS_BSRR, SpiTx, replay, self_test


def u32(uc, addr):
    return struct.unpack('<I', uc.mem_read(addr, 4))[0]


def main(path):
    self_test()
    original = Path(path).read_bytes()
    print('image', path, 'SHA256', hashlib.sha256(original).hexdigest(), flush=True)
    m = sysemu.Mono(path)
    uc = m.uc
    init_frames, frame = [], bytearray()
    selected = False
    runtime_configs = []

    def init_write(uc, access, addr, size, value, data):
        nonlocal selected, frame
        if addr == CS_BSRR:
            if value & 8:
                if frame:
                    init_frames.append(bytes(frame))
                frame = bytearray()
                selected = False
            if value & (8 << 16):
                selected = True
        elif addr == SPI + 0x20 and selected:
            frame.extend(value.to_bytes(size, 'little'))
        elif addr == SPI + 12 and value == 0x07420060:
            assert not (u32(uc, SPI) & 1), 'CFG2 changed with SPE enabled'
            assert not selected, 'mode changed while CS selected'
            runtime_configs.append(value)

    hooks = [uc.hook_add(UC_HOOK_MEM_WRITE, init_write, begin=a, end=a)
             for a in (CS_BSRR, SPI + 0x20, SPI + 12)]
    m.boot()
    if frame:
        init_frames.append(bytes(frame))
    for h in hooks:
        uc.hook_del(h)
    # Exact multi-register protocol fingerprint, not an ID read or board BOM.
    expected = ['cc', '3100', '3207', '4107', '42ffffff0f', '3502',
                '3600', '373f', '3400', '3503']
    for item in expected:
        assert bytes.fromhex(item) in init_frames, ('missing init command', item)
    ram = [f for f in init_frames if f[0] in (0x80, 0x82, 0x84, 0x86, 0x88)]
    assert {f[0] for f in ram} == {0x80, 0x82, 0x84, 0x86, 0x88}
    display = [f for f in ram if f[0] == 0x80]
    assert sorted((f[1], len(f) - 2) for f in display) == [(i * 32, 28) for i in range(8)]
    print('boot protocol fingerprint:', ' '.join(expected),
          'RAM opcodes:', sorted({hex(f[0]) for f in ram}), flush=True)
    print('boot RAM frame lengths:', sorted({len(f) for f in ram}), flush=True)
    regs = {o: u32(uc, SPI + o) for o in (0, 4, 8, 12)}
    p6 = regs[12] == 0x07420060
    assert len(runtime_configs) == int(p6)
    rcc = {o: u32(uc, sysemu.RCC + o) for o in (0x28, 0x38, 0x3C, 0x50)}
    assert (rcc[0x50] >> 16) & 7 == 1, 'SPI45 must use PLL2Q'
    assert rcc[0x28] & 3 == 2, 'PLL source must be HSE'
    assert rcc[0x3C] == 0, 'fractional PLL not modelled'
    hse = struct.unpack('<f', uc.mem_read(0x08104CA8, 4))[0]
    divm = (rcc[0x28] >> 12) & 63
    n = (rcc[0x38] & 511) + 1
    q = ((rcc[0x38] >> 16) & 127) + 1
    kernel = hse / divm * n / q
    assert kernel == 40e6
    print('HSE=%g PLL2 M=%d N=%d Q=%d kernel=%g registers=%s' %
          (hse, divm, n, q, kernel, regs), flush=True)

    system = m.call_preserving(0x08109614)
    led = m.call_preserving(0x0810977C, system, 4)
    # Isolate exactly one dirty LED at a converged brightness of zero.
    for i in range(70):
        uc.mem_write(led + 0x94 + 20 * i, bytes(20))
    uc.mem_write(led + 0x94 + 20 * 60 + 3, b'\x01')
    uc.mem_write(led + 0x60C, b'\x3b')
    uc.mem_write(led + 0x90, b'\x80')
    uc.mem_write(SPI, struct.pack('<I', regs[0] & ~0x200))
    # The newer p5 image has a DWT-based inter-transaction guard. This
    # isolated trace starts with the previous transaction long finished.
    # It audits byte spacing, not that guard's time-dependent branches.
    guarded = original[0xA002:0xA006] != bytes.fromhex('10b50122')
    if guarded:
        uc.mem_write(0x2000E400 + 0x48, struct.pack('<I', 1))
        uc.mem_write(0x2000E400 + 0xD48, struct.pack('<I', 0))
        uc.mem_write(0xE0001004, struct.pack('<I', 400000))
    # Real write-only IFCR reads return zero, not the previous RAM write.
    m.rules[SPI + 0x18] = 0
    trace, ordinal = [], 0

    def instruction(uc, addr, size, data):
        nonlocal ordinal
        ordinal += 1

    def access(uc, kind, addr, size, value, data):
        trace.append((ordinal, uc.reg_read(UC_ARM_REG_PC),
                      'W' if kind == UC_MEM_WRITE else 'R', addr, size,
                      value if kind == UC_MEM_WRITE else u32(uc, addr)))

    hooks = [uc.hook_add(UC_HOOK_CODE, instruction),
             uc.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, access,
                         begin=SPI, end=SPI + 0x23),
             uc.hook_add(UC_HOOK_MEM_WRITE, access, begin=CS_BSRR, end=CS_BSRR)]
    m.call_preserving(0x08109FD0, led)
    for h in hooks:
        uc.hook_del(h)
    assert any(e[2] == 'R' and e[3] == SPI + 0x14 for e in trace) == p6
    payload = bytes(e[5] for e in trace if e[2] == 'W' and e[3] == SPI + 0x20)
    assert payload == b'\x80\x4d\x00', payload.hex()
    assert uc.mem_read(led + 0x94 + 20 * 60 + 3, 1)[0] == 0
    print('actual sender MMIO trace (instruction ordinal, PC, direction, address, value):')
    for insn, pc, kind, addr, size, val in trace:
        print('%3d %08x %s %08x/%d %08x' % (insn, pc, kind, addr, size, val))

    # Run ordinary 4 kHz traffic with a range of explicit CPU timing
    # assumptions. None of these is an MCU performance measurement.
    for cpi in (1, 2, 4, 8, 16, 32):
        s = SpiTx(regs, kernel)
        for i in range(16):
            replay(s, trace, 10000 + i * 250000, cpi * 2.5)
        s.advance(s.now + 20000)
        s.wire.cs(s.now, True)
        assert s.wire.frames == [(24, payload)] * 16
        assert s.wire.gaps == ([2800] if p6 else [400]) * 32
        assert s.wire.violations['byte gap < 2 us (relaxed bound)'] == (0 if p6 else 32)
        assert not s.log['CS raised during transfer']
        assert len(s.completions) == 16
        assert [e - b for b, e in zip(s.starts, s.completions)] == [14400 if p6 else 9600] * 16
        if p6:
            assert not any(s.wire.violations.values()) and not s.log
            assert min(s.launch_gaps) == 2600
        print('nominal cpi=%2d: 16 EOT, byte gap=%d ns; CS high=%g ns; violations=%s' %
              (cpi, s.wire.gaps[0], s.wire.high_widths[0], dict(s.wire.violations)), flush=True)

    # Positive control: hypothetical mode 3 (matches Holtek diagrams),
    # MIDI=6, guaranteed CS high >=200 ns, normal 250 us scheduling.
    # This changes ONLY the model configuration/schedule, not firmware.
    corrected = {**regs, 12: (regs[12] & ~0x030000F0) | 0x03000060}
    s = SpiTx(corrected, kernel)
    for i in range(16):
        replay(s, trace, 10000 + i * 250000, 2.5, cs_high_min_ns=200)
    s.advance(s.now + 20000)
    s.wire.cs(s.now, True)
    assert s.wire.frames == [(24, payload)] * 16
    assert not s.wire.violations and not s.log
    assert min(s.launch_gaps) == 2600  # last rising -> next falling edge
    assert len(s.completions) == 16
    assert [e - b for b, e in zip(s.starts, s.completions)] == [14400] * 16
    print('counterfactual mode3/MIDI6/CS-high200ns: 16 valid frames, '
          'byte sample gap=2800ns, launch gap=2600ns, no model violations', flush=True)

    # Adversarial delayed-SysTick pair: same actual sender runs a second
    # time right after returning, with no modelled IRQ prologue/tailchain.
    # Demonstrates absent busy guard; DOES NOT establish real reachability.
    if not guarded:
        s = SpiTx(regs, kernel)
        end = replay(s, trace, 10000, 2.5)
        replay(s, trace, end + 1000, 2.5)
        s.advance(s.now + 30000)
        assert s.log['CS raised during transfer'] == 1
        assert s.log['CSTART while active'] == 1
        assert s.wire.violations['CS split byte'] > 0
        print('compressed pair (adversarial, not WCET proof):', dict(s.log),
              dict(s.wire.violations), flush=True)
    else:
        print('p5 guard present: compressed-pair replay intentionally skipped; '
              'a completed-transaction trace cannot prove its busy-path behavior.')
    assert Path(path).read_bytes() == original
    print('Audit expectations passed; image unchanged; not a hardware freeze reproduction.')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'build/firmware-patched.bin')
