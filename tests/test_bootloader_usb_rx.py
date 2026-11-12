#!/usr/bin/env python3
"""The bootloader's USB receive path, and how fast a host may send.

Runs the secondary bootloader's own receive code from build/bootloader.bin
in Unicorn: the USB OUT callback (0x080250D4) copies each 4-byte USB-MIDI
event packet into a 128-entry ring at 0x20001AD0 (0x08026DD0, no full
check); the main loop drains that ring once per SysTick (1 ms) through
0x08026C80, which feeds the SysEx parser 0x08026B00; the parser appends 7
decoded bytes per 8-byte group to the AXI SRAM buffer and, on the message
whose index equals the total, branches to the decompress-and-flash routine
0x08026460 (stopped there and the buffer compared with the packed stream).

Nothing in that path checks the ring for overflow, the message indexes for
gaps, or the payload for a checksum.  More than 127 packets between two
drains laps the ring and silently loses data.  One firmware message is 123
bytes = 41 packets, so:

  1. one message per tick, or per 8 ticks (tools/flash.py's pacing):
     the buffer is the packed stream, byte for byte;
  2. three messages landing inside one tick (123 packets): still exact;
     four (164 packets): garbled or stalled;
  3. another application on the same port: a short SysEx (device
     inquiry) is harmless, a long one from another maker overwrites the
     parser's buffer pointer (its header index has no bound: bytes 20..27
     land on the write offset and the buffer pointer) -- a wild write, a
     HardFault on the unit; a MIDI clock packet inside a message adds two
     zero bytes (the packet's CIN is ignored, its padding fed as data);
  4. an unpaced stream of 64-byte USB transfers: 7 per tick (112 packets)
     exact, 8 per tick (128) and up garbled, stalled or a wild write;
  5. half a file, then the whole file again without re-entering the
     bootloader: the write offset and the total survive, garbled.

    build/venv/bin/python tests/test_bootloader_usb_rx.py [file.syx]
"""
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import norand_syx                                  # noqa: E402
from unicorn import Uc, UcError, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_CODE   # noqa: E402
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,  # noqa: E402
                               UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP)

BL = 0x08020000
OBJ = 0x20001AD0             # rings + SysEx parser state
USB_OUT = 0x080250D4         # (buffer, length): USB-MIDI packets -> the ring
DRAIN = 0x08026C80           # main loop, once per tick: ring -> parser
FLASH_ROUTINE = 0x08026460
XFER = 0x2001D000            # where a USB transfer lands
SENTINEL = 0x08010000
PACKETS_PER_XFER = 16        # 64-byte bulk endpoint (descriptor at 0x08027780)
ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print('  %-74s ok' % label)
    else:
        fail += 1
        print('  %-74s FAIL got %r want %r' % (label, got, want))


def usb_packets(msg):
    """a SysEx message as USB-MIDI event packets, cable 0, as a class
    driver sends it: CIN 4 for three bytes that do not end it, 5/6/7 for
    the last one, two or three"""
    out = []
    for i in range(0, len(msg), 3):
        part = msg[i:i + 3]
        cin = 4 if i + 3 < len(msg) else 4 + len(part)
        out.append(bytes([cin]) + part + bytes(3 - len(part)))
    return out


class Bootloader:
    def __init__(self):
        self.uc = uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        for base, size in ((0x08000000, 0x200000), (0x20000000, 0x20000), (0x24000000, 0x80000),
                           (0x30000000, 0x50000), (0x40000000, 0x10000000), (0x50000000, 0x10000000),
                           (0xE0000000, 0x100000)):
            uc.mem_map(base, size)
        uc.mem_write(BL, (ROOT / 'build/bootloader.bin').read_bytes())
        uc.mem_write(OBJ + 0x544, struct.pack('<II', 0x24000000, 0x30000000))   # as main sets them
        uc.mem_write(SENTINEL, b'\xfe\xe7')
        self.done = None
        self.errors = 0

        def ret(u, value):
            u.reg_write(UC_ARM_REG_R0, value)
            u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR))

        def hook(u, addr, size, data):
            if addr == SENTINEL:
                u.emu_stop()
            elif addr == FLASH_ROUTINE:              # r1 buffer, r2 declared size, r3 bytes in
                self.done = (u.reg_read(UC_ARM_REG_R1), u.reg_read(UC_ARM_REG_R2),
                             u.reg_read(UC_ARM_REG_R3))
                u.emu_stop()
            elif addr == 0x08026E58:                 # the progress display's state
                ret(u, 0x2001E000)
            elif addr in (0x080253FC, 0x08025448):   # board objects, read before the flash
                ret(u, 0x2001E100)
            elif addr == 0x08026A00:                 # the size error (a BKPT in the stock)
                self.errors += 1
                ret(u, 0)
        for a in (SENTINEL, FLASH_ROUTINE, 0x08026E58, 0x080253FC, 0x08025448, 0x08026A00):
            uc.hook_add(UC_HOOK_CODE, hook, begin=a, end=a)

    def call(self, fn, *args):
        uc = self.uc
        for reg, v in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3), args):
            uc.reg_write(reg, v)
        uc.reg_write(UC_ARM_REG_SP, 0x2001F000)
        uc.reg_write(UC_ARM_REG_LR, SENTINEL | 1)
        uc.emu_start(fn | 1, 0, count=50_000_000)

    def transfer(self, packets):
        data = b''.join(packets)
        self.uc.mem_write(XFER, data)
        self.call(USB_OUT, XFER, len(data))

    def drain(self):
        self.call(DRAIN, OBJ)


