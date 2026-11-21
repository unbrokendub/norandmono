#!/usr/bin/env python3
"""R1: TEMPO+FUNC+note builds an instrument, not thirty random knobs.

np_modnote_random (patch/np_mnrand.cpp) replaces the stock mod-note
randomizer 0x0814476C.  It picks one of 33 instruments by weight,
clears the mod note and writes the recipe's parameters as ModNoteParamData
events, tagged and dispatched the way the stock handler does it.

sysemu, mod-note mode:

  1. the chord: TEMPO (key 25) + FUNC (26) + a note key randomizes the mod
     note that key plays;
  2. every instrument, forced through D_MNRAND_FORCE, twice (NP_DRAWS): the mod note's
     records are exactly what the handler counted, none is a reset value,
     no other mod note is touched -- and the note, played, behaves like the
     instrument (a kick's pitch drop, a hat's open high-pass and short
     decay, a pad's slow attack, a clap's volume bursts, ...);
  3. 480 free draws: every instrument comes up, roughly by weight, and the
     project's record list does not grow -- the clear frees the old sound;
  4. the stock handler, written back: thirty-odd knobs, no waveform, type
     or routing, and D_MNRAND_N does not move (what R1 replaces);
  5. R2, black + white: a white key pressed while black keys are held
     builds an instrument from their group (p49: C# kick, C#+D# hats/clap/
     snare, D# toms and other drums, F# deep basses, F#+G# other basses, G#
     tonal, A# zaps/blips/glitch, F#+A# the rest; another pair = both) in that key's
     mod note before its note-on, so the press plays the new sound; with
     RECORD lit it records no step, press or release; a plain white key
     neither generates nor stops recording;
  6. K1, TEMPO+FUNC+C#: a drum kit over the page's eight mod notes (kick,
     snare, clap, closed and open hat, tom, perc, cymbal or shaker), the
     hats sharing one metal, the tom over the kick; nothing else touched,
     no stock event; TEMPO+FUNC+F# does nothing yet (D#, A#: test_kits.py);
  7. S1, the pattern's scale: in A minor every interval lands in the
     scale, the kick on the root or the fifth, the drop's ringing filter
     on a scale tone.
"""
import math
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sysemu
from unicorn import UC_HOOK_CODE
from test_modnote_ui import booted

IMAGE = 'build/firmware-patched.bin'
STOCK = 'build/firmware.bin'
E = 0x20004834
DIAG = 0x2000E400
D_MNRAND_N, D_MNRAND_LAST, D_MNRAND_FORCE = 0xFE8, 0xFEC, 0xFF0
MGR = 0x2000C68C
RAND_PATCH_VT = 0x08170188
ENTRY, ENTRY_END = 0x0814476C, 0x08144CBC
NAMES = ('kick snare clap hat-closed hat-open tom perc bass acid lead pad pluck '
         'bell zap stab glitch '
         'cymbal shaker drop fm-bass reese wobble growl sync-lead hoover phaser-pad '
         'gate fm-ep mallet bleeps riser siren drone '
         'metal am-bell talk wind crackle 808 '
         'cowbell rim conga chord syncussion '
         'organ gong bounce vargan '
         'rumble brass flute bowl sub deep').split()
WEIGHT = (12, 9, 6, 9, 5, 5, 4, 9, 5, 4, 4, 5, 4, 4, 4, 5,
          4, 4, 3, 6, 4, 5, 4, 4, 3, 3, 4, 4, 4, 4, 3, 3, 3,
          4, 3, 3, 2, 2, 5,
          3, 4, 4, 4,                                  # p54: cowbell rim conga chord; perc 6 -> 4
          4,                                           # p64: syncussion
          4, 3, 4, 3,                                  # p66: organ gong bounce vargan
          3, 4, 3, 3,                                  # p69: rumble brass flute bowl
          4, 4)                                        # p70: sub deep
# R2 (p49 layout): C# 1 kick, C#+D# 2 hats/clap/snare, D# 4 toms
# and the other drums, F# 8 deep basses, F#+G# 16 the other basses, G# 32
# tonal, A# 64 zaps/blips/glitch, F#+A# 128 everything else
GROUP = dict(zip(NAMES, (1, 2, 2, 2, 2, 4, 4, 8, 16, 32, 32, 288, 288, 64, 288, 64,
                         4, 4, 4, 520, 8, 16, 16, 32, 32, 32, 32, 288, 288, 64, 128, 128, 32,
                         4, 288, 32, 128, 128, 8,
                         4, 4, 4, 288,
                         1024,
                         32, 288, 16, 32,      # p66: organ gong bounce vargan
                         136, 32, 32, 288,     # p69: rumble brass flute bowl
                         8, 8)))               # p70: sub deep.  p51: 256 = G#+A#, plucked/struck tonal; p52: 512 = D#+F#, FM bass; p64: 1024 = C#+F#, Syncussion
