#!/usr/bin/env python3
"""Reproducible level survey, through the real generator, UI and audio IRQ.

    build/venv/bin/python tools/bench/levels.py kick clap pad --draws 4

Keeps seeds, parameter records, firmware hash, CVs and UNNORMALISED audio.
The metric is the mean of maximum K-weighted 100/400 ms window levels;
it is a comparison metric for one-shots, NOT integrated LUFS / true peak.
The analog voice is still an uncalibrated model: never treat these results
as measurements of the hardware or fit coefficients without holdout seeds.
"""
import argparse
import hashlib
import json
import math
import struct
import subprocess
from pathlib import Path

import numpy as np
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_LR, UC_ARM_REG_PC

from bench import Bench, T, ROOT

FS = 32000
# ITU-R BS.1770, Annex 1 tables 1 and 2 (48 kHz).
# https://www.itu.int/rec/R-REC-BS.1770
K48 = (((1.53512485958697, -2.69169618940638, 1.19839281085285),
        (1., -1.69065929318241, .73248077421585)),
       ((1., -2., 1.), (1., -1.99004745483398, .99007225036621)))


def retime(b, a, fs):
    """48 kHz -> analog bilinear prototype -> fs; not 48k taps at 32k."""
    r = fs / 48000.
    p, q = np.array([1-r, 1+r]), np.array([1+r, 1-r])
    basis = np.array([np.convolve(q, q), np.convolve(p, q), np.convolve(p, p)])
    bb, aa = np.asarray(b) @ basis, np.asarray(a) @ basis
    return bb / aa[0], aa / aa[0]


def k_weight(x, fs=FS):
    y = np.asarray(x, dtype=float).copy()
    for b, a in K48:
        b, a = retime(b, a, fs)
        z1 = z2 = 0.
        for i, v in enumerate(y):
            out = b[0]*v + z1
            z1 = b[1]*v - a[1]*out + z2
            z2 = b[2]*v - a[2]*out
            y[i] = out
    return y


def metrics(audio, fs=FS):
    x = np.asarray(audio, dtype=float)
    if not len(x) or not np.isfinite(x).all():
        raise ValueError('empty or non-finite audio')
    y = k_weight(x, fs)
    energy = np.r_[0., np.cumsum(y*y)]
    levels = []
    for ms in (100, 400):
        n = round(fs * ms / 1000)
        if len(x) < n:
            raise ValueError('record at least 400 ms')
        power = np.max(energy[n:] - energy[:-n]) / n
        levels.append(10 * math.log10(max(power, 1e-20)))
    return dict(k100_db=levels[0], k400_db=levels[1],
                level_db=sum(levels)/2,
                peak=float(np.max(np.abs(x))), rms=float(np.sqrt(np.mean(x*x))))


def symbol(elf, needle):
    lines = subprocess.check_output(['arm-none-eabi-nm', '-C', str(elf)], text=True).splitlines()
    matches = [int(l.split()[0], 16) for l in lines if needle in l and ' t ' in l]
    if len(matches) != 1:
        raise ValueError('expected one symbol: ' + needle)
    return matches[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('names', nargs='*', choices=T.NAMES)
    ap.add_argument('--draws', type=int, default=1)
    ap.add_argument('--seed', type=int, default=2201126)
    ap.add_argument('--hold', type=int, default=300)
    ap.add_argument('--tail', type=int, default=900)
    ap.add_argument('--image', type=Path, default=Path(T.IMAGE))
    ap.add_argument('--elf', type=Path, default=Path('build/np_patch.elf'))
    ap.add_argument('--out', type=Path, default=Path('build/bench/levels'))
    args = ap.parse_args()
    if args.draws < 1 or args.hold + args.tail < 450:
        ap.error('positive draw count and at least 450 ms hold + tail required')
    args.out.mkdir(parents=True, exist_ok=True)
    if (args.out / 'survey.json').exists():
        ap.error('survey.json already exists; choose a new --out to preserve the previous survey')
    T.IMAGE = str(args.image)
    # Do not use a boot cache: peripheral state is not included in it.
    def fresh(image):
        m = T.sysemu.Mono(image=image)
        m.boot(limit=3_000_000_000)
        m.settle(50)
        m.factory_defaults()
        return m
    T.booted = fresh
    b = Bench()
    seed_addr = symbol(args.elf, '::seeded()')
    level_addr = symbol(args.elf, '::leveled(')
    image = args.image.read_bytes()
    # Range check, then validate the hooks against the live emitted records
    # below. An address in range alone does not establish that an ELF matches.
    for addr in (seed_addr, level_addr):
        assert 0x08100000 <= addr < 0x08100000 + len(image)
    captured = []
    seed_calls = []
    def seed_hook(u, a, s, d):
        seed_calls.append(current_seed)
        u.reg_write(UC_ARM_REG_R0, current_seed)
        u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR))
    def level_hook(u, a, s, d):
        captured.append(struct.unpack('<171H', u.mem_read(u.reg_read(UC_ARM_REG_R0), 342)))
    b.uc.hook_add(UC_HOOK_CODE, seed_hook, begin=seed_addr, end=seed_addr)
    b.uc.hook_add(UC_HOOK_CODE, level_hook, begin=level_addr, end=level_addr)
    b.uc.ctl_flush_tb()
    sha = hashlib.sha256(image).hexdigest()
    elf_sha = hashlib.sha256(args.elf.read_bytes()).hexdigest()
    rows = []
    for name in args.names or T.NAMES:
        what = T.NAMES.index(name)
        for draw in range(args.draws):
            current_seed = (args.seed + 0x9E3779B9*what + 0x85EBCA6B*draw) & 0xffffffff or 1
            captured.clear()
            seed_calls.clear()
            key = 41 if len(rows) % 2 == 0 else 43
            note = b.r.index(key)
            b.r.randomize(note, force=what+1)
            assert len(captured) == 1, 'live generator did not call leveled exactly once'
            assert seed_calls == [current_seed], 'seed hook was not called exactly once'
            assert b.r.diag(T.D_MNRAND_LAST) & 0xff == what, 'wrong instrument generated'
            records = [list(r[1:]) for r in b.r.records()[0] if r[0] == note]
            emitted = np.array([[T.reset_value(s, l) for l in range(9)] for s in range(19)])
            for s, l, value in records:
                emitted[s, l] = value
            before = np.array(captured[0]).reshape(19, 9)
            unchanged = np.ones((19, 9), dtype=bool)
            unchanged[17, [0, 1, 4]] = False
            assert np.array_equal(emitted[unchanged], before[unchanged]), 'capture does not match live records'
            cv, sync = b.play(key, args.hold, args.tail)
            assert len(b.r.starts)==1, 'missing/duplicate note start in full-system capture'
            label = '%s-%08x' % (name, current_seed)
            raw = args.out / (label + '.audio.f32')
            b.render(cv, sync, 'survey-' + label, raw_path=raw, directory=args.out)
            np.savez_compressed(args.out / (label + '.cv.npz'), cv=cv, sync=sync,
                                cutoff_unclipped=b.cutoff_unclipped)
            row = dict(name=name, seed=current_seed, image_sha256=sha, elf_sha256=elf_sha,
                       hold_ms=args.hold, tail_ms=args.tail,
                       pre_level=list(captured[0]), records=records,
                       **metrics(np.fromfile(raw, dtype='<f4')))
            rows.append(row)
            (args.out / 'survey.json').write_text(json.dumps(rows, indent=2) + '\n')
            print('%-12s seed %08x level %6.2f dB peak %.5f' %
                  (name, current_seed, row['level_db'], row['peak']), flush=True)


if __name__ == '__main__':
    main()
