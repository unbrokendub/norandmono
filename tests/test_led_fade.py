#!/usr/bin/env python3
"""M2: the LED brightness fade converges.

The stock writer at 0x08109FD0 fades with s' = 0.33 s + 0.66 t and clears
an LED's dirty flag when (u32)s' == (u32)t.  0.33 + 0.66 = 0.99, so the fade
settles at 0.985 t: a brightening LED's floor is t - 1 for ever, it is
re-sent every 70 ticks until power-off, and it shows one step too dim.
Measured on the unit as 15-23 permanently dirty LEDs.  The patch uses exact
thirds and lands on the target once within one step of it.
"""
import struct
import sys

sys.path.insert(0, 'tests')
from sysemu import Mono

WRITER = 0x08109FD0
ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print("  %-56s ok" % label)
    else:
        fail += 1
        print("  %-56s FAIL got %r want %r" % (label, got, want))


def main(image='build/firmware-patched.bin'):
    m = Mono(image)
    m.boot()
    m.factory_defaults()
    m.settle(200, audio=1)
    uc = m.uc
    drv = m.call_preserving(0x0810977D, m.call_preserving(0x08109615), 4)
    u8 = lambda a: uc.mem_read(a, 1)[0]
    f32 = lambda a: struct.unpack('<f', bytes(uc.mem_read(a, 4)))[0]

    def fade(led, start, target, visits=12):
        e = drv + led * 20
        uc.mem_write(e + 0x94, bytes([target]))
        uc.mem_write(e + 0x95, b'\0'); uc.mem_write(e + 0x96, b'\0')
        uc.mem_write(e + 0x98, b'\0\0'); uc.mem_write(e + 0x9d, b'\0')
        uc.mem_write(e + 0xa0, struct.pack('<f', float(start)))
        uc.mem_write(e + 0x97, b'\x01')
        trail = []
        for _ in range(visits):
            uc.mem_write(drv + 0x60c, bytes([(led - 1) % 70]))   # next visit is this LED
            m.instructions += 20000                              # past patch L's frame gate
            m.call_preserving(WRITER, drv)
            trail.append((f32(e + 0xa0), u8(e + 0x97), u8(drv + 0x92)))
            if u8(e + 0x97) == 0:
                break
        return trail

    # brightening: the case the stock never finished
    t = fade(5, 0, 16)
    check("a brightening LED clears dirty", t[-1][1], 0)
    check("...within six visits (%d)" % len(t), len(t) <= 6, True)
    check("...landing exactly on the target", t[-1][0], 16.0)
    check("...and the byte sent is the target, not one below", t[-1][2], 16)
    check("the fade still fades (first visit is not the target)", t[0][0] < 16.0, True)
    # dimming: must still converge, and no faster than before
    t = fade(6, 63, 16)
    check("a dimming LED clears dirty", t[-1][1], 0)
    check("...in a few visits, not one (%d)" % len(t), 3 <= len(t) <= 6, True)
    # to off
    t = fade(7, 40, 0)
    check("fading to off clears dirty", t[-1][1], 0)
    # from above the stock's own test succeeds first (floor 0.49 == 0), which
    # is fine: what matters is the byte sent
    check("...with the byte sent zero and the fade under one step",
          (t[-1][2], t[-1][0] < 1.0), (0, True))
    # full scale
    t = fade(8, 0, 63)
    check("fading to full scale clears dirty", t[-1][1], 0)
    check("...at 63, clamped byte 63", (t[-1][0], t[-1][2]), (63.0, 63))

    print()
    if fail:
        print("FAILED: %d check(s)" % fail)
        raise SystemExit(1)
    print("PASS -- the LED fade converges and lands on its target (%d checks)" % ok)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'build/firmware-patched.bin')
