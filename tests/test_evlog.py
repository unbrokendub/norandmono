#!/usr/bin/env python3
"""EV/PM: the X-Env event log, end to end on sysemu.

One note on step 1, the cutoff selected with an X-Env amount, and an
unlinked X-Env trig for the cutoff on step 5 (TYPE+WAVE held, SEQ 5).  The
pattern plays twice.  Every channel-16 CC the firmware writes into its MIDI
output ring is captured and decoded with tools/diagmidi.py, exactly as a
capture from the unit would be.

What must come out:
  * EV records with a valid CRC;
  * on each note a TRIG-from-NOTE event naming the X-Envs the note started --
    not the cutoff, whose unlinked trig takes it off the notes;
  * on step 5 a TRIG-from-PARAM event naming the cutoff alone;
  * RC attack events for the main ADSR on the note and for the cutoff on
    its trig, and no RC decay-restart anywhere (nothing retriggers in decay);
  * a PM record showing the note on step 1 and the param trig on step 5,
    slot 9;
  * the ring itself agreeing with what was decoded.
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import diagmidi
import sysemu
from test_telemetry import RingTap
from test_modnote_ui import booted

IMAGE = 'build/firmware-patched.bin'
D = 0x2000E400
ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print('  %-64s ok' % label)
    else:
        fail += 1
        print('  %-64s FAIL got %r want %r' % (label, got, want))


def main():
    img = Path(IMAGE).read_bytes()
    if img[0x56074:0x5607A] == bytes.fromhex('012380f82f30'):
        print('SKIP -- this image was built without --ev-log')
        return 0
    m = booted(IMAGE)
    uc = m.uc
    u32 = lambda a: struct.unpack('<I', bytes(uc.mem_read(a, 4)))[0]
    tap = RingTap(m, m.call_preserving(0x0811A6A4))

    m.tap(4, hold_ms=20, gap_ms=40)                     # the note, step 1
    m.pot_values[9] = 0x6000; m.settle(150, audio=1)    # select the cutoff
    m.pot_values[21] = 0x1000; m.settle(150, audio=1)   # its X-Env amount
    m.press(2); m.settle(30); m.press(3); m.settle(60)  # TYPE + WAVE held
    m.tap(8, hold_ms=30, gap_ms=40)                     # unlinked trig, step 5
    m.release(3); m.settle(30); m.release(2); m.settle(60)
    m.settle(5200, audio=1)                             # a PM record goes out
    m.tap(27, hold_ms=20, gap_ms=10)                    # PLAY
    m.settle(4300, audio=1)                             # two loops of 16 steps
    m.tap(27, hold_ms=20, gap_ms=10)
    m.settle(400, audio=1)                              # let the log drain

    recs = diagmidi.records(tap.ccs)
    evs = [w for t, w in recs if t == diagmidi.EV]
    pms = [w for t, w in recs if t == diagmidi.PM]
    check('EV records sent, every one with a valid CRC',
          bool(evs) and all(diagmidi.crc_valid(w, 52, 0x4E504556) for w in evs), True)
    check('PM records sent, every one with a valid CRC',
          bool(pms) and all(diagmidi.crc_valid(w, 52, 0x4E50504D) for w in pms), True)

    events = []
    for w in evs:
        for k in range(w[2] & 0xFFFF):
            ms, pos, x = w[4 + 3 * k: 7 + 3 * k]
            events.append((ms, pos, x >> 24, x & 0xFFFFFF))
    played = [e for e in events if e[0] >= 0]            # all of them, in order
    by_type = lambda t: [e for e in played if e[2] == t]
    notes, params = by_type(1), by_type(2)
    rc_attack, rc_decay = by_type(4), by_type(5)
    check('no event lost', all((w[2] >> 16) == 0 for w in evs), True)
    check('the log was drained: everything written was sent',
          u32(D + 0x1404), u32(D + 0x1400))

    loop_notes = notes
    loops = sorted({e[1] // 4096 for e in loop_notes})
    check('a TRIG-from-NOTE in every loop played (>= 2)', len(loops) >= 2 and loops == list(range(len(loops))), True)
    masks = {}
    for e in loop_notes:                 # a millisecond boundary can split one note's burst
        masks[e[1] // 4096] = masks.get(e[1] // 4096, 0) | e[3]
    check('...naming the note-linked X-Envs but not the cutoff',
          all(m_ & (1 << 9) == 0 and bin(m_).count('1') >= 10 for m_ in masks.values()), True)
    check('...landing on step 1 of the pattern',
          all((e[1] // 256) % 16 == 0 for e in loop_notes), True)
    check('a TRIG-from-PARAM per loop, the cutoff alone',
          [(e[3], (e[1] // 256) % 16) for e in params][:2], [(1 << 9, 4), (1 << 9, 4)])
    cut_rc = [e for e in rc_attack if e[3] & (1 << 10)]
    check('the cutoff RC envelope starts on its trig, step 5',
          sorted({(e[1] // 256) % 16 for e in cut_rc}), [4])
    main_rc = [e for e in rc_attack if e[3] & 1]
    check('the main ADSR RC envelope starts on the note, step 1',
          sorted({(e[1] // 256) % 16 for e in main_rc}), [0])
    check('no RC decay restart anywhere', rc_decay, [])

    pm = pms[-1]
    f0 = lambda step: (pm[5 + step // 4] >> (8 * (step & 3))) & 0xFF
    check('PM: a note on step 1, a param trig on step 5', (f0(0) & 1, f0(4) & 4), (1, 4))
    check('PM: steps 2..4 and 6..16 empty', [f0(s) for s in list(range(1, 4)) + list(range(5, 16))],
          [0] * 14)
    trigs = [w for w in pm[37:51] if w]
    check('PM: the param trig names slot 9 (cutoff) on step 5',
          [((w >> 24) & 0x7F, (w & 0xFFFFFF) >> 16, w >> 31) for w in trigs], [(4, 9, 0)])
    check('PM: pattern length 16', pm[3] & 0xFFFF, 16)

    print('\n--- the timeline, as diagmidi prints it ---')
    for t, w in recs:
        if t == diagmidi.PM and w is pm:
            diagmidi.show_pm(w)
    for w in evs:
        diagmidi.show_ev(w)
    print('%s -- X-Env event log (%d checks)' % ('PASS' if fail == 0 else 'FAIL', ok + fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
