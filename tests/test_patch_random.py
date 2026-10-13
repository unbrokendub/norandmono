#!/usr/bin/env python3
"""P1: TEMPO+FUNC+note in normal mode builds an instrument into the patch.

np_nm_key_press / np_nm_key_release (patch/np_mnrand.cpp) take the branches
at the end of the thunks that hand a key to the normal-mode press
(0x08151DA4) and release (0x08151FFC).  On TEMPO (key 25) + FUNC (26) + a
note key they build one of the instruments into the pattern's patch --
PatchParamData type 0, what a knob turn writes -- and then the stock press
plays it.  A black key picks the group as R2 does in mod-note mode.

sysemu, normal mode:

  1. the chord: one patch, built before the press's note-on; only
     PatchParamData type 0 to the UI's pattern, no ClearPatchData (its
     handler resets mod note 0 with the patch), nothing queued;
  2. the patch's records are exactly the new sound: every value off the
     reset is a record, every old record the sound does not set is gone
     (the project's VCO1 +6 among them); mod notes 0 and 1, the FOLLOW
     offset layer and the other layer's patch are untouched;
  3. every instrument, forced, built at C4 and played: it behaves like
     itself (the same checks as the mod-note test);
  4. black keys: C# kick, D# other drums, F# basses, G# tonal, A# effects,
     and the press plays the black key's own note;
  5. RECORD lit: a plain press and release record as stock, the chord
     nothing, a plain press after it records again;
  6. no chord, no patch: a plain key, FUNC + key, TEMPO + key; in mod-note
     mode TEMPO+FUNC+note still randomizes the mod note;
  7. 120 free draws: the record list does not grow;
  8. D1, DELETE + RECORD: the patch and its offsets are cleared, mod notes
     0, 1 and 5 keep their sounds (the stock reset mod note 0's).
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sysemu
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R1
from test_modnote_ui import booted
import test_modnote_random as T

IMAGE = 'build/firmware-patched.bin'
STOCK = 'build/firmware.bin'
E = 0x20004834
DIAG = 0x2000E400
MGR = 0x2000C68C
D_MNRAND_N, D_MNRAND_LAST, D_MNRAND_FORCE = 0xFE8, 0xFEC, 0xFF0
D_PMARK, D_PKIND, D_PATCHES = 0x1BEC, 0x1BF0, 0x1BF4
PATCH_PARAM_VT, CLEAR_PATCH_VT = 0x0815EE7C, 0x081648B4
MN_PARAM_VT, MN_CLEAR_VT = 0x08164274, 0x0816489C
TEMPO, FUNC, RECORD, DELETE = 25, 26, 32, 33
ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print('  %-72s ok' % label)
    else:
        fail += 1
        print('  %-72s FAIL got %r want %r' % (label, got, want))


class Rig:
    def __init__(self):
        self.m = m = booted(IMAGE)
        self.uc = uc = m.uc
        self.trace, self.starts, self.log = [], [], []
        unpack = struct.Struct('<4f64xf').unpack
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: self.trace.append(
            unpack(bytes(u.mem_read(E + 0x2EDC, 0x54)))), begin=0x08115040, end=0x08115040)
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: self.starts.append(len(self.trace)),
                    begin=0x081173C0, end=0x081173C0)
        uc.hook_add(UC_HOOK_CODE, self.event, begin=0x081576D4, end=0x081576D4)
        uc.hook_add(UC_HOOK_CODE, self.event, begin=0x08157578, end=0x08157578)
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: self.log.append(('note-on', self.diag(D_PATCHES))),
                    begin=0x081170E8, end=0x081170E8)
        uc.ctl_flush_tb()
        self.mgr = m.call_preserving(0x0811F854)

    def event(self, u, a, s, d):
        p = self.u32(u.reg_read(UC_ARM_REG_R1))          # the slot holds the event
        vt = self.u32(p)
        how = 'sync' if a == 0x081576D4 else 'queue'
        if vt == PATCH_PARAM_VT:
            b = bytes(u.mem_read(p, 16))
            self.log.append((how, vt, b[8], b[9], b[10], b[11], b[12] | b[13] << 8))
        else:
            self.log.append((how, vt))

    def u32(self, a):
        return struct.unpack('<I', bytes(self.uc.mem_read(a, 4)))[0]

    def diag(self, off, value=None):
        if value is not None:
            self.uc.mem_write(DIAG + off, struct.pack('<I', value))
        return self.u32(DIAG + off)

    def ui(self, off):
        return self.uc.mem_read(sysemu.UI + off, 1)[0]

    def records(self):
        """the project's records: [(bank/layer/note, type, slot, local, value)]"""
        out, rec, n = [], self.u32(MGR + 0x1BC), 0
        while rec and n < 20000:
            b = bytes(self.uc.mem_read(rec, 10))
            out.append((b[4], b[5], b[6], b[7], b[8] | b[9] << 8))
            rec, n = self.u32(rec), n + 1
        return out

    def patch(self):
        """layer 0's patch: {(slot, local): value} of its type-0 records"""
        return {(s, l): v for b, t, s, l, v in self.records() if b == 0 and t == 0}

    def sync(self, vt, size, payload):
        m, uc = self.m, self.uc
        ev = m.call_preserving(0x081591D4, size)
        uc.mem_write(ev, struct.pack('<I', vt) + bytes(payload).ljust(size - 4, b'\0'))
        slot = m.call_preserving(0x081591D4, 8)
        uc.mem_write(slot, struct.pack('<I', ev))
        m.call_preserving(0x081576D4, self.mgr, slot)

    def chord(self, key, hold=40, after=30, mods=(FUNC, TEMPO), audio=0):
        m = self.m
        for k in mods:
            m.press(k); m.settle(30)
        self.log.clear(); self.trace.clear(); self.starts.clear()
        m.press(key); m.settle(hold, audio=audio); m.release(key); m.settle(after, audio=audio)
        lg = list(self.log)
        for k in reversed(mods):
            m.release(k); m.settle(30)
        return lg


