#!/usr/bin/env python3
"""The listening bench: the real firmware in sysemu, a digital voice after it.

sysemu runs the patched image; for every audio sample the DSP's published
control values (engine+0x2EB4..+0x2F2C, see render.c) are captured from the
note's start, written as float32 frames and rendered by build/bench/render
into a WAV.  Then a few numbers describe what came out: where the level
peaks and how it decays, how bright it is early and late, its pitch.

    bench.py kick snare fm-bass        # one draw each
    bench.py --count 3 bass            # three draws
    bench.py --all                     # every instrument once
    bench.py --random 8                # eight free draws (TEMPO+FUNC style)

WAVs land in build/bench/wav/ (peak-normalised previews; the voice is a
model, the parameters are the firmware's).
"""
import argparse
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
os.chdir(ROOT)
import sysemu                                   # noqa: E402
from unicorn import UC_HOOK_CODE                # noqa: E402
from unicorn.arm_const import UC_ARM_REG_S16    # noqa: E402
import test_modnote_random as T                 # noqa: E402

E = 0x20004834
NCV = 31
RENDER = ROOT / 'build/bench/render'
OUT = ROOT / 'build/bench/wav'


def ensure_renderer():
    source = ROOT / 'tools/bench/render.c'
    if not RENDER.exists() or source.stat().st_mtime_ns > RENDER.stat().st_mtime_ns:
        RENDER.parent.mkdir(parents=True, exist_ok=True)
        # Parallel surveys must never execute an unfinished linker output.
        with tempfile.TemporaryDirectory(dir=RENDER.parent) as tmp:
            target = Path(tmp) / 'render'
            subprocess.run(['cc', '-O3', '-Wall', '-Wextra', str(source), '-lm', '-o', str(target)], check=True)
            os.replace(target, RENDER)


class Bench:
    def __init__(self):
        ensure_renderer()
        self.r = r = T.Rig()
        self.uc = uc = r.uc
        self.frames = []
        self.cutoff_before_clip = []
        self.owner18 = r.u32(E + 0x2E68 + 4 * 18)
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: self.frames.append(bytes(u.mem_read(E + 0x2EB4, NCV * 4))),
                    begin=0x08115040, end=0x08115040)
        # The linear cutoff sum before its 0..149 clamp. Needed for paired
        # filter-only replay: a clipped CV alone cannot tell how far past
        # the limit the original sum was.
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: self.cutoff_before_clip.append(
            struct.unpack('<f', struct.pack('<I', u.reg_read(UC_ARM_REG_S16)))[0]),
                    begin=0x081131A2, end=0x081131A2)
        uc.ctl_flush_tb()

    def play(self, key, hold_ms, tail_ms):
        r, m = self.r, self.r.m
        self.frames.clear()
        self.cutoff_before_clip.clear()
        r.trace.clear()
        r.starts.clear()
        m.press(key)
        m.settle(hold_ms, audio=1)
        sync = struct.unpack('<f', bytes(self.uc.mem_read(self.owner18 + 0x1EC, 4)))[0]
        m.release(key)
        m.settle(tail_ms, audio=1)
        start = r.starts[0] if r.starts else 0
        # r.trace and self.frames are appended by the same per-sample hook
        frames = self.frames[start:]
        assert len(self.cutoff_before_clip) == len(self.frames), 'cutoff hook/sample alignment'
        self.cutoff_unclipped = np.asarray(self.cutoff_before_clip[start:], dtype='<f4')
        cv = np.frombuffer(b''.join(frames), dtype='<f4').reshape(-1, NCV)
        np.testing.assert_allclose(cv[:, 12], np.clip(self.cutoff_unclipped, 0, 149), atol=2e-5, rtol=0)
        return cv, sync >= 0.5

    def render(self, cv, sync, name, raw_path=None, directory=None):
        out_dir = OUT if directory is None else Path(directory)
        out_dir.mkdir(parents=True, exist_ok=True)
        f32 = out_dir / (name + '.f32')
        wav = out_dir / (name + '.wav')
        cv.astype('<f4').tofile(f32)
        command = [str(RENDER), str(f32), str(wav), '1' if sync else '0']
        if raw_path is not None:
            command.append(str(raw_path))
        out = subprocess.run(command,
                             check=True, capture_output=True, text=True).stdout
        f32.unlink()
        self.peak = float(out.split('peak')[1]) if 'peak' in out else 0.0
        return wav


