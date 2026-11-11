#!/usr/bin/env python3
"""WL (p62): the white LEDs, dimmer.

FOLLOW (LED 24), SEQ 1-16 (25..40), TYPE (45) and WAVE (46) are the white
ones, and in a dark room they glare: FOLLOW sits at 63 of 63 when on.  The
writer 0x08109FD0 sends `80 <address> <level>`; for the white LEDs the
level is scaled to N/64 (a lit one at least 1) as it is stored for the frame.
TYPE and WAVE show the type and the wave by brightness (32/48, 4/8/12/16),
so at the default 16/64 those steps must stay apart.  TEMPO+FOLLOW steps the
scale 25 -> 50 -> 100 -> 25 % and toggles no FOLLOW; TEMPO+FUNC+FOLLOW stays
the stock's.  Read off the SPI frames sysemu captures.

  0. control, the stock: FOLLOW sends 62, a SEQ note step 15 (its fade
     settles one step short of 63 and 16, which M2 fixes);
  1. default: FOLLOW 15, the SEQ note 4, TYPE 8/12, WAVE 1/2/3/4; the
     coloured ones (PAGE A, the mini keyboard) unchanged;
  2. TEMPO+FOLLOW: 31, 63, back to 15; FOLLOW itself stays on, no event;
  3. TEMPO+FUNC+FOLLOW leaves the scale alone.

    build/venv/bin/python tests/test_led_white.py
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sysemu import Mono                              # noqa: E402

IMAGE = 'build/firmware-patched.bin'
STOCK = 'build/firmware.bin'
DRV = 0x2000190C
ADDR = 0x0815DE4C              # LED index -> HT16D35A address, 70 bytes
D_LED_WHITE = 0x2000E400 + 0x13F4
PATTERN_PARAM_VT = 0x0815EE94
FOLLOW, SEQ1, PAGE_A, KEY_C, KEY_CS, TYPE, WAVE = 24, 25, 41, 51, 52, 45, 46
ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print('  %-72s ok' % label)
    else:
        fail += 1
        print('  %-72s FAIL got %r want %r' % (label, got, want))


class Panel:
    def __init__(self, image):
        # a fresh boot: a restored snapshot leaves the LED writer waiting on
        # an SPI end-of-transfer the snapshot does not carry (peripheral
        # registers are not in it), and the patched writer holds every frame
        # behind that one (L) -- test_led_fade boots fresh for the same reason
        self.m = m = Mono(image)
        m.boot()
        m.factory_defaults()
        m.settle(200)
        self.uc = m.uc
        self.addr = bytes(self.uc.mem_read(ADDR, 70))

    def sent(self, ms=400):
        """the level the writer sends for each LED index now: every LED is
        marked dirty (the writer sends only those) and the frames read"""
        m = self.m
        for i in range(70):
            self.uc.mem_write(DRV + 0x94 + 20 * i + 3, b'\x01')
        m.led_bytes = []
        m.settle(ms)
        b, last = m.led_bytes, {}
        for i in range(len(b) - 2):
            if b[i] == 0x80:
                last[b[i + 1]] = b[i + 2]
        return {i: last.get(self.addr[i]) for i in range(70)}

    def want(self, i):
        """the level the UI asks for (base + pulse + overlay)"""
        return sum(struct.unpack('<3b', bytes(self.uc.mem_read(DRV + 0x94 + 20 * i, 3))))


def stock():
    print('0. control, the stock')
    p = Panel(STOCK)
    m = p.m
    m.tap(36); m.settle(15); m.tap(4); m.settle(40)          # a note on step 1
    s = p.sent()
    # the stock's fade settles one step short (0.33 + 0.66, fixed by M2)
    check('FOLLOW sends %r, the SEQ 1 note %r (one short: M2)' % (s[FOLLOW], s[SEQ1]), (s[FOLLOW], s[SEQ1]), (62, 15))


def default():
    print('1. default, 25 %')
    p = Panel(IMAGE)
    m = p.m
    m.tap(36); m.settle(15); m.tap(4); m.settle(40)
    s = p.sent()
    check('FOLLOW asks %d, sends %r' % (p.want(FOLLOW), s[FOLLOW]), (p.want(FOLLOW), s[FOLLOW]), (63, 15))
    check('the SEQ 1 note asks %d, sends %r' % (p.want(SEQ1), s[SEQ1]), (p.want(SEQ1), s[SEQ1]), (16, 4))
    check('coloured: PAGE A %r, keyboard C %r and C# %r as asked' % (s[PAGE_A], s[KEY_C], s[KEY_CS]),
          (s[PAGE_A], s[KEY_C], s[KEY_CS]), (p.want(PAGE_A), p.want(KEY_C), p.want(KEY_CS)))
    m.pot_values[9] = 0x2000; m.settle(100)                  # select the cutoff
    types, waves = [], []
    for _ in range(3):
        m.tap(2); types.append((p.want(TYPE), p.sent(200)[TYPE]))
    for _ in range(4):
        m.tap(3); waves.append((p.want(WAVE), p.sent(200)[WAVE]))
    check('TYPE (asked, sent): %s' % types, [t for t in types if t[0]], [(32, 8), (48, 12)])
    check('WAVE (asked, sent): %s' % waves, waves, [(4, 1), (8, 2), (12, 3), (16, 4)])


def gesture():
    print('2. TEMPO+FOLLOW steps the scale')
    p = Panel(IMAGE)
    m = p.m
    got = []
    for _ in range(3):
        m.events = []
        m.press(25); m.settle(40); m.tap(1, hold_ms=40, gap_ms=30); m.release(25); m.settle(40)
        got.append((p.want(FOLLOW), p.sent(600)[FOLLOW], PATTERN_PARAM_VT in m.events))
    check('FOLLOW (asks, sends, toggle event) per press: %s' % got, got,
          [(63, 31, False), (63, 63, False), (63, 15, False)])
    m.tap(1, hold_ms=40, gap_ms=30); m.settle(60)
    check('FOLLOW alone still toggles it: asks %d' % p.want(FOLLOW), p.want(FOLLOW), 0)


def randomizer():
    print('3. TEMPO+FUNC+FOLLOW is still the stock\'s')
    p = Panel(IMAGE)
    m = p.m
    before = struct.unpack('<I', bytes(p.uc.mem_read(D_LED_WHITE, 4)))[0]
    m.events = []
    m.press(26); m.settle(30); m.press(25); m.settle(30); m.tap(1, hold_ms=40, gap_ms=30)
    m.release(25); m.settle(30); m.release(26); m.settle(60)
    after = struct.unpack('<I', bytes(p.uc.mem_read(D_LED_WHITE, 4)))[0]
    check('scale word %d -> %d, an event was sent (%d)' % (before, after, len(m.events)),
          (after == before, len(m.events) > 0), (True, True))


def main():
    stock()
    default()
    gesture()
    randomizer()
    print('%s -- the white LEDs, dimmer (%d checks)' % ('PASS' if fail == 0 else 'FAIL', ok + fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