def main():
    r = Rig()
    m, uc = r.m, r.uc
    check('normal mode, pattern %d' % r.ui(0xEF), r.ui(0x84), 1)
    if r.u32(0x08147B98) == struct.unpack_from('<I', Path(STOCK).read_bytes(), 0x08147B98 - 0x08100000)[0]:
        print('  built with --no-patch-random, skipped')
        return 0

    print('1. TEMPO + FUNC + C')
    pattern = r.ui(0xEF)
    # what must survive: mod notes 0 and 1 (the stock ClearPatchData takes
    # mod note 0 with layer 0's patch), a FOLLOW offset (type 5), layer 1's patch
    r.sync(MN_PARAM_VT, 12, struct.pack('<HBBBxH', 0, 0, 9, 0, 0x3000))
    r.sync(MN_PARAM_VT, 12, struct.pack('<HBBBxH', 0, 1, 9, 0, 0x3001))
    r.sync(PATCH_PARAM_VT, 16, struct.pack('<HHBBBBI', 0, 0, pattern, 5, 5, 0, 0x0300))
    r.sync(PATCH_PARAM_VT, 16, struct.pack('<HHBBBBI', 0, 0, 1, 0, 9, 0, 0x2222))
    m.settle(20)
    before = r.records()
    old = r.patch()
    others = sorted(x for x in before if not (x[0] == 0 and x[1] == 0))
    n0, mn0 = r.diag(D_PATCHES), r.diag(D_MNRAND_N)
    lg = r.chord(34)
    last = r.diag(D_MNRAND_LAST)
    params = [e for e in lg if e[0] == 'sync' and e[1] == PATCH_PARAM_VT]
    other_events = [e for e in lg if e[0] in ('sync', 'queue') and e[1] != PATCH_PARAM_VT]
    check('one patch (%s), no mod note' % T.NAMES[last & 0xFF], (r.diag(D_PATCHES) - n0, r.diag(D_MNRAND_N) - mn0), (1, 0))
    check('LAST: note byte 0xFF, any group, %d events' % len(params), (last >> 8 & 0xFF, last >> 24, last >> 16 & 0xFF),
          (0xFF, 0, len(params)))
    check('only PatchParamData type 0 to pattern %d, nothing else' % pattern,
          (all(e[2] == pattern and e[3] == 0 for e in params), other_events), (True, []))
    on = [e for e in lg if e[0] == 'note-on']
    check('the press plays it: note-on after the build', on[:1], [('note-on', n0 + 1)])
    check('...at C4 (UI+0x94)', r.ui(0x94), 60)

    print('2. the records are the new sound')
    new = r.patch()
    sent = {(s, l): v for _, _, _, _, s, l, v in params}
    sound = {k: v for k, v in sent.items() if v != T.reset_value(*k)}
    check('every value off the reset is a record (%d), nothing else' % len(sound), new, sound)
    gone = sorted(k for k in old if k not in sound)
    check('old records the sound does not set are reset: %s' % gone,
          all(sent.get(k) == T.reset_value(*k) for k in gone), True)
    check('mod notes 0, 1, the FOLLOW offset, layer 1 untouched',
          sorted(x for x in r.records() if not (x[0] == 0 and x[1] == 0)), others)

    print('3. every instrument, built at C4 and played')
    for i, name in enumerate(T.NAMES):
        r.diag(D_MNRAND_FORCE, i + 1)
        lg = r.chord(34, hold=300, after=400, audio=1)
        last = r.diag(D_MNRAND_LAST)
        rec = [(s, l, v) for (s, l), v in r.patch().items()]
        t = r.trace[r.starts[0]:] if r.starts else []
        amp = max(x[4] for x in t) if t else 0
        what, good = T.behaves(name, rec, t)
        loud = 0.05 if name in ('riser', 'drone', 'pad', 'phaser-pad', 'wind') else 0.2
        check('%-10s %2d records; %s' % (name, len(rec), what),
              (last & 0xFF == i, amp > loud, good), (True, True, True))
    r.diag(D_MNRAND_FORCE, 0)

    print('4. black keys pick the group')
    for key, want in sorted(T.BLACK.items()):
        got, good = [], True
        for _ in range(4):
            lg = r.chord(key)
            last = r.diag(D_MNRAND_LAST)
            name = T.NAMES[last & 0xFF]
            got.append(name)
            good = good and last >> 24 == want & 0xFF and bool(T.GROUP[name] & want) and r.ui(0x94) == 60 + key - 34
        check('black %d -> %s' % (key, ', '.join(got)), good, True)
    # p49: a second black key pressed while one is held picks the pair's group
    for held, key in ((40, 42), (40, 44), (35, 37)):
        want = T.black_want((held, key))
        got, good = [], True
        for _ in range(3):
            r.chord(key, mods=(FUNC, TEMPO, held))
            last = r.diag(D_MNRAND_LAST)
            name = T.NAMES[last & 0xFF]
            got.append(name)
            good = good and last >> 24 == want & 0xFF and bool(T.GROUP[name] & want)
        check('black %d held + %d -> %s' % (held, key, ', '.join(got)), good, True)

    print('5. RECORD')
    m.press(RECORD); m.settle(30); m.release(RECORD); m.settle(30)
    check('RECORD lit', r.ui(0xED), 1)
    queued = lambda lg: [e[1] for e in lg if e[0] == 'queue']
    n0 = r.diag(D_PATCHES)
    plain = queued(r.chord(36, mods=()))
    chord = queued(r.chord(36))
    mark = r.diag(D_PMARK)
    again = queued(r.chord(36, mods=()))
    m.press(RECORD); m.settle(30); m.release(RECORD); m.settle(30)
    check('plain records %s, the chord nothing, plain again the same' % ['%08x' % v for v in plain],
          (bool(plain), chord, again == plain, mark, r.diag(D_PATCHES) - n0, r.ui(0xED)),
          (True, [], True, 0, 1, 0))

    print('6. no chord, no patch')
    n0 = r.diag(D_PATCHES)
    lg = r.chord(38, mods=())
    check('a plain key: no patch, the note plays',
          (r.diag(D_PATCHES) - n0, any(e[0] == 'note-on' for e in lg)), (0, True))
    # FUNC or TEMPO with a key is a stock gesture (FUNC + a note opens a
    # settings page, UI mode 3, and a first UP+DOWN only leaves it): what
    # happens must be what the stock thunks do, on two fresh boots
    def singles(rig):
        out = []
        for mods in ((FUNC,), (TEMPO,)):
            n0 = rig.diag(D_PATCHES)
            lg = rig.chord(38, mods=mods)
            out.append((rig.diag(D_PATCHES) - n0, sum(1 for e in lg if e[0] == 'note-on'), rig.ui(0x84)))
        return out
    a, b = Rig(), Rig()
    stock = Path(STOCK).read_bytes()
    for address in (0x08147B98, 0x08147BA0):
        b.uc.mem_write(address, stock[address - 0x08100000:address - 0x08100000 + 4])
    b.uc.ctl_flush_tb()
    got, want = singles(a), singles(b)
    check('FUNC + key, TEMPO + key: no patch, as stock %s' % want, (got, [x[0] for x in got]), (want, [0, 0]))
    for _ in range(3):                                   # leave the page, then mod-note mode
        if a.ui(0x84) == 2:
            break
        a.m.press(28); a.m.settle(30); a.m.press(29); a.m.settle(30)
        a.m.release(28); a.m.settle(30); a.m.release(29); a.m.settle(60)
    n0, mn0 = a.diag(D_PATCHES), a.diag(D_MNRAND_N)
    a.chord(41)
    check('mod-note mode: TEMPO+FUNC+note is the mod note\'s', (a.ui(0x84), a.diag(D_PATCHES) - n0,
                                                               a.diag(D_MNRAND_N) - mn0), (2, 0, 1))
    del a, b

    print('7. 120 free draws')
    length0 = len(r.records()) - len(r.patch())
    count = {}
    for _ in range(120):
        r.chord(34, hold=10, after=10)
        name = T.NAMES[r.diag(D_MNRAND_LAST) & 0xFF]
        count[name] = count.get(name, 0) + 1
    print('    ' + ', '.join('%s %d' % kv for kv in sorted(count.items(), key=lambda kv: -kv[1])))
    check('%d kinds; the record list does not grow' % len(count),
          (len(count) >= 20, len(r.records()) - len(r.patch()), r.ui(0x84)), (True, length0, 1))

    print('8. DELETE + RECORD (D1)')
    if r.u32(0x08127A12) == struct.unpack_from('<I', Path(STOCK).read_bytes(), 0x08127A12 - 0x08100000)[0]:
        print('  built with --no-clear-fix, skipped')
    else:
        for note in (0, 1, 5):
            r.sync(MN_PARAM_VT, 12, struct.pack('<HBBBxH', 0, note, 9, 0, 0x3000 + note))
        r.sync(PATCH_PARAM_VT, 16, struct.pack('<HHBBBBI', 0, 0, pattern, 5, 5, 0, 0x0300))
        m.settle(20)
        mods = sorted(x for x in r.records() if x[1] == 7)
        layer0 = len([x for x in r.records() if x[0] == 0 and x[1] != 7])
        r.log.clear()
        m.press(DELETE); m.settle(30); m.press(RECORD); m.settle(40)
        m.release(RECORD); m.settle(30); m.release(DELETE); m.settle(80)
        check('ClearPatchData sent', any(e[0] == 'queue' and e[1] == CLEAR_PATCH_VT for e in r.log), True)
        check('layer 0\'s patch and offsets cleared (%d records), mod notes kept %s'
              % (layer0, [x[0] for x in mods]),
              ([x for x in r.records() if x[0] == 0 and x[1] != 7], sorted(x for x in r.records() if x[1] == 7)),
              ([], mods))

    print('%s -- TEMPO+FUNC+note builds the patch (%d checks)' % ('PASS' if fail == 0 else 'FAIL', ok + fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