def analyse(wav):
    raw = np.fromfile(wav, dtype='<i2', offset=44).astype(float) / 32768.0
    fs = 32000
    hop = 320                                  # 10 ms
    rms = np.array([np.sqrt(np.mean(raw[i:i + hop] ** 2)) for i in range(0, len(raw) - hop, hop)])
    db = 20 * np.log10(rms + 1e-9)
    pk = int(np.argmax(rms))
    below = np.nonzero(db[pk:] < db[pk] - 20)[0]
    decay = (below[0] * 10) if len(below) else None

    def centroid(a_ms, b_ms):
        seg = raw[int(a_ms * 32):int(b_ms * 32)]
        if len(seg) < 256 or np.max(np.abs(seg)) < 1e-4:
            return None
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        freqs = np.fft.rfftfreq(len(seg), 1 / fs)
        return float(np.sum(spec * freqs) / (np.sum(spec) + 1e-12))

    def pitch(a_ms, b_ms):
        """YIN: the first dip of the cumulative-mean-normalised difference"""
        seg = raw[int(a_ms * 32):int(b_ms * 32)]
        if len(seg) < 4096 or np.max(np.abs(seg)) < 1e-3:
            return None
        w = 2048
        hi = min(fs // 30, len(seg) - w - 1)
        x = seg[:w + hi]
        d = np.array([np.sum((x[:w] - x[t:t + w]) ** 2) for t in range(hi)])
        cmn = d[1:] * np.arange(1, hi) / (np.cumsum(d[1:]) + 1e-12)
        dips = np.nonzero(cmn < 0.15)[0]
        if not len(dips):
            return None
        t = dips[0]
        while t + 1 < len(cmn) and cmn[t + 1] < cmn[t]:
            t += 1
        return fs / (t + 1)
    return {'peak_ms': pk * 10, 'decay20_ms': decay, 'cent_early': centroid(0, 40),
            'cent_late': centroid(150, 300), 'f0': pitch(20, 200)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('names', nargs='*')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--count', type=int, default=1)
    ap.add_argument('--random', type=int, default=0)
    ap.add_argument('--hold', type=int, default=300)
    ap.add_argument('--tail', type=int, default=900)
    ap.add_argument('--tag', default='')
    ap.add_argument('--cv', action='store_true', help='print the control values early and late')
    ap.add_argument('--set', action='append', default=[],
                    help='a hand-made mod note instead of the randomizer: "slot.local=raw,..."')
    a = ap.parse_args()
    names = T.NAMES if a.all else a.names
    b = Bench()
    keys = (41, 43)
    jobs = [(n, i) for n in names for i in range(a.count)] + [(None, i) for i in range(a.random)]
    jobs += [('set:' + spec, i) for i, spec in enumerate(a.set)]
    for j, (name, i) in enumerate(jobs):
        key = keys[j % 2]
        note = b.r.index(key)
        if name and name.startswith('set:'):
            m = b.r.m
            m.submit_event(0x0816489C, struct.pack('<HB', 0, note))
            m.settle(10)
            for item in name[4:].split(','):
                sl, raw = item.split('=')
                slot, local = (int(x) for x in sl.split('.'))
                m.submit_event(0x08164274, struct.pack('<HBBBxH', 0, note, slot, local, int(raw, 16)))
                m.settle(4)
            what = 'set'
        else:
            force = T.NAMES.index(name) + 1 if name else 0
            b.r.randomize(note, force=force, settle=10)
            what = T.NAMES[b.r.diag(T.D_MNRAND_LAST) & 0xFF]
        cv, sync = b.play(key, a.hold, a.tail)
        label = '%s%s_%d' % (a.tag, what, i)
        wav = b.render(cv, sync, label)
        m = analyse(wav)
        fmt = lambda v, f='%.0f': '-' if v is None else f % v
        print('%-22s level %5.3f  peak %4s ms  -20dB %5s ms  centroid %5s -> %5s Hz  f0 %6s Hz  %s'
              % (label, b.peak, m['peak_ms'], fmt(m['decay20_ms']), fmt(m['cent_early']), fmt(m['cent_late']),
                 fmt(m['f0'], '%.1f'), 'sync' if sync else ''), flush=True)
        if a.cv:
            names = ['v1saw', 'v1sq', 'v1sin', 'v1tri', 'v2sq', 'v2saw', 'v2tri', 'v2sin', 'tzfm', 'v1lvl',
                     'p1', 'p2', 'cut', 'vol', 'reso', 'col']
            for lo, hi in ((0, 30), (200, 300)):
                seg = cv[lo * 32:hi * 32]
                if not len(seg):
                    continue
                print('    %3d-%3d ms ' % (lo, hi) + ' '.join(
                    '%s %.2f..%.2f' % (nm, seg[:, k].min(), seg[:, k].max())
                    for k, nm in enumerate(names) if seg[:, k].max() > 0.001) +
                    ' amp %.2f..%.2f' % (seg[:, 30].min(), seg[:, 30].max()))
            note = b.r.index(key)
            rec = sorted((s_, l, v) for n_, s_, l, v in b.r.records()[0] if n_ == note)
            print('    records: ' + ' '.join('%d.%d=%04x' % x for x in rec))


if __name__ == '__main__':
    main()
