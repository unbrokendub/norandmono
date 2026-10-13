#!/usr/bin/env python3
"""Drive oscillator TYPE/AMOUNT through the real UI; print actual coverage.

Feed the full 16-bit DMA sample range that the firmware normalises by 65536.
Check the display source and LED base brightness too, not just DSP Amount
and continued calls of the renderer. SPI smoothing is checked separately.
Neither host instruction counts nor serial IRQ calls establish CPU margin.
"""
import struct
import sys
import hashlib
from pathlib import Path
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1
import sysemu

PROJECT = 0x200047F0
DSP = PROJECT + 0x44


def u32(m, a):
    return struct.unpack('<I', m.uc.mem_read(a, 4))[0]


def f32(m, a):
    return struct.unpack('<f', m.uc.mem_read(a, 4))[0]


def main(image, note_key=34):
    print('SHA256', hashlib.sha256(Path(image).read_bytes()).hexdigest(), flush=True)
    m = sysemu.Mono(image).boot()
    m.settle(50)
    m.factory_defaults()
    uc = m.uc
    # Registered after the default row hook: replace only external ADC data.
    def adc16(uc, a, size, data):
        row = uc.reg_read(UC_ARM_REG_R1) & 7
        buf = u32(m, uc.reg_read(UC_ARM_REG_R0) + 0x6C)
        uc.mem_write(buf, struct.pack('<3H', *(m.pot_values[i] for i in m.POT_ROWS[row])))
    uc.hook_add(UC_HOOK_CODE, adc16, begin=0x0810F0F4, end=0x0810F0F4)
    counts = {'main': 0, 'pot': 0, 'led_scan': 0, 'ui_refresh': 0, 'led_render': 0,
              'main_render': 0}
    def mainloop(uc, a, size, data): counts['main'] += 1
    def pot(uc, a, size, data): counts['pot'] += 1
    uc.hook_add(UC_HOOK_CODE, mainloop, begin=0x08119A02, end=0x08119A02)
    uc.hook_add(UC_HOOK_CODE, pot, begin=0x0810F1EA, end=0x0810F1EA)
    for name, address in (('led_scan', 0x08109FD0), ('ui_refresh', 0x081499A4),
                          ('led_render', 0x0814F80C), ('main_render', 0x0814A08C)):
        def count_hook(uc, a, size, data, name=name): counts[name] += 1
        uc.hook_add(UC_HOOK_CODE, count_hook, begin=address, end=address)
    system = m.call_preserving(0x08109614)
    led = m.call_preserving(0x0810977C, system, 4)
    m.pot_values = [32768] * 24
    m.settle(100, audio=1)
    # Pot 0 is oscillator-1 pitch, pot 18 the selected slot's X-Mod amount.
    m.pot_values[0] = 40000
    m.settle(100, audio=1)
    assert uc.mem_read(sysemu.UI + 0x92, 1)[0] == 0
    for _ in range(3):
        m.tap(2, audio=1)
    owner = u32(m, DSP + 0x2E68)
    print('image', image, 'ADC_CFGR=%08x' % u32(m, 0x4002200C), flush=True)
    print('selected', uc.mem_read(sysemu.UI + 0x92, 1)[0],
          'raw TYPE', f32(m, owner + 0x1A4) * 65536,
          'mode/marker', list(uc.mem_read(owner + 0x10C, 1)),
          list(uc.mem_read(owner + 0x25, 1)), flush=True)
    assert f32(m, owner + 0x1A4) * 65536 == 3
    assert uc.mem_read(owner + 0x25, 1)[0] == 2
    # 34 = low C, 46 = octave C (36 used by the previous probe is D).
    assert note_key in range(34, 47)
    m.press(note_key)
    values = []
    for raw in (0, 8192, 16384, 24576, 32768, 40960, 49152, 57344, 65535,
                32768, 0, 65535, 0, 65535):
        m.pot_values[18] = raw
        prev = counts.copy()
        old_bytes = len(m.led_bytes)
        m.settle(80, audio=1)
        value = f32(m, owner + 0x164)
        values.append(value)
        print('ADC=%5d amount=%.6f main=%d pot_events=%d pc=%08x' % (
            raw, value, counts['main'] - prev['main'], counts['pot'] - prev['pot'],
            uc.reg_read(sysemu.UC_ARM_REG_PC)), flush=True)
        assert counts['main'] > prev['main'], 'main stopped'
        updates = m.led_bytes[old_bytes:]
        assert len(updates) % 3 == 0, 'partial LED transaction at slice boundary'
        assert all(updates[i] == 0x80 and updates[i + 2] <= 63
                   for i in range(0, len(updates), 3)), 'bad LED command or brightness'
        print('  key=%d note=%g LEDs: scans=%d render=%d refresh=%d commands=%d '
              'index=%d UI=%s SPI4_CR1=%08x' % (
            note_key, f32(m, PROJECT + 0x5FCC), counts['led_scan'] - prev['led_scan'],
            counts['led_render'] - prev['led_render'], counts['ui_refresh'] - prev['ui_refresh'],
            len(updates) // 3, uc.mem_read(led + 0x60C, 1)[0],
            bytes(uc.mem_read(sysemu.UI + 0x8B, 7)).hex(), u32(m, 0x40013400)), flush=True)
        # 320 = 80 slices x 4 kHz / (1 kHz ...): the harness drops an injection
        # that lands inside a PRIMASK section (telemetry CC writes), and with the
        # honest DMA1 S0 model plus the re-arm guard that costs exactly one scan
        assert 318 <= counts['led_scan'] - prev['led_scan'] <= 320, counts['led_scan'] - prev['led_scan']
        assert counts['led_render'] > prev['led_render'], 'LED renderer stopped'
        selected = uc.mem_read(sysemu.UI + 0x92, 1)[0]
        assert selected == 0, 'selection changed during Amount editing'
        # Constructor 0x0811CDE0..0x0811CE14 registers LFOLevelCParam as
        # local 1 (not local 4, which is the other envelope's level).
        display_param = u32(m, PROJECT + 0x6074 + 4 * (selected * 9 + 1))
        display_value = f32(m, display_param + 12)
        assert display_param + 12 == owner + 0x164, 'display reads another parameter'
        assert display_value == value
        # Exact VFP single-precision arithmetic of stock 0x08116C00.
        round_f32 = lambda x: struct.unpack('<f', struct.pack('<f', x))[0]
        expected_base = int(abs(round_f32(round_f32(value * 65535.0) - 32767.5))) >> 11
        entry = bytes(uc.mem_read(led + 0x94 + 18 * 20, 20))
        assert entry[0] == expected_base, (value, entry[0], expected_base)
        print('  display=%08x value=%.6f LED18 base=%d dirty=%d smoothed=%.3f '
              'main_render=%d' % (display_param, display_value, entry[0], entry[3],
              struct.unpack_from('<f', entry, 12)[0],
              counts['main_render'] - prev['main_render']), flush=True)
    assert min(values) < 0.01 and max(values) > 0.99, 'amount sweep never reached both endpoints'
    m.release(note_key)
    m.settle(120, audio=1)
    print('full-range ratio/amount serial-IRQ scenario passed; not a preemption/cycle test')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'build/firmware-patched.bin',
         int(sys.argv[2]) if len(sys.argv) > 2 else 34)
