#!/usr/bin/env python3
"""PL: a project loaded while the sequencer plays takes over at the end of
the playing pattern, from its first step.

np_load_request / np_load_done / np_proj_service (patch/np_runtime.cpp) and
np_save_gate (np_patch.S).  sysemu, with the asynchronous QSPI model.  Slot 0
holds a project with D on step 1; the live one has a note on every step (D
on 1, C on 5, E on 9, G on 13, B on 16, A elsewhere), and LOAD + SEQ 1
brings slot 0 back:

  1. mid-pattern: the old project plays every one of its remaining steps,
     evenly -- the slot's read (one polled 512 KB HAL receive, ~63 ms on the
     unit) starts just after a step's notes, not where it would hold up the
     next step -- and its last step's note too; the replay lands in the last
     step after that note, and the new project's step 1 falls on the old
     grid, then keeps it;
  2. stopped: the load is the stock's, at once;
  3. FUNC+SAVE while the load waits is ignored (nothing reaches the flash),
     and works again afterwards;
  4. a pattern change asked for while it waits is dropped: the new project
     starts on its own pattern;
  5. the new project's pattern is 8 steps, the old 16: its step 1 still on
     the boundary, then every 8 steps;
  6. asked in the last step: the read waits for the next pass, the old
     project plays it whole and the new one starts at the boundary after;
  7. a one-step pattern: its only step is also its last, and the read
     still starts in it (p60; before, the load waited for a stop).

The emulator's projects grow out of a two-event journal, and a save leaves
the patterns without a section of their own pointing at the journal's end,
which the load's replay rebuilds as 0: the stock's own check (0x0812414C,
code 0xA2) would refuse the slot.  At that check the image is given the
table the replay built -- what the stock writes for a slot it lays out
itself; a unit's projects come laid out from the factory.
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sysemu
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R1
from test_modnote_ui import booted

IMAGE = 'build/firmware-patched.bin'
STOCK = 'build/firmware.bin'
P = 0x200047F0
SEQ = 0x2000CFF4
MGR = 0x2000C68C
D_PLOAD = 0x2000E400 + 0x1BF8
STEP_MS = 125                         # 120 BPM, sixteenths
D, C, E, G, A, B = 62, 60, 64, 67, 69, 71
ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print('  %-72s ok' % label)
    else:
        fail += 1
        print('  %-72s FAIL got %r want %r' % (label, got, want))


def u32(uc, a):
    return struct.unpack('<I', bytes(uc.mem_read(a, 4)))[0]


class Rig:
    KEY = {C: 34, D: 36, E: 38, G: 41, A: 43, B: 45}

    def __init__(self, slot_steps=16):
        self.m = m = booted(IMAGE)
        self.uc = uc = m.uc
        m.factory_defaults()
        m.enable_qspi_async(delay_slices=4)
        self.log = []
        f = lambda a: struct.unpack('<f', bytes(uc.mem_read(a, 4)))[0]
        pos = lambda: u32(uc, SEQ + 0x48)
        for addr, kind, what in ((0x081173C0, 'note', lambda u: round(f(P + 0x5FCC))),
                                 (0x08124054, 'replay', lambda u: pos()),
                                 (0x08103E9C, 'read', lambda u: pos()),
                                 (0x0814FA54, 'fail', lambda u: u.reg_read(UC_ARM_REG_R1))):
            uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d, k=kind, w=what: self.log.append((k, self.t(), w(u))),
                        begin=addr, end=addr)
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: uc.mem_write(0x24000000, bytes(uc.mem_read(MGR + 0x228, 0x100))),
                    begin=0x0812414C, end=0x0812414C)
        request = u32(uc, 0x08175200 + 0x1D8) & ~1                  # np_load_request: the SEQ press
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: self.log.append(('request', self.t(), pos())),
                    begin=request, end=request)
        self.programs = 0
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: setattr(self, 'programs', self.programs + 1),
                    begin=0x081262FC, end=0x081262FC)
        uc.ctl_flush_tb()
        if slot_steps != 16:
            self.length(slot_steps)
        self.note(D, 1)                                    # slot 0: D on step 1
        self.save()
        if slot_steps != 16:
            self.length(16)
        for step in range(2, 17):                          # live: a note on every step
            self.note({5: C, 9: E, 13: G, 16: B}.get(step, A), step)
        self.log.clear()

    def t(self):
        return self.m.slices * 0.25

    def length(self, steps):
        self.m.submit_event(self.m.PATTERN_PARAM_VT, struct.pack('<HBBB', 0, 0, 0, steps))
        self.m.settle(20)

    def note(self, note, step):
        self.m.tap(self.KEY[note]); self.m.settle(15); self.m.tap(3 + step); self.m.settle(15)

    def save(self):
        m = self.m
        m.press(26); m.settle(20); m.tap(32, hold_ms=40, gap_ms=10); m.release(26); m.settle(20)
        for _ in range(80):
            m.settle(10)
            if m.qspi_queue_len() == 0:
                break
        m.settle(50)

    def load(self, slot_key=4):
        m = self.m
        m.press(26); m.settle(10); m.tap(33, hold_ms=30, gap_ms=10); m.release(26); m.settle(10)
        m.tap(slot_key, hold_ms=30, gap_ms=10)
        return self.t()

    def notes(self, after=0.0, before=1e12):
        return [(t, n) for k, t, n in self.log if k == 'note' and after <= t < before]

    def of(self, kind):
        return [(t, v) for k, t, v in self.log if k == kind]


def at_tick(r, passes, tick):
    """settle until track 0 has wrapped `passes` times and stands at `tick`"""
    m, uc = r.m, r.uc
    last, wraps = u32(uc, SEQ + 0x48), 0
    for _ in range(20000):                           # 20 s: the sequencer must be running
        m.settle(1)
        pos = u32(uc, SEQ + 0x48)
        if pos < last:
            wraps += 1
        last = pos
        if wraps >= passes and pos >= tick:
            return pos
    raise AssertionError('track 0 never reached pass %d tick %d (at %d): is the sequencer running?'
                         % (passes, tick, u32(uc, SEQ + 0x48)))


def grid(r):
    """step 1's time from the first D, and the pass length"""
    d = [t for t, n in r.notes() if n == D]
    return d[0], (d[1] - d[0]) if len(d) > 1 else 16 * STEP_MS


