#!/usr/bin/env python3
"""The bootloader's USB control requests: which ones crash it.

The secondary bootloader's USB MIDI class table (0x080277E8) has no Setup
handler (the third word is 0), and the ST device stack it uses calls that
pointer without a NULL check:

  0x08024D30  interface requests (any type) on interface 0 or 1 once
              configured -> blx [class+8]
  0x08024D6C  class requests to an endpoint, and SET_FEATURE /
              CLEAR_FEATURE(ENDPOINT_HALT) on a non-zero endpoint (the old
              ST library hands those to the class too) -> blx [class+8]

blx to address 0 on a Cortex-M is a HardFault (INVSTATE): the bootloader
stops, its OUT endpoint is never re-armed, the host's writes pend forever.
Device requests (GET_DESCRIPTOR, GET_STATUS, SET_CONFIGURATION...) take
other paths and are fine.

This runs the stock setup-stage entry 0x08024864 in Unicorn on a configured
device with each request below and reports whether it returns or jumps to
0.  The low-level HAL calls are stubbed.

    build/venv/bin/python tests/test_bootloader_usb_setup.py
"""
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from unicorn import Uc, UcError, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_CODE  # noqa: E402
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_LR, UC_ARM_REG_PC,  # noqa: E402
                               UC_ARM_REG_SP)

BL = 0x08020000
SETUP_STAGE = 0x08024864     # USBD_LL_SetupStage(pdev, setup)
CLASS = 0x080277E8           # Init, DeInit, Setup (= 0), ...
DESC = 0x08027764            # device, langid, strings... descriptor callbacks
DATA_LOAD, DATA, DATA_END = 0x08027740, 0x20000000, 0x20000144   # Reset_Handler's .data copy
PDEV = 0x20010000
SETUP = 0x2001F800
SENTINEL = 0x08010000
LL_STUBS = (0x08024714, 0x0802472C, 0x08024744, 0x08024784, 0x0802479C)
ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print('  %-66s ok' % label)
    else:
        fail += 1
        print('  %-66s FAIL got %r want %r' % (label, got, want))


def run(setup):
    uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
    for base, size in ((0x08000000, 0x200000), (0x20000000, 0x20000), (0x40000000, 0x10000000),
                       (0x50000000, 0x10000000), (0xE0000000, 0x100000)):
        uc.mem_map(base, size)
    uc.mem_write(BL, (ROOT / 'build/bootloader.bin').read_bytes())
    uc.mem_write(SENTINEL, b'\xfe\xe7')
    uc.mem_write(DATA, bytes(uc.mem_read(DATA_LOAD, DATA_END - DATA)))   # descriptors live in .data
    uc.mem_write(PDEV + 0x10, b'\x01')                        # USBD_SPEED_FULL
    uc.mem_write(PDEV + 0x1FC, b'\x03')                       # USBD_STATE_CONFIGURED
    uc.mem_write(PDEV + 0x214, struct.pack('<II', DESC, CLASS))   # pDesc, pClass
    uc.mem_write(SETUP, setup)
    state = {'end': None}

    def hook(u, addr, size, data):
        if addr == SENTINEL:
            state['end'] = 'returned'
            u.emu_stop()
        elif addr in LL_STUBS:
            u.reg_write(UC_ARM_REG_R0, 0)
            u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR))
    for a in (SENTINEL,) + LL_STUBS:
        uc.hook_add(UC_HOOK_CODE, hook, begin=a, end=a)
    uc.reg_write(UC_ARM_REG_SP, 0x2001F000)
    uc.reg_write(UC_ARM_REG_R0, PDEV)
    uc.reg_write(UC_ARM_REG_R1, SETUP)
    uc.reg_write(UC_ARM_REG_LR, SENTINEL | 1)
    try:
        uc.emu_start(SETUP_STAGE | 1, 0, count=200_000)
    except UcError:
        pc = uc.reg_read(UC_ARM_REG_PC)
        return 'jumps to 0x%X' % pc if pc < 0x100 else 'faults at 0x%08X' % pc
    return state['end'] or 'runs away'


REQUESTS = [
    # (label, bmRequestType, bRequest, wValue, wIndex, wLength, crashes)
    ('GET_DESCRIPTOR device', 0x80, 0x06, 0x0100, 0, 18, False),
    ('GET_DESCRIPTOR configuration', 0x80, 0x06, 0x0200, 0, 101, False),
    ('GET_STATUS device', 0x80, 0x00, 0, 0, 2, False),
    ('GET_STATUS endpoint 0x81', 0x82, 0x00, 0, 0x81, 2, False),
    ('SET_FEATURE(ENDPOINT_HALT) endpoint 0x01', 0x02, 0x03, 0, 0x01, 0, True),
    ('CLEAR_FEATURE(ENDPOINT_HALT) endpoint 0x01', 0x02, 0x01, 0, 0x01, 0, True),
    ('CLEAR_FEATURE(ENDPOINT_HALT) endpoint 0x81', 0x02, 0x01, 0, 0x81, 0, True),
    ('class request to endpoint 0x01', 0x22, 0x01, 0, 0x01, 0, True),
    ('GET_STATUS interface 1', 0x81, 0x00, 0, 1, 2, True),
    ('GET_INTERFACE interface 1', 0x81, 0x0A, 0, 1, 1, True),
    ('SET_INTERFACE interface 1, alt 0', 0x01, 0x0B, 0, 1, 0, True),
    ('class GET_CUR to interface 0', 0xA1, 0x81, 0x0100, 0, 2, True),
    ('interface 2 (does not exist): stalled, no call', 0x81, 0x00, 0, 2, 2, False),
]


def main():
    print('control requests to the configured bootloader (0x08024864):')
    for label, rt, req, val, idx, length, crashes in REQUESTS:
        r = run(struct.pack('<BBHHH', rt, req, val, idx, length))
        check('%s: %s' % (label, r), r.startswith('jumps to 0x0'), crashes)
    print('%s -- requests that reach the NULL Setup crash, the rest return (%d checks)'
          % ('PASS' if fail == 0 else 'FAIL', ok + fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
