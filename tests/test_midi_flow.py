#!/usr/bin/env python3
"""MF1-MF5: MIDI output flow control, receive rings, poly pressure.

The real code runs on the bare test rig (tests/test_patch.py) -- the
stock MIDI fan-out 0x0811A904 against np_midi_fanout (both with the real USB
append 0x081101DC/0x08108D54), np_midi_rx, the USB receive callback
0x08108D48 -- with only the System / driver getters and the USB-connected
predicate stubbed.  "Stock" receive paths are this image with the stock
bytes written back over the patched sites, so every difference measured is
the patch.  Poly pressure runs on sysemu, through the real input dispatcher
0x0811B700.

  MF1  USB: a burst of 100 events reaches USB complete and in order across
       flushes (stock: 64, the rest dropped silently);
  MF2  DIN: with USB up a message goes to DIN whole or not at all, so the
       DIN stream parses back to whole messages and USB is not slowed to
       DIN's speed; with USB down nothing is dropped, DIN waits (stock: the
       ring drops queued bytes from the middle of messages);
  MF3  DIN receive ring full: the new byte is dropped, the parser's index
       untouched (stock: the interrupt moves the parser's tail);
  MF4  USB receive ring, the same;
  MF5  A0 kk vv: the pressure is vv (stock: kk).
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_patch as T
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0

IMAGE = 'build/firmware-patched.bin'
STOCK_BIN = 'build/firmware.bin'
D = 0x2000E400
D_MIDI_USB_HELD, D_MIDI_DIN_HELD, D_MIDI_RX_DROP, D_USB_RX_DROP, D_MIDI_DIN_DROP = \
    0xFA8, 0xFAC, 0xFB0, 0xFB4, 0xFB8
OBJ = 0x30010000                  # a MIDI object for the fan-out rig
USB_LEN, USB_BUF = 0x20000704, 0x200005C0
SITES = (0x08108D4A, 0x0811B754)
STOCK_FUNCS = ((0x0811A904, 0x0811AC90), (0x08110330, 0x081103A0))
BLOB = 0x08175200
ok = fail = 0
stock_bytes = Path(STOCK_BIN).read_bytes()


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print('  %-68s ok' % label)
    else:
        fail += 1
        print('  %-68s FAIL got %r want %r' % (label, got, want))


def rig(stock, connected=True):
    T.IMAGE = IMAGE
    m = T.Machine()
    if stock:
        for a in SITES:
            m.uc.mem_write(a, stock_bytes[a - 0x08100000:a - 0x08100000 + 6])
        # the stock fan-out and USB ring push themselves: with MF in, their
        # space holds code of ours (HM, p68)
        for lo, hi in STOCK_FUNCS:
            m.uc.mem_write(lo, stock_bytes[lo - 0x08100000:hi - 0x08100000])
    T.stub_return_const(m, 0x08109614, 0x20010000)
    T.stub_return_const(m, 0x0810977C, 0x20011000)
    T.stub_return_const(m, 0x08110240, 1 if connected else 0)
    for a in (0x200, 0x400, 0x808, 0x80C, 0xC18, 0xC1C, 0xCA8, 0xCB0, 0xD18):
        m.w32(OBJ + a, 0)
    m.w32(USB_LEN, 0)
    for off in (D_MIDI_USB_HELD, D_MIDI_DIN_HELD, D_MIDI_RX_DROP, D_USB_RX_DROP, D_MIDI_DIN_DROP):
        m.w32(D + off, 0)
    m.fan = 0x0811A904 if stock else m.u32(BLOB + 0x194)
    return m


def din_len(status):
    hi = status & 0xF0
    return 2 if hi in (0xC0, 0xD0) else 1 if hi == 0xF0 else 3


def burst(m, events):
    m.uc.mem_write(OBJ + 0x408, b''.join(events))
    m.w32(OBJ + 0x808, 0)
    m.w32(OBJ + 0x80C, len(events) & 0xFF)


def run(m, events, din_per_pass=3, passes=400):
    """Fan-out passes with a 1 ms USB flush and the USART draining
    `din_per_pass` bytes in between; returns (usb packets, DIN bytes,
    silently failed USB appends)."""
    usb, din, failed = [], bytearray(), []

    def after_append(uc, addr, size, data):
        if uc.reg_read(UC_ARM_REG_R0) != 0:
            failed.append(1)
    m.uc.hook_add(UC_HOOK_CODE, after_append, begin=0x0811020A, end=0x0811020A)
    burst(m, events)
    for _ in range(passes):
        m.call(m.fan, r0=OBJ, count=2_000_000)
        n = m.u32(USB_LEN)
        raw = bytes(m.uc.mem_read(USB_BUF, n))
        usb += [raw[i:i + 4] for i in range(0, n, 4)]
        m.w32(USB_LEN, 0)
        for _ in range(din_per_pass):
            rd, wr = m.u32(OBJ + 0xC18), m.u32(OBJ + 0xC1C)
            if rd == wr:
                break
            din.append(m.uc.mem_read(OBJ + 0xA18 + rd, 1)[0])
            m.w32(OBJ + 0xC18, (rd + 1) & 0x1FF)
        if m.u32(OBJ + 0x808) == m.u32(OBJ + 0x80C) and m.u32(OBJ + 0xC18) == m.u32(OBJ + 0xC1C):
            break
    return usb, bytes(din), len(failed)


def expected_din(events):
    return b''.join(e[1:1 + din_len(e[1])] for e in events)


def test_out():
    print('MF1/MF2: the fan-out')
    cc = [bytes((0x0B, 0xB0, i & 0x7F, 17)) for i in range(100)]
    for stock in (True, False):
        m = rig(stock)
        burst(m, cc)
        m.call(m.fan, r0=OBJ, count=2_000_000)
        tag = 'stock' if stock else 'here '
        if stock:
            check('%s: one pass consumes all 100, USB keeps 64' % tag,
                  (m.u32(OBJ + 0x808), m.u32(USB_LEN) // 4), (100, 64))
        else:
            check('%s: one pass takes 64 -- what USB staging holds' % tag,
                  (m.u32(OBJ + 0x808), m.u32(USB_LEN) // 4), (64, 64))
            check('%s: ...and counts the hold' % tag, m.u32(D + D_MIDI_USB_HELD), 1)

    usb_s, din_s, failed_s = run(rig(True), cc, din_per_pass=400)
    usb_h, din_h, failed_h = run(rig(False), cc, din_per_pass=400)
    check('stock: USB packets delivered of 100 / dropped silently', (len(usb_s), failed_s), (64, 36))
    check('here: all 100 reach USB, in order, none dropped', (usb_h, failed_h), (cc, 0))

    # a mixed stream against a 3-byte-per-pass USART: notes, program
    # changes, clock -- 251 events, 567 DIN bytes: more than the 511 the
    # DIN ring holds, so the stock has to drop, and its 56 oldest bytes end
    # two bytes into a note-on (250 events would cut on a boundary by luck)
    mixed = []
    for i in range(251):
        k = i % 4
        mixed.append(bytes((0x09, 0x90, 36 + i % 48, 100)) if k == 0 else
                     bytes((0x08, 0x80, 36 + (i - 1) % 48, 0)) if k == 1 else
                     bytes((0x0C, 0xC0, i % 64, 0)) if k == 2 else
                     bytes((0x0F, 0xF8, 0, 0)))
    want = expected_din(mixed)
    usb_s, din_s, _ = run(rig(True), mixed)
    m = rig(False)
    usb_h, din_h, _ = run(m, mixed)
    check('stock, USB up: DIN stream equals the events sent', din_s == want, False)
    check('stock, USB up: ...nor parses into whole messages', whole(din_s, mixed), False)
    check('here, USB up: USB got every event, in order', usb_h, mixed)
    check('here, USB up: DIN carries whole messages only, in order', whole(din_h, mixed), True)
    check('here, USB up: messages DIN skipped are counted',
          m.u32(D + D_MIDI_DIN_DROP), len(mixed) - whole(din_h, mixed, count=True))
    m = rig(False, connected=False)
    usb_h, din_h, _ = run(m, mixed)
    check('here, USB down: DIN stream equals the events sent, byte for byte', din_h == want, True)
    check('here, USB down: nothing skipped', m.u32(D + D_MIDI_DIN_DROP), 0)

    for connected in (True, False):
        for stock in (True, False):
            m = rig(stock, connected)
            m.w32(OBJ + 0xC18, 6)                     # 5 DIN bytes free
            burst(m, cc[:3])
            m.call(m.fan, r0=OBJ, count=2_000_000)
            tag = '%s, USB %s' % ('stock' if stock else 'here ', 'up  ' if connected else 'down')
            got = (m.u32(OBJ + 0x808), m.u32(OBJ + 0xC1C), m.u32(OBJ + 0xC18),
                   m.u32(D + D_MIDI_DIN_HELD), m.u32(D + D_MIDI_DIN_DROP))
            if stock:
                check('%s: 3 CCs into 5 free DIN bytes -> USART index moved' % tag,
                      m.u32(OBJ + 0xC18) != 6, True)
            elif connected:
                check('%s: all 3 to USB, 1 to DIN whole, 2 skipped, index kept' % tag,
                      got, (3, 3, 6, 0, 2))
            else:
                check('%s: takes 1 CC, leaves 2 queued, USART index kept' % tag,
                      got, (1, 3, 6, 1, 0))


def whole(din, events, count=False):
    """Does the DIN byte stream parse as a subsequence of `events`, each
    message whole?  With count, how many of them."""
    i = n = 0
    for e in events:
        msg = e[1:1 + din_len(e[1])]
        if din[i:i + len(msg)] == msg:
            i += len(msg)
            n += 1
    return n if count else i == len(din)


def test_rx():
    print('MF3: DIN receive ring (np_midi_rx)')
    midi = 0x2000B8E0
    head, tail = midi + 0xA14, midi + 0xA10
    for stock in (True, False):
        m = rig(stock)
        m.w32(tail, 10); m.w32(head, 9)              # full: one more does not fit
        if stock:
            m.call(0x0811A738, r0=midi, r1=0x42)
            check('stock: full ring -> the interrupt moves the parser tail', m.u32(tail), 11)
        else:
            m.w32(0x40011000, 0)                       # CR1: no TXEIE
            m.w32(0x4001101C, 1 << 5)                  # ISR: RXNE
            m.w32(0x40011024, 0x42)                    # RDR
            m.call(m.midi_rx)
            check('here: full ring -> the byte is dropped, indices kept',
                  (m.u32(head), m.u32(tail), m.u32(D + D_MIDI_RX_DROP)), (9, 10, 1))
            m.w32(head, 5)
            m.call(m.midi_rx)
            check('here: room -> stored at head, head advanced',
                  (m.uc.mem_read(midi + 0x810 + 5, 1)[0], m.u32(head)), (0x42, 6))

    print('MF4: USB receive ring (0x08108D48)')
    ring, rtail, rhead = 0x20003260, 0x20003A60, 0x20003A64
    pkts = b''.join(bytes((0x09, 0x90, 60 + i, 100)) for i in range(3))
    for stock in (True, False):
        m = rig(stock)
        m.uc.mem_write(0x30020000, pkts)
        m.w32(rtail, 100); m.w32(rhead, 98)          # one slot free
        m.call(0x08108D48, r0=0x30020000, r1=len(pkts))
        if stock:
            check('stock: 3 packets into 1 free slot -> the reader tail moved',
                  m.u32(rtail) != 100, True)
        else:
            check('here: 1 stored, 2 dropped, reader tail kept',
                  (m.u32(rhead), m.u32(rtail), m.u32(D + D_USB_RX_DROP),
                   bytes(m.uc.mem_read(ring + 98 * 4, 4))), (99, 100, 2, pkts[:4]))


def test_polyat():
    print('MF5: poly pressure (sysemu, the real input dispatcher)')
    import sysemu
    from test_modnote_ui import booted
    midi = 0x2000B8E0
    for image in (STOCK_BIN, IMAGE):
        m = booted(image)
        uc = m.uc
        got = []
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: got.append(u.reg_read(UC_ARM_REG_R0)),
                    begin=0x08119E68, end=0x08119E68)
        uc.ctl_flush_tb()
        mgr = m.call_preserving(0x0811F854)
        ch = m.call_preserving(0x0811FF60, mgr, 1) & 0xF
        rd = lambda a: struct.unpack('<I', bytes(uc.mem_read(a, 4)))[0]

        def feed(*events):
            t = rd(midi + 0x404)
            for e in events:
                uc.mem_write(midi + (t & 0xFF) * 4, bytes(e))
                t = (t + 1) & 0xFF
            uc.mem_write(midi + 0x404, struct.pack('<I', t))
            m.settle(5)
        out0 = rd(midi + 0x80C)
        feed((0, 0xA0 | ch, 60, 100), (0, 0xD0 | ch, 77, 0), (0, 0xA0 | ((ch + 1) & 15), 61, 90))
        fwd = [bytes(uc.mem_read(midi + 0x408 + ((out0 + i) & 0xFF) * 4, 4))
               for i in range((rd(midi + 0x80C) - out0) & 0xFF)]
        # our own channel's pressure is echoed to MIDI out as well (the
        # expressivity echo, 0x08117C6C); the forward is the other channel
        fwd = [f for f in fwd if f[1] == 0xD0 | ((ch + 1) & 15)]
        tag = 'stock' if image == STOCK_BIN else 'here '
        want = [60, 77] if image == STOCK_BIN else [100, 77]
        check('%s: A0 60 100 then D0 77 on our channel -> pressures' % tag, got, want)
        check('%s: A0 61 90 on another channel forwarded as D0 with' % tag,
              [f[2] for f in fwd], [61] if image == STOCK_BIN else [90])


def main():
    test_out()
    test_rx()
    test_polyat()
    print('%s -- MIDI flow and poly pressure (%d checks)' % ('PASS' if fail == 0 else 'FAIL', ok + fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
