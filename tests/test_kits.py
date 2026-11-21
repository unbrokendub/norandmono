#!/usr/bin/env python3
"""K2, K3: TEMPO+FUNC+D# builds a bass kit in the scale; +A# every mod note.

np_mn_chord_check (patch/np_mnrand.cpp) answers the TEMPO+FUNC randomize
handler's key check in mod-note mode: C# a drum kit (K1, test_modnote_random
section 6), D# a bass kit over the page, A# all forty mod notes.

sysemu, mod-note mode:

  1. D# in A minor: a different bass on every key (all eight basses on the
     page's eight mod notes), each key a degree of the scale up from the
     root (A3 B3 C4 D4 E4 F4 G4 A4) -- the keys' live pitches -- and each
     bass's oscillator on that degree; nothing off the page touched, no
     stock event; pressing a key gives the engine its degree;
  2. no scale: C major, each key its own note (C4 .. C5);
  3. a five-note scale carries on into the next octave;
  4. A#: forty instruments, one per mod note on every page, the record list
     bounded, no Error_Handler, how long main is busy with it;
  5. normal mode: TEMPO+FUNC+D# / A# are P1's (a patch), not a kit.
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sysemu
from unicorn import UC_HOOK_CODE, UC_HOOK_BLOCK
from unicorn.arm_const import UC_ARM_REG_R1, UC_ARM_REG_LR
import test_modnote_random as T

DIAG = 0x2000E400
D_MNRAND_N, D_MNRAND_KITS, D_MNRAND_KIND, D_PATCHES = 0xFE8, 0xFFC, 0x1BC0, 0x1BF4
MGR = 0x2000C68C
PATTERN_PARAM_VT = 0x0815EE94
STOCK = 'build/firmware.bin'
ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print('  %-72s ok' % label)
    else:
        fail += 1
        print('  %-72s FAIL got %r want %r' % (label, got, want))


def main():
    r = T.Rig()                                   # mod-note mode
    m, uc = r.m, r.uc
    if r.u32(0x081524D4) == struct.unpack_from('<I', Path(STOCK).read_bytes(), 0x081524D4 - 0x08100000)[0]:
        print('  built with --no-kit, skipped')
        return 0
    submits = []
    uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: submits.append(r.u32(r.u32(u.reg_read(UC_ARM_REG_R1)))),
                begin=0x08157578, end=0x08157578)
    uc.ctl_flush_tb()
    ui = lambda off, n=1: list(uc.mem_read(sysemu.UI + off, n))
    page = ui(0xE9)[0]
    notes = [page * 8 + c for c in range(8)]

    def chord(key):
        submits.clear()
        m.press(26); m.settle(30); m.press(25); m.settle(30)
        m.press(key); m.settle(40); m.release(key); m.settle(30)
        m.release(25); m.settle(30); m.release(26); m.settle(30)

    def scale(index, root):
        m.submit_event(PATTERN_PARAM_VT, struct.pack('<HBBB', 0, 0, 4, index)); m.settle(20)
        m.submit_event(PATTERN_PARAM_VT, struct.pack('<HBBB', 0, 0, 5, root)); m.settle(20)

    def pitches():
        semi, octv = ui(0x98 + page * 8, 8), ui(0xC0 + page * 8, 8)
        return [s + ((o ^ 0x80) - 0x80 + 5) * 12 for s, o in zip(semi, octv)]

    def bass_kit(label, want_pitch):
        before = sorted(x for x in r.records()[0] if x[0] not in notes)
        kits, n0 = r.diag(D_MNRAND_KITS), r.diag(D_MNRAND_N)
        chord(37)
        kinds = list(uc.mem_read(DIAG + D_MNRAND_KIND + page * 8, 8))
        names = [T.NAMES[k - 1] if k else '-' for k in kinds]
        recs = r.records()[0]
        rec = [{(s, l): v for n, s, l, v in recs if n == note} for note in notes]
        get = lambda c, s, l=0: rec[c].get((s, l), T.reset_value(s, l))
        got = pitches()
        # the sounding oscillator: VCO2 when VCO1 is quiet (an FM bass's
        # carrier is VCO2; its VCO1 is the modulator, at most 0x2000)
        osc = [get(c, 4) if get(c, 3) < 0x3000 else get(c, 0) for c in range(8)]
        # the bouncing octave's VCO1 sits halfway between its two notes (p66):
        # its root is half the interval under the base, the interval read
        # off the square's depth
        hop = {0x8000 + int((st / 126.0) ** (1 / 3) * 32767 + 0.5): int(st * 256) for st in (3.5, 6, 12)}
        osc = [osc[c] - hop[get(c, 0, 1)] if names[c] == 'bounce' else osc[c] for c in range(8)]
        degree_ok = all((got[c] + osc[c] // 256 - 57 - want_pitch[c]) % 12 == 0 for c in range(8))
        shapes = len({tuple(sorted(x.items())) for x in rec})
        check('%s: %s' % (label, ' '.join(names)),
              (r.diag(D_MNRAND_KITS) - kits, r.diag(D_MNRAND_N) - n0, len(set(names)),
               all(T.GROUP.get(n, 0) & 24 for n in names), all(rec)),
              (0x100, 8, 8, True, True))
        check('  pitches %s, every bass on its degree, 8 sounds, nothing else touched' % got,
              (got, degree_ok, shapes, T.RAND_PATCH_VT in submits,
               sorted(x for x in r.records()[0] if x[0] not in notes) == before),
              (want_pitch, True, 8, False, True))

    print('1. D#: a bass kit in A minor')
    scale(6, 9)
    a_minor = [57, 59, 60, 62, 64, 65, 67, 69]
    for _ in range(3):
        bass_kit('A minor', a_minor)
    # the note the engine is given when the key plays (project+0x5FCC)
    started = []
    uc.hook_add(UC_HOOK_CODE, lambda u, a, s_, d: started.append(
        struct.unpack('<f', bytes(u.mem_read(0x200047F0 + 0x5FCC, 4)))[0]), begin=0x081173C0, end=0x081173C0)
    uc.ctl_flush_tb()
    for key, col in ((34, 0), (38, 2), (46, 7)):
        started.clear()
        m.press(key); m.settle(40); m.release(key); m.settle(30)
        check('  key %d plays note %s (degree %d)' % (key, started[:1], a_minor[col]),
              started[:1], [float(a_minor[col])])

    print('2. no scale: C major on the keys')
    scale(0, 0)
    bass_kit('chromatic', [60, 62, 64, 65, 67, 69, 71, 72])

    print('3. a five-note scale')
    mgr = MGR
    five = None
    for idx in range(1, 20):
        d = list(uc.mem_read(mgr + 0x6C + idx * 14, 14))
        if d[0] == 5 and d[1] == 0:
            five = (idx, sorted(d[1:6]))
            break
    if five is None:
        check('a five-note scale in the table', None, 'found')
    else:
        idx, deg = five
        scale(idx, 7)                                       # on G: base G3 = 55
        want = [55 + deg[c % 5] + 12 * (c // 5) for c in range(8)]
        bass_kit('scale %d %s on G' % (idx, deg), want)

    print('4. A#: every mod note of every page')
    kits, n0, err0 = r.diag(D_MNRAND_KITS), r.diag(D_MNRAND_N), len(m.errors)
    heap0 = r.diag(0x34)
    entry = r.u32(0x08175200 + 0x1C8) & ~1
    inside, done, ret = [False], [0], [0]

    def enter(u, a, s, d):
        inside[0], ret[0] = True, u.reg_read(UC_ARM_REG_LR) & ~1

    def block(u, a, s, d):
        if inside[0]:
            if a == ret[0]:
                inside[0] = False
            else:
                done[0] += s
    h1 = uc.hook_add(UC_HOOK_CODE, enter, begin=entry, end=entry)
    h2 = uc.hook_add(UC_HOOK_BLOCK, block)
    uc.ctl_flush_tb()                                  # new hooks reach only fresh translations
    chord(44)
    uc.hook_del(h1)
    uc.hook_del(h2)
    kinds = list(uc.mem_read(DIAG + D_MNRAND_KIND, 40))
    recs, length = r.records()
    per = [sum(1 for x in recs if x[0] == n) for n in range(40)]
    names = [T.NAMES[k - 1] for k in kinds if k]
    print('    ' + ', '.join('%s %d' % (n, names.count(n)) for n in sorted(set(names), key=lambda n: -names.count(n))))
    check('40 instruments (%d kinds), %d..%d records each, list %d long'
          % (len(set(names)), min(per), max(per), length),
          (r.diag(D_MNRAND_KITS) - kits, r.diag(D_MNRAND_N) - n0, all(kinds), min(per) >= 8,
           len(m.errors) - err0, T.RAND_PATCH_VT in submits),
          (0x10000, 40, True, True, 0, False))
    # ~3 bytes of Thumb-2 an instruction, ~1 instruction a cycle at 400 MHz
    print('    heap high-water 0x%x -> 0x%x; main busy ~%.0f ms (%d k instructions)'
          % (heap0, r.diag(0x34), done[0] / 3 / 400e3, done[0] / 3 / 1000))

    print('4b. F#: every mod note back to C4, the sounds kept (p53)')
    before_pitch, before_recs = pitches(), sorted(r.records()[0])
    submits.clear()
    chord(40)
    allp = [s + ((o ^ 0x80) - 0x80 + 5) * 12 for s, o in zip(ui(0x98, 40), ui(0xC0, 40))]
    started.clear()
    m.press(46); m.settle(40); m.release(46); m.settle(30)
    check('  pitches %s -> all 40 at 60, records untouched, no event, key C\' plays %s' % (before_pitch, started[:1]),
          (before_pitch != [60] * 8, allp == [60] * 40, sorted(r.records()[0]) == before_recs,
           T.RAND_PATCH_VT in submits, started[:1]),
          (True, True, True, False, [60.0]))

    print('4c. G#: every mod note in the page\'s style; A# clears it (p56)')
    styles = ['techno', 'dub', 'acid', 'electro', 'ambient']
    kits, n0 = r.diag(D_MNRAND_KITS), r.diag(D_MNRAND_N)
    chord(42)
    for _ in range(60):                       # a styled set takes ~2x A#'s time
        if r.diag(D_MNRAND_KITS) != kits:
            break
        m.settle(10)
    style_g = r.diag(0x1BFC) >> 24
    names = [T.NAMES[k - 1] for k in uc.mem_read(DIAG + D_MNRAND_KIND, 40) if k]
    kits_g, n_g = r.diag(D_MNRAND_KITS) - kits, r.diag(D_MNRAND_N) - n0
    chord(44)
    for _ in range(60):
        if r.diag(D_MNRAND_KITS) != kits + kits_g:
            break
        m.settle(10)
    check('  G# on page %d: %s, 40 new (%d kinds); A# then: style %d'
          % (page + 1, styles[style_g - 1] if 0 < style_g <= 5 else style_g, len(set(names)), r.diag(0x1BFC) >> 24),
          (style_g, kits_g, n_g, len(names), r.diag(0x1BFC) >> 24, r.diag(D_MNRAND_KITS) - kits - kits_g),
          (page + 1, 0x1000000, 40, 40, 0, 0x10000))

    print('5. normal mode: D# and A# are a patch (P1)')
    m.press(28); m.settle(30); m.press(29); m.settle(30); m.release(28); m.settle(30); m.release(29); m.settle(60)
    p0, kits = r.diag(D_PATCHES), r.diag(D_MNRAND_KITS)
    chord(37)
    chord(44)
    check('mode %d: two patches, no kit' % ui(0x84)[0],
          (ui(0x84)[0], r.diag(D_PATCHES) - p0, r.diag(D_MNRAND_KITS) - kits), (1, 2, 0))

    print('%s -- the bass kit and the whole set (%d checks)' % ('PASS' if fail == 0 else 'FAIL', ok + fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
