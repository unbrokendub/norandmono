#!/usr/bin/env python3
"""LV (p62): the LEDs levelled.

The stock asks each LED for its own level -- FOLLOW 63 when on, TYPE 32/48,
the current pattern 24, a note step 16, the dim states 2..7 -- and in a dark
room the white ones glare.  np_led_smooth levels each LED's target before the
writer's fade: off stays off, a dim state goes to `dim`, 8..23 to `normal`,
24+ to `bright`; TYPE (LED 45) and WAVE (46), which show the type and the
wave by brightness, keep small steps of their own; the writer's animation
(the playhead, PAGE A's tempo blink) rises to `high`.  Presets
{dim, normal, bright, high}: 1 {1,1,1,4}, 2 {1,2,3,8}, 3 {2,4,6,16},
4 {3,8,12,32}, 5 the stock's levels.  TEMPO+FOLLOW steps them; FOLLOW is not
toggled; TEMPO+FUNC+FOLLOW stays the stock's.  Read off the SPI frames
sysemu captures (`80 <address> <level>`).

  0. control, the stock: FOLLOW 62, a note step 15, PAGE A 15 (its fade
     settles one short, which M2 fixes);
  1. preset 1 at power-on: every lit LED sends 1 -- FOLLOW, the note step,
     PAGE A, the mini keyboard (bright and dim alike), the selected knob;
     TYPE 2/3, WAVE 1/2/3/4;
  2. playing: the playhead and PAGE A's blink go above 1, up to 4; FOLLOW
     and the other lit steps stay at 1;
  3. TEMPO+FOLLOW: FOLLOW (bright class) 3, 6, 12, 63, back to 1 and a dim
     key's LED (asks 2) 1, 2, 3, 2, 1, with no FOLLOW toggle and no event;
     FOLLOW alone toggles.  (Not a SEQ step or the selected key: after a
     few TEMPO presses the stock starts a tempo blink on them, which levels
     as "high", as PAGE A's does.)
  4. TEMPO+FUNC+FOLLOW leaves the preset alone.

    build/venv/bin/python tests/test_led_level.py
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
D_LED_LEVEL = 0x2000E400 + 0x13F4
PATTERN_PARAM_VT = 0x0815EE94
FOLLOW, SEQ1, PAGE_A, KEY_C, KEY_CS, KEY_D, TYPE, WAVE = 24, 25, 41, 51, 52, 53, 45, 46
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

    def frames(self, ms, mark=True):
        """{LED index: [levels sent]} over ms; every LED is marked dirty
        first (the writer sends only those), so each one shows"""
        m = self.m
        if mark:
            for i in range(70):
                self.uc.mem_write(DRV + 0x94 + 20 * i + 3, b'\x01')
        m.led_bytes = []
        m.settle(ms)
        b, out = m.led_bytes, {}
        index = {a: i for i, a in enumerate(self.addr)}
        for k in range(len(b) - 2):
            if b[k] == 0x80 and b[k + 1] in index:
                out.setdefault(index[b[k + 1]], []).append(b[k + 2])
        return out

    def sent(self, ms=400):
        f = self.frames(ms)
        return {i: (f[i][-1] if i in f else None) for i in range(70)}

    def want(self, i):
        """the level the UI asks for (base + pulse + overlay)"""
        return sum(struct.unpack('<3b', bytes(self.uc.mem_read(DRV + 0x94 + 20 * i, 3))))

    def note(self, step):
        self.m.tap(36); self.m.settle(15); self.m.tap(3 + step); self.m.settle(40)


def stock():
    print('0. control, the stock')
    p = Panel(STOCK)
    p.note(1)
    s = p.sent()
    check('FOLLOW %r, the note step %r, PAGE A %r (one short: M2)' % (s[FOLLOW], s[SEQ1], s[PAGE_A]),
          (s[FOLLOW], s[SEQ1], s[PAGE_A]), (62, 15, 15))


def preset1():
    print('1. preset 1 at power-on')
    p = Panel(IMAGE)
    m = p.m
    p.note(1)
    m.pot_values[9] = 0x2000; m.settle(100)                  # select the cutoff: its knob LED
    s = p.sent()
    lit = {i: (p.want(i), s[i]) for i in range(70) if p.want(i) > 0 and i not in (TYPE, WAVE)}
    check('every lit LED sends 1: %s' % sorted(lit.items())[:12],
          sorted(set(v for _, v in lit.values())), [1])
    check('they asked for a spread: FOLLOW %d, note %d, PAGE A %d, key C %d, C# %d'
          % tuple(p.want(i) for i in (FOLLOW, SEQ1, PAGE_A, KEY_C, KEY_CS)),
          len(set(p.want(i) for i in (FOLLOW, SEQ1, KEY_CS))), 3)
    types, waves = [], []
    for _ in range(3):
        m.tap(2); types.append((p.want(TYPE), p.sent(200)[TYPE]))
    for _ in range(4):
        m.tap(3); waves.append((p.want(WAVE), p.sent(200)[WAVE]))
    check('TYPE (asked, sent): %s' % types, [t for t in types if t[0]], [(32, 2), (48, 3)])
    check('WAVE (asked, sent): %s' % waves, waves, [(4, 1), (8, 2), (12, 3), (16, 4)])


def playing():
    print('2. playing: the playhead and the tempo blink above the rest')
    p = Panel(IMAGE)
    m = p.m
    for step in (1, 5, 9, 13):
        p.note(step)
    m.tap(27); m.settle(100)
    f = p.frames(2200, mark=False)
    m.tap(27); m.settle(30)
    seq = [v for i in range(25, 41) for v in f.get(i, [])]
    check('SEQ steps over 2 s: highest %d (playhead), lowest lit %d'
          % (max(seq or [0]), min([v for v in seq if v] or [0])),
          (3 <= max(seq or [0]) <= 4, min([v for v in seq if v] or [0])), (True, 1))
    check('PAGE A blinks above 1: %s' % sorted(set(f.get(PAGE_A, [])))[-3:],
          max(f.get(PAGE_A, [0])) > 1, True)
    check('FOLLOW stays at 1: %s' % sorted(set(f.get(FOLLOW, [1]))), set(f.get(FOLLOW, [1])), {1})


def gesture():
    print('3. TEMPO+FOLLOW steps the presets')
    p = Panel(IMAGE)
    m = p.m
    p.note(1)
    got = []
    for _ in range(5):
        m.events = []
        m.press(25); m.settle(40); m.tap(1, hold_ms=40, gap_ms=30); m.release(25); m.settle(40)
        s = p.sent(600)
        got.append((s[FOLLOW], s[KEY_CS], PATTERN_PARAM_VT in m.events))
    check('(FOLLOW, dim key C#, toggle event) per press: %s' % got, got,
          [(3, 1, False), (6, 2, False), (12, 3, False), (63, 2, False), (1, 1, False)])
    check('FOLLOW still asks 63 (not toggled)', p.want(FOLLOW), 63)
    m.tap(1, hold_ms=40, gap_ms=30); m.settle(60)
    check('FOLLOW alone still toggles it: asks %d' % p.want(FOLLOW), p.want(FOLLOW), 0)


def randomizer():
    print('4. TEMPO+FUNC+FOLLOW is still the stock\'s')
    p = Panel(IMAGE)
    m = p.m
    before = struct.unpack('<I', bytes(p.uc.mem_read(D_LED_LEVEL, 4)))[0]
    m.events = []
    m.press(26); m.settle(30); m.press(25); m.settle(30); m.tap(1, hold_ms=40, gap_ms=30)
    m.release(25); m.settle(30); m.release(26); m.settle(60)
    after = struct.unpack('<I', bytes(p.uc.mem_read(D_LED_LEVEL, 4)))[0]
    check('preset word %d -> %d, an event was sent (%d)' % (before, after, len(m.events)),
          (after == before, len(m.events) > 0), (True, True))


def main():
    stock()
    preset1()
    playing()
    gesture()
    randomizer()
    print('%s -- the LEDs levelled (%d checks)' % ('PASS' if fail == 0 else 'FAIL', ok + fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
