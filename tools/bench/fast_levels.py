#!/usr/bin/env python3
"""All-recipe level survey using real DSP IRQ replay, stopped transport.

UI/generator/note-on/off run in the full M7 emulator. Between gate events
only the audio IRQ runs, on a native-FP64 emulator clone. This deliberately
does not model foreground RNG reseeding or running sequencer timing. Use
levels.py for full-system confirmation, not host elapsed time as MCU cycles.
"""
import argparse
import copy
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
from pathlib import Path
import struct
import time

import numpy as np
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_LR, UC_ARM_REG_PC

from bench import Bench, T, ensure_renderer
from fast_capture import AudioReplay, build_trace
from levels import metrics, symbol


def batch(job):
    image, elf, out, names, draws, base_seed, hold, tail, existing = job
    out = Path(out)
    T.IMAGE = image
    def fresh(image):
        m = T.sysemu.Mono(image=image)
        m.boot(limit=3_000_000_000); m.settle(50); m.factory_defaults()
        return m
    T.booted = fresh
    b = Bench(); m = b.r.m
    a = AudioReplay(m)
    capture, calls = [], []
    offs = []
    b.uc.hook_add(UC_HOOK_CODE, lambda *args: offs.append(1), begin=0x08117594, end=0x08117594)
    def gate(key, on):
        events = b.r.starts if on else offs
        events.clear()
        (m.press if on else m.release)(key)
        for _ in range(100):
            m.settle(2, main_instr=2000)
            if events:
                break
        assert len(events) == 1, ('missing/duplicate note event', key, on, len(events))
        m.run_unmasked()
    current_seed = 0
    def seed_hook(u, *_):
        calls.append(current_seed)
        u.reg_write(UC_ARM_REG_R0, current_seed)
        u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR))
    def level_hook(u, *_):
        capture.append(struct.unpack('<171H', u.mem_read(u.reg_read(UC_ARM_REG_R0), 342)))
    b.uc.hook_add(UC_HOOK_CODE, seed_hook, begin=symbol(elf, '::seeded()'), end=symbol(elf, '::seeded()'))
    b.uc.hook_add(UC_HOOK_CODE, level_hook, begin=symbol(elf, '::leveled('), end=symbol(elf, '::leveled('))
    b.uc.ctl_flush_tb()
    # Every draw starts without held notes/legato/choke/debounce state from
    # the preceding sound. Include the peripheral backing and host clocks;
    # Mono.snapshot alone intentionally does not carry them.
    snap = m.snapshot()
    extra = [(p, bytes(b.uc.mem_read(p,n))) for p,n in
             ((0,0x10000),(0x40000000,0x30000),(0x48000000,0x30000),
              (0x58000000,0x30000),(0xE0000000,0x100000))]
    host = {k: copy.deepcopy(getattr(m,k)) for k in
            ('tim5_acc','slices','instructions','usart_acc','cyccnt_reads','dma1_s0_tc','reads')}
    rng_state = m.rng.getstate()
    sha = hashlib.sha256(Path(image).read_bytes()).hexdigest()
    esha = hashlib.sha256(Path(elf).read_bytes()).hexdigest()
    from bench import RENDER
    renderer_sha = hashlib.sha256(RENDER.read_bytes()).hexdigest()
    rows = []
    for name in names:
        what = T.NAMES.index(name)
        for draw in range(draws):
            m.restore(snap)
            for p,data in extra: b.uc.mem_write(p,data)
            for k,value in host.items(): setattr(m,k,copy.deepcopy(value))
            m.rng.setstate(rng_state)
            current_seed = (base_seed + 0x9E3779B9*what + 0x85EBCA6B*draw) & 0xffffffff or 1
            label = '%s-%08x' % (name, current_seed)
            if label in existing:
                row=existing[label]
                assert row['image_sha256']==sha and row['elf_sha256']==esha
                assert row['hold_ms']==hold and row['tail_ms']==tail
                if row.get('renderer_sha256') != renderer_sha:
                    data=np.load(out/(label+'.cv.npz'))
                    raw=out/(label+'.audio.f32')
                    wav=b.render(data['cv'],bool(data['sync']),label,raw_path=raw,directory=out)
                    row.update(metrics(np.fromfile(raw,dtype='<f4')),renderer_sha256=renderer_sha)
                    raw.unlink(); wav.unlink()
                rows.append(row)
                (out/(names[0]+'.json')).write_text(json.dumps(rows)+'\n')
                continue
            key = 41
            note = b.r.index(key)
            capture.clear(); calls.clear()
            b.r.randomize(note, force=what+1)
            assert len(capture) == 1 and calls == [current_seed]
            assert b.r.diag(T.D_MNRAND_LAST) & 255 == what
            records = [list(r[1:]) for r in b.r.records()[0] if r[0] == note]
            emitted = np.array([[T.reset_value(s, l) for l in range(9)] for s in range(19)])
            for s, l, value in records:
                emitted[s, l] = value
            mask = np.ones((19, 9), dtype=bool); mask[17, [0, 1, 4]] = False
            assert np.array_equal(emitted[mask], np.array(capture[0]).reshape(19, 9)[mask])
            gate(key, True)
            sync = struct.unpack('<f', b.uc.mem_read(b.owner18+0x1EC, 4))[0] >= .5
            a.load(m); first = a.render(hold); a.store(m)
            gate(key, False)
            a.load(m); last = a.render(tail); a.store(m)
            frames = np.concatenate((first, last))
            cv = frames[:, :31]
            np.savez_compressed(out/(label+'.cv.npz'), cv=cv, sync=sync, cutoff_unclipped=frames[:,31])
            raw = out/(label+'.audio.f32')
            wav = b.render(cv, sync, label, raw_path=raw, directory=out)
            row = dict(name=name, seed=current_seed, draw=draw, pre_level=list(capture[0]),
                       records=records, hold_ms=hold, tail_ms=tail,
                       image_sha256=sha, elf_sha256=esha, replay='audio IRQ only between gate events',
                       renderer_sha256=renderer_sha,
                       **metrics(np.fromfile(raw, dtype='<f4')))
            rows.append(row)
            raw.unlink(); wav.unlink()
            (out/(names[0]+'.json')).write_text(json.dumps(rows)+'\n')
        print('%-12s %d draws, %.1f .. %.1f dB' %
              (name, draws, min(r['level_db'] for r in rows[-draws:]), max(r['level_db'] for r in rows[-draws:])), flush=True)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('names', nargs='*')
    ap.add_argument('--draws', type=int, default=24)
    ap.add_argument('--seed', type=int, default=73221126)
    ap.add_argument('--hold', type=int, default=300)
    ap.add_argument('--tail', type=int, default=500)
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--image', default='build/loudness-p72-candidate/firmware-patched.bin')
    ap.add_argument('--elf', default='build/loudness-p72-candidate/np_patch.elf')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--resume', action='store_true')
    args = ap.parse_args()
    if args.draws<1 or args.workers<1 or min(args.hold,args.tail)<=0 or args.hold+args.tail<400:
        ap.error('positive draws/workers/gate/tail and at least 400 ms total are required')
    args.out.mkdir(parents=True, exist_ok=args.resume)
    names = args.names or T.NAMES
    if not all(n in T.NAMES for n in names):
        ap.error('unknown instrument name')
    build_trace(); ensure_renderer()
    existing={}
    if args.resume:
        files=[args.out/'survey.json'] if (args.out/'survey.json').exists() else list(args.out.glob('*.json'))
        for path in files:
            for row in json.loads(path.read_text()):
                existing['%s-%08x'%(row['name'],row['seed'])]=row
    jobs = [(args.image, args.elf, str(args.out), names[i::args.workers], args.draws,
             args.seed, args.hold, args.tail, existing) for i in range(min(args.workers,len(names)))]
    t = time.monotonic()
    with ProcessPoolExecutor(args.workers) as pool:
        rows = sum(pool.map(batch, jobs), [])
    rows.sort(key=lambda r:(T.NAMES.index(r['name']),r['draw']))
    (args.out/'survey.json').write_text(json.dumps(rows, indent=2)+'\n')
    print('%d draws in %.1f seconds' % (len(rows), time.monotonic()-t))


if __name__ == '__main__':
    main()
