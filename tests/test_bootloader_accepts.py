#!/usr/bin/env python3
"""The real bootloader accepts the update and flashes exactly our image.

p43 (525,440 bytes) bricked a unit: the bootloader's decompress-and-flash
routine (0x08026460) erases bank 2 first, then parses the stream's
`CF 00 <uleb128 size>` header, and a size over 0x80000 reads as 0
(0x08026746) -- the update is refused with the application already gone.
protocol.md had the routine's other two 0x80000 checks but not that one.

This runs the bootloader's own code from build/bootloader.bin in Unicorn on
the decoded payload of a .syx, with the HAL flash calls stubbed (erase fills
bank 2 with 0xFF, program copies a 32-byte flash word), and checks:

  1. the receive path's declared-size check (messages * 112 < 0x80000);
  2. the routine reaches its jump to the application (0x0802663A);
  3. bank 2 then holds exactly the image, byte for byte, and 0xFF after it.

    test_bootloader_accepts.py [file.syx ...]     (default: the patched build)
"""
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import norand_syx                                  # noqa: E402
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_CODE   # noqa: E402
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_LR,  # noqa: E402
                               UC_ARM_REG_PC, UC_ARM_REG_SP)

BL = 0x08020000
FLASH_ROUTINE = 0x08026460
JUMP_TO_APP = 0x0802663A
STUBS = {
    0x080209C8: 'unlock', 0x08020830: 'erase', 0x08020620: 'program',
    0x08027116: 'malloc', 0x08027112: 'free', 0x08026E58: 'progress', 0x08026A00: 'error',
    0x080272E8: 'abort',
}


def run(syx):
    raw = Path(syx).read_bytes()
    msgs = norand_syx.split_messages(raw)
    packed = norand_syx.fw_unpack_packed(raw)
    image = norand_syx.lzss_decompress(packed)
    declared = (len(msgs) - 1) * 112                  # total * 7 * 16, the tail adds < 112
    uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
    for base, size in ((0x08000000, 0x200000), (0x20000000, 0x20000), (0x24000000, 0x80000),
                       (0x30000000, 0x50000), (0x40000000, 0x10000000), (0x50000000, 0x10000000),
                       (0xE0000000, 0x100000)):
        uc.mem_map(base, size)
    uc.mem_write(0x08000000, b'\xFF' * 0x200000)
    uc.mem_write(BL, (ROOT / 'build/bootloader.bin').read_bytes())
    uc.mem_write(0x24000000, packed)
    heap = [0x30000000]
    free_list, sizes = {}, {}
    state = {'erased': 0, 'words': 0, 'result': None, 'error': 0}

    def ret(u, value=0):
        u.reg_write(UC_ARM_REG_R0, value)
        u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR))

    def hook(u, addr, size, data):
        what = STUBS.get(addr)
        if addr == JUMP_TO_APP:
            state['result'] = 'jump'
            u.emu_stop()
        elif what == 'erase':
            u.mem_write(0x08100000, b'\xFF' * 0x100000)
            state['erased'] += 1
            ret(u)
        elif what == 'program':
            a, src = u.reg_read(UC_ARM_REG_R1), u.reg_read(UC_ARM_REG_R2)
            u.mem_write(a, bytes(u.mem_read(src, 32)))
            state['words'] += 1
            ret(u)
        elif what == 'malloc':
            n = max((u.reg_read(UC_ARM_REG_R0) + 7) & ~7, 8)
            if free_list.get(n):
                p = free_list[n].pop()
            else:
                p = heap[0]
                heap[0] += n
            sizes[p] = n
            u.mem_write(p, b'\0' * n)
            ret(u, p)
        elif what == 'free':
            p = u.reg_read(UC_ARM_REG_R0)
            if p in sizes:
                free_list.setdefault(sizes[p], []).append(p)
            ret(u)
        elif what == 'progress':
            ret(u, 0x20010000)
        elif what == 'error':
            state['error'] += 1
            ret(u)
        elif what == 'abort':
            state['result'] = 'abort'
            u.emu_stop()
        elif what:
            ret(u)
    for a in list(STUBS) + [JUMP_TO_APP]:
        uc.hook_add(UC_HOOK_CODE, hook, begin=a, end=a)
    obj = 0x20008000                                   # the updater object: page count, staging
    uc.mem_write(obj, b'\0' * 0x200)
    uc.reg_write(UC_ARM_REG_SP, 0x2001F000)
    uc.reg_write(UC_ARM_REG_R0, obj)
    uc.reg_write(UC_ARM_REG_R1, 0x24000000)
    sentinel = 0x08010000
    uc.mem_write(sentinel, b'\xfe\xe7')                # b .
    uc.reg_write(UC_ARM_REG_LR, sentinel | 1)
    uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: (state.update(result='returned'), u.emu_stop()),
                begin=sentinel, end=sentinel)
    uc.emu_start(FLASH_ROUTINE | 1, 0, count=400_000_000)
    flashed = bytes(uc.mem_read(0x08100000, 0x100000))
    exact = flashed[:len(image)] == image
    pad = (-len(image)) % 32                           # the last flash word is padded
    tail = flashed[len(image) + pad:] == b'\xFF' * (0x100000 - len(image) - pad)
    return image, declared, state, exact, tail


def main():
    files = sys.argv[1:] or [str(ROOT / 'build/mono_V2.2.0r-patched.syx')]
    bad = 0
    for f in files:
        image, declared, st, exact, tail = run(f)
        ok = (declared < 0x80000 and st['result'] == 'jump' and st['erased'] == 1 and exact and tail
              and not st['error'])
        print('%-44s %7d bytes (%+d to the cap), declared %6d: %s, %d flash words, image %s%s'
              % (Path(f).name, len(image), 0x80000 - len(image), declared, st['result'], st['words'],
                 'exact' if exact else 'DIFFERS', '' if tail else ', junk after it'))
        bad += not ok
    print('%s -- the bootloader %s' % ('PASS' if not bad else 'FAIL',
                                       'flashes every image as built' if not bad else 'refuses or garbles one'))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