def gaps(times):
    return [round(b - a) for a, b in zip(times, times[1:])]


def mid_pattern():
    print('1. a load in the middle of the pattern')
    r = Rig()
    m = r.m
    m.tap(27); m.settle(20)
    at_tick(r, 1, 6 * 256 + 100)                              # inside step 7 of the second pass
    asked = r.load()
    m.settle(4500)
    m.tap(27); m.settle(20)
    t0, period = grid(r)
    boundary = t0 + 2 * period
    old = r.notes(asked - 200, boundary - 50)
    rep, read = r.of('replay'), r.of('read')
    new = r.notes(boundary - 50)
    check('the old project plays on to its last step: %s' % [n for _, n in old],
          [n for _, n in old][-10:], [A, A, E, A, A, A, G, A, A, B])
    check('evenly, through the request and the read: gaps %s ms' % gaps([t for t, _ in old]),
          all(115 <= g <= 135 for g in gaps([t for t, _ in old])), True)
    check('the read starts just after a step, not in the last (tick %s)' % [p for _, p in read],
          (len(read), bool(read) and 16 <= read[0][1] % 256 < 64 and read[0][1] < 3840), (1, True))
    check('one replay, in the last step after its note (tick %s)' % [p for _, p in rep],
          (len(rep), bool(rep) and 4096 - 224 <= rep[0][1] < 4096), (1, True))
    check('step 1 of the new project on the grid: %.0f ms (%.0f expected)' % (new[0][0], boundary),
          abs(new[0][0] - boundary) <= 15, True)
    check('then only the new project, every pass: %s' % [n for _, n in new], set(n for _, n in new), {D})
    d = [t for t, n in new]
    check('second D one pass later (%.0f ms)' % (d[1] - d[0]), abs(d[1] - d[0] - period) <= 15, True)
    check('D_PLOAD %s, no Error_Handler, no load failure' % bytes(m.uc.mem_read(D_PLOAD, 4)).hex(),
          (bytes(m.uc.mem_read(D_PLOAD, 4)), m.errors, r.of('fail')), (b'\x01\x01\x01\x00', [], []))


def stopped():
    print('2. stopped: the stock load')
    r = Rig()
    m = r.m
    asked = r.load()
    m.settle(400)
    rep = r.of('replay')
    check('replayed %.0f ms after the request, no synced request counted' % (rep[0][0] - asked if rep else -1),
          (len(rep), bool(rep) and rep[0][0] - asked < 300, bytes(m.uc.mem_read(D_PLOAD, 4))),
          (1, True, b'\0\0\0\0'))
    m.tap(27); m.settle(20); m.settle(2100); m.tap(27); m.settle(20)
    check('it plays the loaded project: %s' % [n for _, n in r.notes()], set(n for _, n in r.notes()), {D})