BLACK = {35: 1, 37: 4, 40: 8, 42: 32, 44: 64}                 # one key
COMBO = {frozenset((35, 37)): 2, frozenset((40, 42)): 16, frozenset((40, 44)): 128, frozenset((42, 44)): 256,
         frozenset((37, 40)): 512, frozenset((35, 40)): 1024}


def black_want(blacks):
    """the groups a held set of black keys picks: a pair of its own, else the union"""
    s = frozenset(blacks)
    if s in COMBO:
        return COMBO[s]
    want = 0
    for b in blacks:
        want |= BLACK[b]
    return want
RECSEQ_VT = (0x081641E4, 0x0815EE34)        # what a recording press / release submit
DEFAULTS = struct.unpack_from('<19H', Path(STOCK).read_bytes(), 0x0815E8A0 - 0x08100000)
ok = fail = 0
DRAWS = int(os.environ.get('NP_DRAWS', '2'))      # forced draws per instrument
FREE = 480                                       # free draws for the weights


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print('  %-72s ok' % label)
    else:
        fail += 1
        print('  %-72s FAIL got %r want %r' % (label, got, want))


def reset_value(slot, local):
    return DEFAULTS[slot] if local == 0 else 0x8000 if local <= 5 else 0


class Rig:
    def __init__(self, stock_entry=False):
        self.m = m = booted(IMAGE)
        self.uc = uc = m.uc
        if stock_entry:
            # the whole stock function: past its entry the image holds
            # generator code of ours (HL, p66)
            stock = Path(STOCK).read_bytes()
            uc.mem_write(ENTRY, stock[ENTRY - 0x08100000:ENTRY_END - 0x08100000])
        self.trace, self.starts = [], []
        # VCO1 pitch, VCO2 pitch, cutoff, output volume (+0x2EDC..+0x2EE8)
        # and the amp ADSR level (+0x2F2C), one read per sample
        unpack = struct.Struct('<4f64xf').unpack
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: self.trace.append(
            unpack(bytes(u.mem_read(E + 0x2EDC, 0x54)))),
            begin=0x08115040, end=0x08115040)
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: self.starts.append(len(self.trace)),
                    begin=0x081173C0, end=0x081173C0)
        uc.ctl_flush_tb()
        if uc.mem_read(sysemu.UI + 0x84, 1)[0] != 2:              # UP+DOWN: mod-note mode
            m.press(28); m.settle(30); m.press(29); m.settle(30)
            m.release(28); m.settle(30); m.release(29); m.settle(60)

    def u32(self, a):
        return struct.unpack('<I', bytes(self.uc.mem_read(a, 4)))[0]

    def diag(self, off, value=None):
        if value is not None:
            self.uc.mem_write(DIAG + off, struct.pack('<I', value))
        return self.u32(DIAG + off)

    def index(self, key):
        col = [0, 255, 1, 255, 2, 3, 255, 4, 255, 5, 255, 6, 7][key - 34]
        return col + 8 * self.uc.mem_read(sysemu.UI + 0xE9, 1)[0]

    def records(self):
        """the project's parameter records: [(note, slot, local, raw)] of type 7"""
        out, rec, n = [], self.u32(MGR + 0x1BC), 0
        while rec and n < 20000:
            b = bytes(self.uc.mem_read(rec, 12))
            if b[5] == 7:
                out.append((b[4], b[6], b[7], b[8] | b[9] << 8))
            rec, n = self.u32(rec), n + 1
        return out, n

    def randomize(self, note, force=0, settle=10):
        self.diag(D_MNRAND_FORCE, force)
        self.m.submit_event(RAND_PATCH_VT, struct.pack('<HB', 0, note))
        self.m.settle(settle)

    def play(self, key, hold_ms, after_ms):
        m = self.m
        self.trace.clear(); self.starts.clear()
        m.press(key); m.settle(hold_ms, audio=1); m.release(key); m.settle(after_ms, audio=1)
        return self.trace[self.starts[0]:] if self.starts else []


def at(t, ms, col):
    return t[min(len(t) - 1, int(ms * 32))][col]


def fell_below(t, level, start_ms=0):
    """ms at which the amp level first drops under `level` after its peak"""
    amp = [x[4] for x in t]
    pk = max(range(len(amp)), key=lambda i: amp[i])
    pk = max(pk, int(start_ms * 32))
    return next((i / 32 for i in range(pk, len(amp)) if amp[i] < level), None)


