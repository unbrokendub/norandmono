"""Read-only P6 audit: stock MIDI fan-out silently loses USB messages at 64.

Execute real sub_0811A904 -> sub_081101DC -> sub_08108D54. Only the
System/driver getters and USB-connected predicate are stubbed. No USB flush
is scheduled during one burst (a declared adversarial scheduling condition).
This reproduces a loss mechanism, NOT the physical LED freeze.
"""
import hashlib
import struct
import sys
from pathlib import Path
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_LR, UC_ARM_REG_R0
import test_patch as T


def main(path):
    raw = Path(path).read_bytes()
    print('image', path, 'SHA256', hashlib.sha256(raw).hexdigest())
    T.IMAGE = path
    for count in (1, 63, 64, 65, 100, 128):
        m = T.Machine()
        T.stub_return_const(m, 0x08109614, 0x20010000)
        T.stub_return_const(m, 0x0810977C, 0x20011000)
        T.stub_return_const(m, 0x08110240, 1)  # USB link connected
        obj = 0x30010000
        # Unique USB-MIDI CC words; word i has controller i, value 17.
        packets = [bytes((0x0B, 0xBF, i, 17)) for i in range(count)]
        m.uc.mem_write(obj + 0x408, b''.join(packets))
        m.w32(obj + 0x808, 0)
        m.w32(obj + 0x80C, count)
        m.w32(0x20000704, 0)  # real 256-byte USB staging length
        failures = []
        def after_append(uc, addr, size, data):
            if uc.reg_read(UC_ARM_REG_R0) != 0:
                failures.append(uc.reg_read(UC_ARM_REG_R0))
        m.uc.hook_add(UC_HOOK_CODE, after_append, begin=0x0811020A, end=0x0811020A)
        m.call(0x0811A904, r0=obj, count=1000000)
        assert m.u32(obj + 0x808) == count, 'stock fan-out did not consume every word'
        assert m.u32(0x20000704) == min(count, 64) * 4
        assert bytes(m.uc.mem_read(0x200005C0, min(count, 64) * 4)) == b''.join(packets[:64])
        assert failures == [2] * max(0, count - 64)
        assert m.u32(obj + 0xC1C) == count * 3, 'DIN copy missing unexpectedly'
        assert bytes(m.uc.mem_read(obj + 0xA18, count * 3)) == b''.join(p[1:] for p in packets)
        print('input=%3d consumed=%3d USB=%2d silently dropped=%2d DIN=%3d' %
              (count, m.u32(obj + 0x808), min(count, 64), len(failures), count))
    assert Path(path).read_bytes() == raw
    print('Confirmed: USB append returns 2 at packet index 64; upstream ignores it. '
          'Firmware file unchanged. No LED-cause inference.')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'build/firmware-p6-spi-diag.bin')
