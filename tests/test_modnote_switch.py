#!/usr/bin/env python3
"""N1, N2 and the held-slot smoother: switching mod notes is instant.

The case: a kick mod note with the filter closed, a hat mod note
with it open, the hat straight after the kick -- the hat must start on its
own open filter, not on a re-tune from closed to open.

Every one of the 19 CV destinations passes its value through a 100 Hz
one-pole TPT smoother (owner+0x1D8 g = tan(pi 100/32000), +0x1DC state,
+0x1E8 input, +0x1EC output): a knob de-zipper in the stock, and a ~4 ms
(90 %) slide from the previous instrument's parameters at every mod-note
switch.  Since p19 the slots evaluated once per block (np_slot_hold) ran
that smoother at the block rate with the per-sample g -- 12.5 Hz, ~30 ms.

sysemu, mod-note mode, a kick (mod note 16) and a hat (17) that differ in
pitch, cutoff, resonance and VCO1 volume; the hat 2 ms after the kick:

  1. this image: every smoother's output equals its input from the first
     sample after the hat's note start -- and the DSP's cutoff (engine+
     0x2EE4) is the hat's from that sample;
  2. this image with N1 written back (no snap): the slide is there, and a
     held slot (resonance) moves at the inline slots' (cutoff's) pace --
     the block-rate g fix, not the 8x slower p19..p35 smoother;
  3. N2: the mod note's parameters applied directly give the same bank
     table and the same 171 parameter values as the stock's one event per
     parameter, in under half the main-loop instructions for a full note.
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sysemu
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_IPSR
from test_modnote_ui import booted

IMAGE = 'build/firmware-patched.bin'
STOCK = 'build/firmware.bin'
P = 0x200047F0
E = P + 0x44
MNPARAM_VT = 0x08164274
FILTFREQ, FILTRESO, VCO1TUNE, VCO1VOL = 9, 10, 0, 3
SNAP_SITES = (0x08116D30, 0x0811503C)
REPLAY_SITE = 0x081209F4
ok = fail = 0
stock = Path(STOCK).read_bytes()


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print('  %-68s ok' % label)
    else:
        fail += 1
        print('  %-68s FAIL got %r want %r' % (label, got, want))


class Rig:
    def __init__(self, restore=()):
        self.m = m = booted(IMAGE)
        self.uc = uc = m.uc
        for a in restore:
            uc.mem_write(a, stock[a - 0x08100000:a - 0x08100000 + 4])
        uc.ctl_flush_tb()
        u32 = lambda a: struct.unpack('<I', bytes(uc.mem_read(a, 4)))[0]
        self.u32 = u32
        self.owners = [u32(E + 0x2E68 + 4 * i) for i in range(19)]
        self.trace = []
        self.starts = []
        f = lambda a: struct.unpack('<f', bytes(uc.mem_read(a, 4)))[0]
        self.f = f
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: self.trace.append(
            ([(f(o + 0x1E8), f(o + 0x1EC)) for o in self.owners], f(E + 0x2EE4))),
            begin=0x08115040, end=0x08115040)
        # the note start's return into the CNoteData handler: params and gates in
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: self.starts.append(len(self.trace)),
                    begin=0x081173C0, end=0x081173C0)
        self.entries = []                 # the CNoteData handler's entry
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: self.entries.append(len(self.trace)),
                    begin=0x081170E8, end=0x081170E8)
        uc.ctl_flush_tb()
        if uc.mem_read(sysemu.UI + 0x84, 1)[0] != 2:              # UP+DOWN: mod-note mode
            m.press(28); m.settle(30); m.press(29); m.settle(30)
            m.release(28); m.settle(30); m.release(29); m.settle(60)

    def index(self, key):
        col = [0, 255, 1, 255, 2, 3, 255, 4, 255, 5, 255, 6, 7][key - 34]
        return col + 8 * self.uc.mem_read(sysemu.UI + 0xE9, 1)[0]

    def param(self, note, slot, local, raw):
        self.m.submit_event(MNPARAM_VT, struct.pack('<HBBBxH', 0, note, slot, local, raw))
        self.m.settle(10)

    def kick_hat(self):
        m = self.m
        kick, hat = self.index(34), self.index(36)
        for slot, k, h in ((VCO1TUNE, 0x3000, 0xD000), (VCO1VOL, 0xF000, 0x8000),
                           (FILTFREQ, 0x0800, 0xF800), (FILTRESO, 0x2000, 0xC000)):
            self.param(kick, slot, 0, k)
            self.param(hat, slot, 0, h)
        m.settle(40, audio=1)
        m.press(34); m.settle(40, audio=1); m.release(34); m.settle(2, audio=1)
        self.trace.clear(); self.starts.clear(); self.entries.clear()
        m.press(36); m.settle(30, audio=1)
        return self.starts[-1]


def progress(rig, b, slot, k):
    old = rig.trace[b - 1][0][slot][1]
    new = rig.trace[b + 400][0][slot][0]
    return (rig.trace[b + k][0][slot][1] - old) / (new - old)


def main():
    print('1. this image: kick -> hat')
    r = Rig()
    b = r.kick_hat()
    exact = all(abs(r.trace[b + k][0][s][1] - r.trace[b + k][0][s][0]) < 1e-5
                for s in (VCO1TUNE, VCO1VOL, FILTFREQ, FILTRESO) for k in range(0, 64))
    check('every smoother output equals its input from the first sample', exact, True)
    check('the DSP cutoff is the hat\'s from the first sample (%.1f)' % r.trace[b][1],
          abs(r.trace[b][1] - r.trace[b + 300][1]) < 0.5, True)
    e = r.entries[-1]
    check('the hat\'s cutoff is open (%.0f), the kick\'s was closed (%.0f)'
          % (r.trace[b][1], r.trace[e - 1][1]), (r.trace[e - 1][1] < 40, r.trace[b][1] > 140), (True, True))
    snaps = struct.unpack('<I', bytes(r.uc.mem_read(0x2000E400 + 0xFC4, 4)))[0]
    check('note starts asked for the snap', snaps > 0, True)

    print('2. N1 written back: the stock\'s slide, at one pace for every slot')
    r = Rig(restore=SNAP_SITES)
    b = r.kick_hat()
    cut32, reso32 = progress(r, b, FILTFREQ, 32), progress(r, b, FILTRESO, 32)
    cut64, reso64 = progress(r, b, FILTFREQ, 64), progress(r, b, FILTRESO, 64)
    check('the cutoff slides: under 60 %% after 1 ms (%.0f %%)' % (100 * cut32), cut32 < 0.6, True)
    check('the held resonance keeps pace with the inline cutoff: %.0f / %.0f %% at 1 ms'
          % (100 * reso32, 100 * cut32), abs(reso32 - cut32) < 0.12, True)
    check('...and at 2 ms: %.0f / %.0f %% (p19..p35: ~15 %% against ~72 %%)'
          % (100 * reso64, 100 * cut64), abs(reso64 - cut64) < 0.12, True)

    print('3. N2: a full mod note, direct against the stock replay')
    outs = []
    for restore in ((REPLAY_SITE,), ()):
        r = Rig(restore=restore)
        uc = r.uc
        st = {'in': False, 'n': 0}
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: st.update(n=st['n'] + 1)
                    if st['in'] and u.reg_read(UC_ARM_REG_IPSR) == 0 else None)
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: st.update({'in': True, 'n': 0}),
                    begin=0x081170E8, end=0x081170E8)
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: st.update({'in': False}),
                    begin=0x081173C0, end=0x081173C0)
        uc.ctl_flush_tb()
        a = r.index(34)
        for slot in range(18):
            for local in range(6):
                r.param(a, slot, local, (0x1234 * (slot + 1) + 0x111 * local) & 0xFFFF)
        r.m.press(34); r.m.settle(30, audio=1); r.m.release(34); r.m.settle(5, audio=1)
        bank = bytes(uc.mem_read(P + 0x4268, 171 * 4)) + bytes(uc.mem_read(P + 0x4268 + 0x180C, 171 * 4))
        params = [bytes(uc.mem_read(r.u32(P + 0x6074 + 4 * i) + 0xC, 4)) for i in range(171)]
        outs.append((bank, params, st['n']))
    check('the same bank tables', outs[0][0] == outs[1][0], True)
    check('the same 171 parameter values', outs[0][1] == outs[1][1], True)
    check('note-on handler %d -> %d instructions (under half)' % (outs[0][2], outs[1][2]),
          outs[1][2] * 2 < outs[0][2], True)
    test_choke()
    print('%s -- mod notes switch instantly (%d checks)' % ('PASS' if fail == 0 else 'FAIL', ok + fail))
    return 1 if fail else 0


def test_choke():
    """CH: a switch while the amp envelope sounds fades it out first, and
    the next instrument starts exactly as it does from silence."""
    samples = struct.unpack_from('<I', Path(IMAGE).read_bytes(), 0x75200 + 0x1B4)[0]
    if samples == 0:
        print('4. CH: built with --choke-ms 0, skipped')
        return
    print('4. CH: the instrument switch fade (%d samples)' % samples)
    r = Rig()
    m, uc = r.m, r.uc
    f = r.f
    level = []
    events = []
    uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: level.append(f(E + 0x2F2C)),
                begin=0x08115040, end=0x08115040)
    uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: events.append(len(level)),
                begin=0x08117A44, end=0x08117A44)
    uc.ctl_flush_tb()
    kick, hat, hat2 = r.index(34), r.index(36), r.index(38)
    for slot, k, h in ((0, 0x3000, 0xD000), (9, 0x0800, 0xF800), (13, 0x0000, 0x0000),
                       (14, 0xF000, 0x2000), (15, 0xF000, 0x2000), (16, 0x0000, 0x0000)):
        r.param(kick, slot, 0, k)
        r.param(hat, slot, 0, h)
        r.param(hat2, slot, 0, h)
    m.settle(40, audio=1)
    snap = m.snapshot()
    chokes = lambda: struct.unpack('<I', bytes(uc.mem_read(0x2000E400 + 0xFE4, 4)))[0]

    def hit(first, second, gap_ms=1, hold_ms=25):
        m.restore(snap)
        level.clear(); events.clear()
        m.press(first); m.settle(25, audio=1); m.release(first); m.settle(gap_ms, audio=1)
        n = len(level)
        c0 = chokes()
        m.press(second); m.settle(hold_ms, audio=1)
        b = [e for e in events if e > n][0]
        return level[b:b + 700], chokes() - c0

    def onset(t):
        return next(i for i in range(1, len(t)) if t[i - 1] < 0.02 and t[i] > t[i - 1])

    k, nk = hit(34, 36)
    h, nh = hit(38, 36)
    check('after the kick (VCA %.2f) the switch fades; after a silent hat2 it does not'
          % k[0], (nk, nh), (1, 0))
    ok_, oh = onset(k), onset(h)
    fall = [k[i] - k[i + 1] for i in range(0, ok_ - 1)]
    check('the fade reaches 0 before the attack (min %.4f)' % min(k[:ok_]), min(k[:ok_]) < 1e-6, True)
    check('no downward step over 1.2/%d of the level (largest %.3f)' % (samples, max(fall)),
          max(fall) <= 1.2 * k[0] / samples, True)
    diff = max(abs(k[ok_ + i] - h[oh + i]) for i in range(500))
    check('the hat after the kick == the hat after silence, aligned (max diff %.4f)' % diff,
          diff < 0.002, True)
    check('the fade costs %d samples of onset (under %d)' % (ok_ - oh, samples + 24),
          0 <= ok_ - oh < samples + 24, True)

    k2, n2 = hit(34, 34, gap_ms=40)
    check('the same instrument again: no fade (continues from %.2f, stock)' % k2[0], n2, 0)

    # a note released inside its own fade: the held note-on must still be
    # applied, then released -- no gate left open
    m.restore(snap)
    m.press(34); m.settle(25, audio=1); m.release(34); m.settle(1, audio=1)
    m.press(36)
    pend_word = lambda: struct.unpack('<I', bytes(uc.mem_read(0x2000E400 + 0xFD4, 4)))[0]
    for _ in range(400):                       # until the note-on is held back
        m.step(slices=1, audio=1)
        if pend_word():
            break
    uc.mem_write(0x2000E400 + 0xFC8, struct.pack('<I', 4000))   # a fade long enough to release in
    m.settle(10, audio=1)
    held = pend_word() != 0
    m.release(36); m.settle(60, audio=1)
    stage = uc.mem_read(E + 0x31, 1)[0]
    pend = struct.unpack('<I', bytes(uc.mem_read(0x2000E400 + 0xFD4, 4)))[0]
    check('released mid-fade: it was held, then nothing left held and the amp '
          'envelope released (stage %d)' % stage, (held, pend, stage in (3, 4)), (True, 0, True))


if __name__ == '__main__':
    sys.exit(main())