def save_and_pattern():
    print('3, 4. FUNC+SAVE and a pattern change while the load waits')
    r = Rig()
    m = r.m
    ui = lambda o: m.uc.mem_read(sysemu.UI + o, 1)[0]
    m.tap(27); m.settle(20)
    at_tick(r, 1, 2 * 256 + 100)                              # inside step 3 of the second pass
    r.load()
    m.settle(300)
    saves = r.programs
    m.press(26); m.settle(20); m.tap(32, hold_ms=40, gap_ms=10); m.release(26); m.settle(20)
    blocked = r.programs == saves and m.qspi_queue_len() == 0
    m.tap(24); m.settle(30); m.tap(5); m.settle(30); m.tap(24); m.settle(30)   # PATTERN, SEQ 5, PATTERN
    m.settle(3500)
    t0, period = grid(r)
    new = [n for _, n in r.notes(t0 + 2 * period - 50)]
    check('the save is ignored while the load waits', blocked, True)
    check('the new project starts on its own pattern (UI+0xEF %d): %s' % (ui(0xEF), new[:3]),
          (ui(0xEF), set(new)), (0, {D}))
    saves = r.programs
    r.save()
    check('a save afterwards goes through', r.programs, saves + 1)
    m.tap(27); m.settle(20)


def other_length():
    print('5. the new project\'s pattern is 8 steps')
    r = Rig(slot_steps=8)
    m = r.m
    m.tap(27); m.settle(20)
    at_tick(r, 1, 6 * 256 + 100)
    r.load()
    m.settle(3500)
    m.tap(27); m.settle(20)
    t0, period = grid(r)
    boundary = t0 + 2 * period
    d = [t for t, n in r.notes(boundary - 50) if n == D]
    check('length %d; D at %s ms (boundary %.0f)' % (struct.unpack('<H', bytes(m.uc.mem_read(SEQ + 0x60, 2)))[0] // 256,
                                                    [round(x) for x in d[:3]], boundary),
          (abs(d[0] - boundary) <= 15, abs(d[1] - d[0] - period / 2) <= 15), (True, True))


def late():
    print('6. asked in the last step: the next boundary')
    r = Rig()
    m = r.m
    m.tap(27); m.settle(20)
    at_tick(r, 1, 3780)                                       # the gesture takes ~60 ms to its SEQ press
    r.load()
    m.settle(4500)
    m.tap(27); m.settle(20)
    t0, period = grid(r)
    read, rep, req = r.of('read'), r.of('replay'), r.of('request')
    asked = req[0][0]
    check('asked in the last step, after its note (tick %d)' % req[0][1], 3840 + 16 <= req[0][1] < 4096, True)
    one_more = [n for _, n in r.notes(asked, t0 + 3 * period - 50)]
    first_new = [t for t, n in r.notes(t0 + 3 * period - 50)]
    check('the read waits for the next pass (at %s)' % [round(t - (t0 + 2 * period)) for t, _ in read],
          bool(read) and read[0][0] > t0 + 2 * period, True)
    check('the old project plays that pass whole: %d notes, last %s' % (len(one_more), one_more[-1:]),
          (len(one_more), one_more[-1:]), (16, [B]))
    check('then the new one, on the grid', bool(first_new) and abs(first_new[0] - (t0 + 3 * period)) <= 15, True)
    check('one replay', len(rep), 1)


def one_step():
    print('7. the playing pattern is one step long')
    r = Rig()
    m = r.m
    r.length(1)
    m.tap(27); m.settle(20)
    m.settle(600)                                             # a few passes of the one step
    r.load()
    m.settle(1500)
    rep, read = r.of('replay'), r.of('read')
    length = struct.unpack('<H', bytes(m.uc.mem_read(SEQ + 0x60, 2)))[0]
    m.tap(27); m.settle(20)
    check('the read starts while playing (ticks %s)' % [p for _, p in read], len(read), 1)
    check('one replay, before the step ends (tick %s)' % [p for _, p in rep],
          (len(rep), bool(rep) and 32 <= rep[0][1] < 256), (1, True))
    check('the loaded project (16 steps) plays: length %d ticks' % length, length, 4096)
    check('D_PLOAD %s, no Error_Handler, no load failure' % bytes(m.uc.mem_read(D_PLOAD, 4)).hex(),
          (bytes(m.uc.mem_read(D_PLOAD, 4)), m.errors, r.of('fail')), (b'\x01\x01\x01\x00', [], []))


def main():
    mid_pattern()
    stopped()
    save_and_pattern()
    other_length()
    late()
    one_step()
    print('%s -- a project loaded while playing takes over at the pattern\'s end (%d checks)'
          % ('PASS' if fail == 0 else 'FAIL', ok + fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