def transfers(packets):
    return [packets[i:i + PACKETS_PER_XFER] for i in range(0, len(packets), PACKETS_PER_XFER)]


def run(msgs, packed, schedule):
    """schedule: a list of ticks, each a list of USB transfers that land in
    it; the main loop drains after each tick.  Returns 'exact', 'garbled',
    'stalled' or 'fault' (a write outside RAM: a HardFault on the unit)."""
    b = Bootloader()
    try:
        for tick in schedule:
            for x in tick:
                b.transfer(x)
            b.drain()
            if b.done:
                break
    except UcError:
        return 'fault'
    if not b.done:
        return 'stalled'
    buf, _, n = b.done
    if buf != 0x24000000:                         # the parser's own buffer pointer was overwritten
        return 'fault'
    got = bytes(b.uc.mem_read(buf, len(packed)))
    return 'exact' if got == packed and n >= len(packed) and not b.errors else 'garbled'


def paced(msgs, per_tick=1):
    """per_tick messages land in each tick (any longer gap only adds empty
    drains, which do nothing, so they are left out)"""
    sched = []
    for i in range(0, len(msgs), per_tick):
        sched.append([x for m in msgs[i:i + per_tick] for x in transfers(usb_packets(m))])
    return sched


def foreign(msg):
    """one tick holding a message from another application"""
    return [transfers(usb_packets(msg))]


def interleaved(msgs, packet):
    """paced, with one extra USB-MIDI packet landing between the first
    message's packets 10 and 11 (another application on the same port)"""
    sched = paced(msgs, 1)
    first = usb_packets(msgs[0])
    sched[0] = transfers(first[:10] + [packet] + first[10:])
    return sched


def stream(msgs, per_tick):
    """the whole file as one packet stream, per_tick 64-byte transfers per tick"""
    xs = transfers([p for m in msgs for p in usb_packets(m)])
    return [xs[i:i + per_tick] for i in range(0, len(xs), per_tick)]


def main():
    syx = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / 'build/mono_V2.2.0r-patched.syx')
    raw = Path(syx).read_bytes()
    msgs = norand_syx.split_messages(raw)
    packed = norand_syx.fw_unpack_packed(raw)
    sizes = sorted(set(len(m) for m in msgs))
    print('%s: %d messages (%s bytes), %d packed bytes' % (Path(syx).name, len(msgs), sizes, len(packed)))

    print('1. paced: one message per tick')
    check('one message per tick (41 packets): buffer', run(msgs, packed, paced(msgs, 1)), 'exact')

    print('2. messages landing inside one tick')
    for k, want in ((2, 'exact'), (3, 'exact')):
        check('%d messages per tick (%d packets): buffer' % (k, 41 * k),
              run(msgs, packed, paced(msgs, k)), want)
    r = run(msgs, packed, paced(msgs, 4))
    check('4 messages per tick (164 packets): buffer %s' % r, r in ('garbled', 'stalled', 'fault'), True)

    print('3. another application sending to the port')
    inquiry = bytes.fromhex('F07E7F0601F7')
    check('a device inquiry (6 bytes) first: buffer',
          run(msgs, packed, foreign(inquiry) + paced(msgs, 1)), 'exact')
    long_sysex = bytes([0xF0, 0x00, 0x20, 0x3C]) + bytes(range(1, 29)) + b'\xF7'
    r = run(msgs, packed, foreign(long_sysex) + paced(msgs, 1))
    check('another maker\'s 33-byte SysEx first: buffer %s' % r, r in ('garbled', 'fault'), True)
    r = run(msgs, packed, interleaved(msgs, bytes([0x0F, 0xF8, 0, 0])))
    check('a MIDI clock packet inside a message: buffer %s' % r, r, 'garbled')
    check('...the same clock between messages: buffer',
          run(msgs, packed, [[transfers([bytes([0x0F, 0xF8, 0, 0])])[0]]] + paced(msgs, 1)), 'exact')

    print('4. an unpaced stream of 64-byte transfers')
    check('7 transfers per tick (112 packets): buffer', run(msgs, packed, stream(msgs, 7)), 'exact')
    for t in (8, 12, 19):
        r = run(msgs, packed, stream(msgs, t))
        check('%d transfers per tick (%d packets): buffer %s' % (t, 16 * t, r),
              r in ('garbled', 'stalled', 'fault'), True)

    print('5. a resend without re-entering the bootloader')
    half = paced(msgs, 1)[:len(msgs) // 2]
    r = run(msgs, packed, half + paced(msgs, 1))
    check('half the file, then the whole file again: buffer %s' % r,
          r in ('garbled', 'stalled', 'fault'), True)

    print('%s -- the USB receive path (%d checks)' % ('PASS' if fail == 0 else 'FAIL', ok + fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