def behaves(name, rec, t):
    """(label, passed) for what makes this instrument this instrument (p42:
    the ranges are wide; these are the things every variant keeps).  p49:
    every drawn range is an eighth of its width wider each side, so a bound
    taken from a recipe's range is that range's widened edge."""
    p = {(s, l): v for s, l, v in rec}
    get = lambda s, l: p.get((s, l), reset_value(s, l))
    base1 = 60 + get(0, 0) / 256 - 57
    base2 = 60 + get(4, 0) / 256 - 57
    amp = lambda ms: at(t, ms, 4)
    peak = max(x[4] for x in t)
    first = t[:160]                                           # 5 ms
    if name == 'kick':
        if get(3, 0) == 0 and get(7, 0) >= 0x3000:          # FM kick: the body is VCO2
            col, base = 1, base2
        else:
            col, base = 0, base1
        if get(3, 0) == 0 and get(7, 0) < 0x3000:           # pinged: the filter drops
            drop = max(x[2] for x in first) - at(t, 600, 2)
            return ('pinged filter, cutoff drops %.0f, blip at %.0f' % (drop, base1),
                    24 <= base1 <= 40 and drop >= 8)
        drop = max(x[col] for x in first) - base
        return ('pitch %.0f, +%.0f st drop, settled %.1f at 600 ms'
                % (base, drop, at(t, 600, col) - base),
                24 <= base <= 40 and drop >= 3 and abs(at(t, 600, col) - base) < 1.0)
    if name == 'snare':
        xs = [x[1] for x in t[:1600]]
        mu = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / len(xs))
        return ('VCO2 noise sd %.0f st, body %.0f, %.2f of its peak at 600 ms' % (sd, base1, amp(600) / peak),
                sd > 5 and 41 <= base1 <= 64 and amp(600) < 0.7 * peak)
    if name == 'clap':
        # engine+0x2EE8 is the output level after a steep curve (OutVol 0.25
        # reads 0.89, 0.5 0.95, 1.0 0.99): a dip of 0.1 there is a deep one
        burst = [x[3] for x in t[64:960]]
        tail = [x[3] for x in t[3200:4800]]
        b, tl = max(burst) - min(burst), max(tail) - min(tail)
        return ('volume swing %.2f in the bursts, %.2f in the tail' % (b, tl),
                b >= 0.1 and b >= 3 * tl)
    if name in ('hat-closed', 'hat-open'):
        end = fell_below(t, 0.1)
        colour = get(11, 0)
        return ('colour %04x, cutoff %.0f, gone at %s ms (gate 300)' % (colour, at(t, 1, 2), end),
                (colour >= 0xB800 or colour <= 0x2400) and at(t, 1, 2) >= 98 and
                ((end is not None and end < 260) if name == 'hat-closed' else amp(100) > 0.1))
    if name == 'tom':
        drop = max(x[0] for x in first) - base1
        return ('pitch %.0f, +%.1f st drop' % (base1, drop), 30 <= base1 <= 64 and drop >= 1.5)
    if name == 'perc':
        return ('a hit: no sustain', get(16, 0) == 0)
    if name == 'bass':
        # p70: deepen() may take it an octave or a fifth lower
        return ('pitch %.0f' % base1, base1 in (24, 29, 36, 41, 48))
    if name == 'acid':
        sweep = max(x[2] for x in t[:960]) - at(t, 290, 2)
        # a per-step S&H on ENV AMOUNT (humanize, half the acids) can take
        # most of a low envelope away on one step: seen down to 8
        sh = get(12, 7) == 1 and get(12, 6) == 4
        return ('resonance %04x, filter sweep %.0f%s' % (get(10, 0), sweep, ', S&H on the env' if sh else ''),
                get(10, 0) >= 0x8380 and sweep >= (4 if sh else 8))
    if name == 'lead':
        return ('sustains at %.2f' % amp(280), amp(280) >= 0.18)
    if name == 'pad':
        return ('slow attack: %.2f at 20 ms, %.2f at 290 ms' % (amp(20), amp(290)),
                amp(20) < 0.5 and amp(290) > amp(20))
    if name == 'pluck':
        return ('no sustain, %.2f at 30 ms -> %.2f at 600 ms' % (amp(30), amp(600)),
                get(16, 0) == 0 and amp(600) < amp(30))
    if name == 'bell':
        fm = get(8, 4) > 0x8000 or get(4, 7) == 3 or get(7, 7) == 3
        return ('pinged FM (TZFM %04x, VCO2 X-Mod type %d), %.2f at 150 ms' % (get(8, 4), get(4, 7), amp(150)),
                fm and amp(150) > 0.15)
    if name == 'zap':
        swing = max((abs(x[0] - base1) for x in first))
        return ('pitch swing %.0f st' % swing, swing >= 10)
    if name == 'stab':
        iv = base2 - base1
        return ('interval %d' % iv, iv in (3, 4, 5, 7, 8, 9, 10, 12, 14, 15, 16, 17, 0, -12))
    if name == 'glitch':
        mods = sum(1 for s in range(18) if get(s, 1) != 0x8000)
        return ('%d modulated destinations' % mods, mods >= 1)
    return behaves_p39(name, get, t, base1, base2)


