"""Run actual stock LED entry + injected hooks with adversarial peripheral states.

These are register/ABI tests; wire timing is independently checked by
probe_spi_led_timing.py. No claim of cycle-accurate Cortex-M7 emulation.
"""
from unicorn import UC_HOOK_MEM_WRITE
from unicorn.arm_const import *


def run(m):
    print('P6 SPI: actual sender call site, busy/error/spacing guards and ABI')
    m.reset(flags=0)
    m.call(m.early)
    uc, d, spi, driver = m.uc, m.diag, 0x40013400, 0x30001000
    entry = driver + 0x94 + 20 * 60
    writes = []
    def watch(uc, access, addr, size, value, data):
        writes.append((addr, size, value))
    uc.hook_add(UC_HOOK_MEM_WRITE, watch, begin=spi, end=spi + 0x23)
    uc.hook_add(UC_HOOK_MEM_WRITE, watch, begin=0x58021018, end=0x58021018)

    def send(sr=0x101A, cr1=0x1001, now=100000, last=0, count=1, pending=0,
             has_started=None):
        uc.mem_write(driver + 0x60C, b'\x3b')
        uc.mem_write(driver + 0x90, b'\x80')
        uc.mem_write(entry, bytes(20))
        uc.mem_write(entry + 3, b'\x01')
        m.w32(spi, cr1); m.w32(spi + 0x14, sr)
        m.w32(d + 0xD48, last); m.w32(d + 0xD4C, count)
        m.w32(d + 0xD8C, int(count != 0) if has_started is None else has_started)
        m.w32(d + 0xD50, pending); m.w32(0xE0001004, now)
        preserved = [UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
                     UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11]
        for i, r in enumerate(preserved): uc.reg_write(r, 0x44440000 + i)
        sp = uc.reg_read(UC_ARM_REG_SP)
        writes.clear()
        m.call(0x08109FD0, r0=driver, count=100000)
        assert uc.reg_read(UC_ARM_REG_PC) == 0x1000, 'did not return'
        assert uc.reg_read(UC_ARM_REG_SP) == sp, 'stack imbalance'
        assert [uc.reg_read(r) for r in preserved] == [0x44440000+i for i in range(8)]
        return bytes(v for a, size, v in writes if a == spi + 0x20)

    assert send() == bytes.fromhex('804d00')
    assert uc.mem_read(entry + 3, 1) == b'\x00'
    assert uc.mem_read(d + 0xE00 + 60, 1) == b'\x00'
    assert m.u32(d + 0xD50) == 0x8000003C
    assert m.u32(d + 0xD48) == 100000
    assert m.u32(d + 0xD4C) == 2
    assert [v for a, _, v in writes if a == 0x58021018] == [8, 8 << 16]

    # Every blocked path must leave both CS and TXDR untouched and retain
    # the dirty LED. A later scan can retry instead of losing the update.
    cases = [dict(cr1=0x1201), dict(sr=0x1002, pending=0x8000073C),
             dict(sr=0x121A), dict(cr1=0), dict(now=7999),
             dict(now=100000, last=100000)]
    for case in cases:
        assert send(**case) == b'', case
        assert not writes, (case, writes)
        assert uc.mem_read(entry + 3, 1) == b'\x01'
    assert m.u32(d + 0xD58) == 2
    assert m.u32(d + 0xD5C) == 2
    assert m.u32(d + 0xD44) == 2

    # Exact boundary, CYCCNT wrap, and first transfer at zero are safe.
    assert send(now=8000, pending=0x8000073C)
    assert uc.mem_read(d + 0xE80 + 60, 1) == b'\x07'
    assert m.u32(d + 0xD54) == 1
    assert send(now=0x1000, last=0xFFFFE000)
    assert send(now=0, count=0)
    # Counter overflow must not accidentally bypass the time guard.
    assert send(count=0xFFFFFFFF)
    assert m.u32(d + 0xD4C) == 0 and m.u32(d + 0xD8C) == 1
    assert send(now=100000, last=100000, count=0, has_started=1) == b''
    m.w32(d + 0x48, 0)
    assert send(now=0, last=0)  # bounded fallback; idle hardware only
    assert send(cr1=0x1201) == b''

    # Warm boot clears all transport/snapshot state, but retains crash ring.
    m.w32(d + 0xD84, 0x12345678)
    m.call(m.early)
    assert m.u32(d + 0xD4C) == m.u32(d + 0xD84) == 0
    assert uc.mem_read(d + 0xE00, 72) == bytes([255]) * 72
    assert uc.mem_read(d + 0xE80, 72) == bytes([255]) * 72
    print('P6 SPI register/ABI tests PASS (busy/error/boundary/wrap/fallback + warm reset)')


if __name__ == '__main__':
    from test_patch import Machine
    run(Machine())
