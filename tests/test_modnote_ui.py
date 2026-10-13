#!/usr/bin/env python3
"""Mod-note mode on the whole-firmware emulator (sysemu): enter the mode,
play a mod note, leave the mode -- on the stock image and on the patched
one -- and compare what the UI does: mode handler calls, LED frames, main
loop liveness.  Usage: test_modnote_ui.py [image ...]"""
import sys
import time
import struct
import pickle
from pathlib import Path
from unicorn.arm_const import *
import sysemu

CACHE = Path('build/sysemu-boot-%s.pkl')


def booted(image):
    """Boot once per image and cache the snapshot on disk (keyed by the
    image's contents, so a rebuilt image never reuses a stale boot)."""
    import hashlib
    tag = Path(image).stem + '-' + hashlib.sha1(Path(image).read_bytes()).hexdigest()[:8]
    m = sysemu.Mono(image=image)
    cache = Path(str(CACHE) % tag)
    if cache.exists():
        m.restore(pickle.loads(cache.read_bytes()))
        m.factory_defaults()
        return m
    t = time.time()
    m.boot(limit=3_000_000_000)
    print('%s: booted in %.0fs (%d fp64 ops, %d init errors)' % (
        image, time.time() - t, m.shim.count, len(m.errors)), flush=True)
    m.settle(50)                       # let the main loop spin up
    cache.write_bytes(pickle.dumps(m.snapshot()))
    m.factory_defaults()
    return m


def probe_liveness(m, ms=100):
    uc = m.uc
    kicks = [0]
    h = uc.hook_add(sysemu.UC_HOOK_CODE, lambda uc, a, s, d: kicks.__setitem__(0, kicks[0] + 1),
                    begin=0x08119A02, end=0x08119A02)
    frames = len(m.led_bytes)
    m.settle(ms)
    uc.hook_del(h)
    return kicks[0], len(m.led_bytes) - frames


TOGGLE_MODNOTE = 0x0816FEA0   # ProjectEvent<ToggleModNoteModeData> vtable


def find_modnote_chord(m, snap, candidates=range(20, 40)):
    """Which two keys toggle mod-note mode: the chord that submits a
    ToggleModNoteModeData project event (or reaches its handler)."""
    import itertools
    seen = {}
    for a, b in itertools.combinations(candidates, 2):
        m.restore(snap)
        m.chord(a, b, hold_ms=30, gap_ms=20)
        kinds = sorted({hex(v) for v in m.events})
        if kinds: seen[(a, b)] = kinds
        if m.toggle_calls or TOGGLE_MODNOTE in m.events:
            print('  chord', (a, b), 'events', kinds, 'toggle handler calls', m.toggle_calls)
            return a, b
    print('  no chord toggled; events seen per pair:', seen)
    return None


def scenario(m, snap, up, down, note, label):
    m.restore(snap)
    m.chord(up, down)
    entered = m.toggle_calls
    k0, f0 = probe_liveness(m, 50)
    m.tap(note)                        # empty slot: copies the patch, plays it
    m.settle(150)
    k1, f1 = probe_liveness(m, 50)
    m.tap(note)                        # play it again
    m.settle(150)
    k2, f2 = probe_liveness(m, 50)
    m.chord(up, down)
    left = m.toggle_calls - entered
    k3, f3 = probe_liveness(m, 50)
    print('%s: toggle calls enter=%d leave=%d; liveness (loop iterations/LED frames per 50 ms):'
          ' before %d/%d, after note %d/%d, after 2nd note %d/%d, after leave %d/%d' % (
              label, entered, left, k0, f0, k1, f1, k2, f2, k3, f3))
    return entered, left, (k0, f0, k1, f1, k2, f2, k3, f3)


if __name__ == '__main__':
    images = sys.argv[1:] or ['build/firmware.bin', 'build/firmware-patched.bin']
    for image in images:
        m = booted(image)
        kicks, frames = probe_liveness(m)
        print('%s: idle 100 ms -> %d main-loop iterations, %d LED bytes' % (image, kicks, frames))
        snap = m.snapshot()
        # does a key press register?  TYPE (key 2) sets modifier bit 10 in UI+0x7C
        m.press(2); m.settle(30)
        print('  TYPE held: UI mask %#x' % struct.unpack('<I', m.uc.mem_read(sysemu.UI + 0x7C, 4))[0])
        m.release(2); m.settle(30)
        print('  released: UI mask %#x' % struct.unpack('<I', m.uc.mem_read(sysemu.UI + 0x7C, 4))[0])
        m.restore(snap)
        chord = find_modnote_chord(m, snap)
        print('  mod-note chord:', chord)