def span(t, col, a_ms, b_ms):
    xs = [x[col] for x in t[int(a_ms * 32):int(b_ms * 32)]]
    return max(xs) - min(xs)


def behaves_p39(name, get, t, base1, base2):
    amp = lambda ms: at(t, ms, 4)
    if name == 'cymbal':
        return ('high-pass colour %04x, still %.2f at 290 ms' % (get(11, 0), amp(290)),
                get(11, 0) >= 0xBA00 and amp(290) > 0.2)
    if name == 'shaker':
        end = fell_below(t, 0.1)
        return ('swells %.2f -> %.2f at 20 ms, gone at %s ms' % (amp(1), amp(20), end),
                amp(1) < amp(20) and end is not None and end < 300)
    if name == 'drop':
        sweep = abs(at(t, 1, 2) - at(t, 150, 2))
        return ('resonance %04x, cutoff sweep %.0f' % (get(10, 0), sweep),
                get(10, 0) >= 0xF280 and sweep >= 4)
    if name == 'fm-bass':
        ratio = get(4, 7) == 3
        tzfm = get(8, 4) > 0x8000
        return ('carrier %.0f, ratio FM %s (swing %.1f st), TZFM %s' % (base2, ratio, span(t, 1, 0, 20), tzfm),
                base2 in (24, 36, 48) and get(3, 0) <= 0x2700 and (ratio or tzfm) and
                (not ratio or span(t, 1, 0, 20) >= 2))
    if name == 'reese':
        return ('detune %+d, drifting, sustains %.2f' % (get(5, 0) - 0x7FFF, amp(280)),
                abs(get(5, 0) - 0x7FFF) >= 1500 and get(5, 1) != 0x8000 and amp(280) >= 0.3)
    if name == 'wobble':
        synced = [s_ for s_ in (9, 11, 2) if get(s_, 7) == 1 and get(s_, 1) != 0x8000]
        return ('synced wobble on %s, cutoff moves %.0f' % (synced, span(t, 2, 50, 300)), bool(synced))
    if name == 'growl':
        return ('cutoff FM %.1f' % span(t, 2, 100, 250), span(t, 2, 100, 250) >= 3)
    if name == 'sync-lead':
        return ('sync, VCO2 %+.0f st over VCO1' % (base2 - base1),
                get(18, 0) == 1 and 3 <= base2 - base1 <= 36)
    if name == 'hoover':
        scoop = base1 - min(x[0] for x in t[:160])
        return ('scoops up from %.1f st below' % scoop, scoop >= 1)
    if name == 'phaser-pad':
        return ('all-pass colour %04x, cutoff sweeping %.1f' % (get(11, 0), span(t, 2, 50, 300)),
                0xAC00 <= get(11, 0) <= 0xD400 and get(9, 1) != 0x8000)
    if name == 'gate':
        # a synced modulator on the volume; down to 1/4 (0.25 s a half at
        # 120 BPM), so look at the whole note, not its first 0.3 s
        return ('synced volume X-Mod, chopped %.2f' % span(t, 3, 50, len(t) / 32),
                get(17, 7) == 1 and get(17, 1) >= 0xE900 and span(t, 3, 50, len(t) / 32) >= 0.05)
    if name == 'fm-ep':
        return ('tine %.1f st, rings %.2f at 290 ms' % (span(t, 1, 0, 20), amp(290)),
                span(t, 1, 0, 20) >= 1 and amp(290) > 0.25)
    if name == 'mallet':
        return ('strike %.1f st' % span(t, 1, 0, 20), span(t, 1, 0, 20) >= 1.0)
    if name == 'bleeps':
        steps = len({round(x[0], 1) for x in t[:9600:160]})
        return ('%d pitches in 300 ms' % steps, steps >= 2)
    if name == 'riser':
        return ('X-Envs on the pitch and the cutoff, the pitch moved %.2f st'
                % abs(at(t, 280, 0) - at(t, 5, 0)),
                get(0, 4) != 0x8000 and get(9, 4) != 0x8000 and abs(at(t, 280, 0) - at(t, 5, 0)) > 0.05)
    if name == 'siren':
        return ('pitch sweeps %.1f st' % span(t, 0, 0, 300), span(t, 0, 0, 300) >= 0.8)
    if name == 'drone':
        return ('slow LFOs on TZFM/wave/cutoff', get(8, 1) != 0x8000 and get(9, 1) != 0x8000)
    if name == 'metal':
        return ('TZFM %04x, VCO1 ratio FM, no sustain' % get(8, 0),
                get(8, 0) >= 0x0C00 and get(0, 7) == 3 and get(16, 0) == 0)
    if name == 'am-bell':
        return ('VCO2 level at a ratio, rings %.2f at 150 ms' % amp(150), get(7, 7) == 3 and amp(150) > 0.1)
    if name == 'talk':
        moving = get(9, 1) != 0x8000 or get(9, 4) != 0x8000 or get(11, 1) != 0x8000
        return ('band-pass %04x, resonance %04x, moving %s' % (get(11, 0), get(10, 0), moving),
                get(11, 0) <= 0x3E00 and get(10, 0) >= 0x8700 and moving)
    if name == 'wind':
        return ('noise, a drifting cutoff, %.2f at 20 ms' % amp(20),
                get(4, 6) == 4 and get(4, 7) == 2 and get(9, 1) != 0x8000 and amp(20) < 0.3)
    if name == 'crackle':
        return ('S&H on the volume at audio rate', get(17, 6) == 4 and get(17, 7) == 2)
    if name == '808':
        return ('pitch %.0f, a drop, holds %.2f' % (base1, amp(280)),
                base1 in (24, 29, 36, 41, 48) and get(0, 4) > 0x8000 and amp(280) >= 0.18)
    if name == 'organ':
        return ('stops %+.0f st apart, holds %.2f at 280 ms' % (base2 - base1, amp(280)),
                base2 - base1 in (12, 19, 24, -12) and amp(280) >= 0.8)
    if name == 'gong':
        return ('TZFM blooms (X-Env attack %04x), still %.2f at 600 ms' % (get(8, 3), amp(600)),
                get(8, 3) > 0x2000 and get(8, 4) > 0x8000 and amp(600) > 0.3)
    if name == 'bounce':
        # a synced square on VCO1's pitch: two levels, a fifth, an octave or two apart
        p1 = sorted(x[0] for x in t[160:int(560 * 32)])
        lo, hi = p1[len(p1) // 50], p1[-1 - len(p1) // 50]
        on = sum(1 for v in p1 if abs(v - lo) < 0.25 or abs(v - hi) < 0.25) / len(p1)
        return ('hops %.2f st (%.1f <-> %.1f), %.0f %% of the time on one of them' % (hi - lo, lo, hi, 100 * on),
                any(abs(hi - lo - iv) < 0.1 for iv in (7, 12, 24)) and on > 0.9 and any(abs(lo - r) < 0.1 for r in (36, 48)))
    if name == 'vargan':
        return ('colour %04x, resonance %04x, cutoff moves %.1f' % (get(11, 0), get(10, 0), span(t, 2, 0, 300)),
                get(11, 0) <= 0x7100 and get(10, 0) >= 0xA000 and span(t, 2, 0, 300) >= 2)   # 0x6000 + p61's 30 %
    if name == 'sub':
        # a low sine, nothing under C1
        return ('pitch %.0f, wave %04x' % (base1, get(2, 0)), 24 <= base1 <= 36 and get(2, 0) <= 0x3600)
    if name == 'deep':
        # a sine an octave under VCO1 (deepen() moves both alike), the filter low
        return ('VCO1 %.0f, sub %.0f' % (base1, base2), base2 == base1 - 12 and base2 >= 24)
    if name == 'rumble':
        # low, held, and the volume pumping on a synced ramp
        return ('pitch %.0f, volume X-Mod wave %d type %d, holds %.2f' % (base1, get(17, 6), get(17, 7), amp(280)),
                base1 in (24, 36) and get(17, 6) == 3 and get(17, 7) == 1 and amp(280) >= 0.3)
    if name == 'brass':
        # the filter blatted open by the envelope, both VCOs scooping up
        return ('ENV AMOUNT %04x, scoop %+d raw on both' % (get(12, 0), get(0, 4) - 0x8000),
                get(12, 0) > 0x9000 and get(0, 4) < 0x8000 and get(4, 4) == get(0, 4))
    if name == 'flute':
        # breath (VCO2 noised) and a vibrato whose depth an X-Env fades in
        return ('breath S&H on VCO2, vibrato depth X-Env dest %d, holds %.2f' % (get(0, 8), amp(280)),
                get(4, 6) == 4 and get(4, 7) == 2 and get(0, 8) == 1 and amp(280) >= 0.4)
    if name == 'bowl':
        # sines, a slow tremolo, still ringing
        return ('tremolo %04x, %.2f at 280 ms' % (get(17, 1), amp(280)),
                get(17, 1) != 0x8000 and get(17, 7) == 0 and amp(280) > 0.3)
    if name == 'syncussion':
        # one sweep on both VCOs and (in cutoff units) on the cutoff; a
        # low-pass without resonance; decay only
        a1, a2, ac = get(0, 4), get(4, 4), get(9, 4)
        tracked = a1 == a2 and abs((ac - 0x8000) - (a1 - 0x8000) * 121 // 128) <= 1
        return ('sweep %+d raw on both VCOs, cutoff %+d, TZFM %04x' % (a1 - 0x8000, ac - 0x8000, get(8, 0)),
                tracked and a1 != 0x8000 and get(10, 0) == 0 and get(11, 0) == 0x8000 and get(16, 0) == 0)
    return ('?', False)


def main():
    print('1. TEMPO + FUNC + note')
    r = Rig()
    m = r.m
    key = 41
    note = r.index(key)
    n0 = r.diag(D_MNRAND_N)
    m.press(26); m.settle(30); m.press(25); m.settle(30)
    m.press(key); m.settle(40); m.release(key); m.settle(30)
    m.release(25); m.settle(30); m.release(26); m.settle(30)
    last = r.diag(D_MNRAND_LAST)
    check('one randomization (%d -> %d)' % (n0, r.diag(D_MNRAND_N)), r.diag(D_MNRAND_N), n0 + 1)
    check('of the key\'s mod note %d (%s)' % (note, NAMES[last & 0xFF]), last >> 8 & 0xFF, note)

    print('2. every instrument, %s' % ('twice' if DRAWS == 2 else '%d times' % DRAWS))
    other = r.index(34)
    r.randomize(other, force=8)                          # someone else's bass
    m.settle(20)
    # the two draws on two mod notes: a pad's 3 s release is still sounding
    # when the next one plays, and a mod note retriggered continues from its
    # own tail (stock); a switch fades it out first (CH), so each starts
    # from silence
    for i, name in enumerate(NAMES):
        for draw, key in enumerate((41, 43, 41, 43)[:DRAWS]):
            note = r.index(key)
            before = sorted(x for x in r.records()[0] if x[0] != note)
            r.randomize(note, force=i + 1)
            last = r.diag(D_MNRAND_LAST)
            allrec, _ = r.records()
            rec = [(s, l, v) for n, s, l, v in allrec if n == note]
            keys = [(s, l) for s, l, v in rec]
            sane = (last & 0xFF == i and len(rec) == last >> 16 and len(set(keys)) == len(keys)
                    and all(s < 19 and l < 9 and v != reset_value(s, l) for s, l, v in rec)
                    and sorted(x for x in allrec if x[0] != note) == before)
            t = r.play(key, 300, 400)
            amp = max(x[4] for x in t) if t else 0
            finite = all(all(v == v for v in x) and 0 <= x[0] <= 127 and 0 <= x[1] <= 127
                         and 0 <= x[2] <= 150 for x in t)
            what, good = behaves(name, rec, t)
            # attacks of seconds (an envelope shorter-gated than its attack
            # still completes it) have barely begun in this 0.7 s window
            loud = 0.05 if name in ('riser', 'drone', 'pad', 'phaser-pad', 'wind') else 0.2
            check('%-10s %2d records; %s' % (name, len(rec), what),
                  (sane, finite, amp > loud, good), (True, True, True, True))

    print('3. %d free draws' % FREE)
    key = 41
    note = r.index(key)
    r.diag(D_MNRAND_FORCE, 0)
    count = [0] * len(NAMES)
    _, length0 = r.records()
    own0 = sum(1 for x in r.records()[0] if x[0] == note)
    for _ in range(FREE):
        r.randomize(note, settle=6)
        count[r.diag(D_MNRAND_LAST) & 0xFF] += 1
    total = sum(WEIGHT)
    expect = [FREE * w / total for w in WEIGHT]
    print('    ' + ', '.join('%s %d/%.0f' % (n, c, e) for n, c, e in zip(NAMES, count, expect)))
    check('every instrument came up', all(count), True)
    check('each within 0.2x..3x of its weight',
          all(0.2 * e <= c <= 3 * e for c, e in zip(count, expect)), True)
    rec, length = r.records()
    own = sum(1 for x in rec if x[0] == note)
    check('the record list does not grow: %d -> %d (this note %d -> %d)'
          % (length0, length, own0, own), length - own, length0 - own0)

    print('4. the stock handler written back')
    s = Rig(stock_entry=True)
    n0 = s.diag(D_MNRAND_N)
    s.randomize(note, settle=30)
    rec = [x for x in s.records()[0] if x[0] == note]
    locals_ = {l for n, sl, l, v in rec}
    check('stock: %d knobs, locals %s' % (len(rec), sorted(locals_)),
          (len(rec) >= 40, bool(locals_ & {6, 7, 8})), (True, False))
    check('stock: D_MNRAND_N does not move', s.diag(D_MNRAND_N), n0)

    test_black_keys()
    test_kit_and_scale()
    print('%s -- the randomizer builds instruments (%d checks)' % ('PASS' if fail == 0 else 'FAIL', ok + fail))
    return 1 if fail else 0



def test_black_keys():
    print('5. black + white keys (R2)')
    r = Rig()
    m, uc = r.m, r.uc
    if r.u32(0x081521AA) == struct.unpack_from('<I', Path(STOCK).read_bytes(), 0x081521AA - 0x08100000)[0]:
        print('  built with --no-black-keys, skipped')
        return
    from unicorn.arm_const import UC_ARM_REG_R1
    log = []
    uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: log.append(('submit', r.u32(r.u32(u.reg_read(UC_ARM_REG_R1))))),
                begin=0x08157578, end=0x08157578)
    uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: log.append(('note-on', r.diag(D_MNRAND_N))),
                begin=0x081170E8, end=0x081170E8)
    uc.ctl_flush_tb()

    def chord(blacks, white, hold=40):
        log.clear()
        for b in blacks:
            m.press(b); m.settle(20)
        n0 = r.diag(D_MNRAND_N)
        r.trace.clear(); r.starts.clear()
        m.press(white); m.settle(hold, audio=1); m.release(white); m.settle(20, audio=1)
        for b in blacks:
            m.release(b); m.settle(20)
        return n0, r.diag(D_MNRAND_LAST), list(log)

    key = 41
    note = r.index(key)
    for blacks in ((35,), (35, 37), (37,), (40,), (40, 42), (42,), (44,), (40, 44), (42, 44), (37, 40), (37, 42), (35, 40)):
        want = black_want(blacks)
        got = []
        for _ in range(4):
            n0, last, lg = chord(blacks, key)
            name = NAMES[last & 0xFF]
            got.append(name)
            good = (r.diag(D_MNRAND_N) == n0 + 1 and last >> 24 == want & 0xFF and
                    last >> 8 & 0xFF == note and bool(GROUP[name] & want) and
                    ('note-on', n0 + 1) in lg)
            if not good:
                break
        check('black %-8s -> %s' % ('+'.join(str(b) for b in blacks), ', '.join(got)), good, True)

    r.diag(D_MNRAND_FORCE, 1)                 # a kick: its drop shows what played
    n0, last, lg = chord((35,), 43, hold=700)
    r.diag(D_MNRAND_FORCE, 0)
    t = r.trace[r.starts[0]:]
    rec = [(s_, l, v) for n, s_, l, v in r.records()[0] if n == r.index(43)]
    what, good = behaves('kick', rec, t)
    check('the press plays the new kick: ' + what, good, True)

    n0, last, lg = chord((), key)
    check('a plain white key plays and generates nothing', (r.diag(D_MNRAND_N), [e for e in lg if e[0] == 'note-on'] != []),
          (n0, True))

    m.press(32); m.settle(30); m.release(32); m.settle(30)            # RECORD on
    check('RECORD lit', uc.mem_read(sysemu.UI + 0xED, 1)[0], 1)
    n0, last, lg = chord((), key)
    plain = [e[1] for e in lg if e[0] == 'submit']
    n0, last, lg = chord((35,), key)
    black = [e[1] for e in lg if e[0] == 'submit']
    marks = (r.diag(0xFF4), r.diag(0xFF8))
    n0, last, lg = chord((), key)
    again = [e[1] for e in lg if e[0] == 'submit']
    m.press(32); m.settle(30); m.release(32); m.settle(30)            # RECORD off
    check('RECORD: plain records press+release, black+white nothing, then plain again',
          (plain, black, again, marks),
          (list(RECSEQ_VT), [], list(RECSEQ_VT), (0, 0)))


PATTERN_PARAM_VT = 0x0815EE94          # param 4 = scale, 5 = root (manager+0x18D)
D_MNRAND_KITS, D_MNRAND_KIND = 0xFFC, 0x1BC0
KIT = ['kick', 'snare', 'clap', 'hat-closed', 'hat-open', 'tom', 'perc']


def test_kit_and_scale():
    print('6. TEMPO+FUNC+C#: a drum kit (K1)')
    r = Rig()
    m, uc = r.m, r.uc
    if r.u32(0x081524D4) == struct.unpack_from('<I', Path(STOCK).read_bytes(), 0x081524D4 - 0x08100000)[0]:
        print('  built with --no-kit, skipped')
        return
    from unicorn.arm_const import UC_ARM_REG_R1
    submits = []
    uc.hook_add(UC_HOOK_CODE, lambda u, a, s_, d: submits.append(r.u32(r.u32(u.reg_read(UC_ARM_REG_R1)))),
                begin=0x08157578, end=0x08157578)
    uc.ctl_flush_tb()
    page = uc.mem_read(sysemu.UI + 0xE9, 1)[0]
    notes = [page * 8 + c for c in range(8)]
    r.randomize(r.index(34) - 8 if page else 39, force=8)   # a bass off the page
    m.settle(20)

    def chord(key):
        submits.clear()
        m.press(26); m.settle(30); m.press(25); m.settle(30)
        m.press(key); m.settle(40); m.release(key); m.settle(30)
        m.release(25); m.settle(30); m.release(26); m.settle(30)

    for rep in range(2):
        before = sorted(x for x in r.records()[0] if x[0] not in notes)
        kits, n0 = r.diag(D_MNRAND_KITS), r.diag(D_MNRAND_N)
        chord(35)
        kinds = list(uc.mem_read(DIAG + D_MNRAND_KIND + page * 8, 8))
        names = [NAMES[k - 1] if k else '-' for k in kinds]
        recs = r.records()[0]
        rec = [{(s_, l): v for n, s_, l, v in recs if n == note} for note in notes]
        get = lambda c, s_, l=0: rec[c].get((s_, l), reset_value(s_, l))
        pitch = lambda c, s_: 60 + get(c, s_) / 256 - 57
        check('kit %d: %s' % (rep + 1, ' '.join(names)),
              (r.diag(D_MNRAND_KITS), r.diag(D_MNRAND_N),
               names[:6] + ['perc' if names[6] in ('perc', 'cowbell', 'rim', 'conga') else names[6]],
               names[7] in ('cymbal', 'shaker'),
               all(len(x) >= 8 for x in rec), RAND_PATCH_VT in submits,
               sorted(x for x in recs if x[0] not in notes) == before),
              (kits + 1, n0 + 8, KIT, True, True, False, True))
        metal = [(get(c, 0), get(c, 4), get(c, 5), get(c, 8)) for c in (3, 4)]
        if names[7] == 'cymbal':
            metal.append((get(7, 0), get(7, 4), get(7, 5), get(7, 8)))
        over = pitch(5, 0) - pitch(0, 0)
        check('  one metal for %d hats/cymbal; tom %+d st over the kick' % (len(metal), over),
              (len(set(metal)), over in (7, 12, 19) or -30 <= pitch(5, 0) - 60 <= 4), (1, True))
    n0 = r.diag(D_MNRAND_N)
    chord(40)
    check('TEMPO+FUNC+F# generates nothing, sends no event (p53: pitches to C4)', (r.diag(D_MNRAND_N), submits), (n0, []))

    print('7. the pattern\'s scale (S1)')
    m.submit_event(PATTERN_PARAM_VT, struct.pack('<HBBB', 0, 0, 4, 6)); m.settle(20)   # natural minor
    m.submit_event(PATTERN_PARAM_VT, struct.pack('<HBBB', 0, 0, 5, 9)); m.settle(20)   # on A
    minor, root = {0, 2, 3, 5, 7, 8, 10}, 9
    note = r.index(41)

    def gen(i):
        r.randomize(note, force=NAMES.index(i) + 1, settle=8)
        return {(s_, l): v for n, s_, l, v in r.records()[0] if n == note}
    deg = lambda midi: int(round(midi - root)) % 12
    tone = lambda rec, slot: 60 + rec.get((slot, 0), reset_value(slot, 0)) / 256 - 57
    vco2 = set()
    for name in ('stab', 'gate', 'lead', 'pad', 'drone', 'pluck'):
        for _ in range(6):
            rec = gen(name)
            if not rec.get((18, 0)):        # a synced VCO2's pitch is timbre, not a note
                vco2.add(deg(tone(rec, 4)))
    def kick_pitch(rec):
        body2 = rec.get((3, 0), reset_value(3, 0)) == 0 and rec.get((7, 0), 0) >= 0x3000
        return tone(rec, 4 if body2 else 0)
    kicks = {deg(kick_pitch(gen('kick'))) for _ in range(10)}
    drops = {deg(gen('drop')[(9, 0)] / 256 + 3 - 12) for _ in range(6)}
    check('A minor: VCO2 degrees %s, kick %s, drop %s' % (sorted(vco2), sorted(kicks), sorted(drops)),
          (vco2 <= minor, kicks <= {0, 7}, drops <= minor), (True, True, True))
    m.submit_event(PATTERN_PARAM_VT, struct.pack('<HBBB', 0, 0, 4, 0)); m.settle(20)   # chromatic
    kicks = {round(kick_pitch(gen('kick'))) - 60 for _ in range(12)}
    check('chromatic: the kick anywhere in -36..-20 (%s)' % sorted(kicks),
          (min(kicks) >= -36, max(kicks) <= -20, len(kicks) >= 4), (True, True, True))


if __name__ == '__main__':
    sys.exit(main())
