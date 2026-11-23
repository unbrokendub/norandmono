#!/usr/bin/env python3
"""Execute all recipes in ARM; verify packing, filter bounds and LV3 gains.

This complements live UI/kit/renderer tests: it is not an audio or IRQ test.
The complete 171 raw parameters and final RNG state are compared, including
negative intervals, scale fallback, tempo fitting and kit seasoning.
"""
import argparse
import json
from pathlib import Path
import struct
import subprocess
import time

from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_PC
import test_patch as T


def symbol(elf, text):
    lines = subprocess.check_output(['arm-none-eabi-nm', '-C', str(elf)], text=True).splitlines()
    matches = [int(l.split()[0], 16) for l in lines if text in l and ' t ' in l]
    assert len(matches) == 1, (text, matches)
    return matches[0]


class Generator:
    PATCH, GEN, CTX, KIT, TRANSPORT = 0x20018000, 0x20018400, 0x20018410, 0x20018440, 0x2001B000

    def __init__(self, directory):
        directory = Path(directory)
        T.IMAGE = str(directory / 'firmware-patched.bin')
        self.m = T.Machine()
        self.uc = self.m.uc
        self.instrument = symbol(directory / 'np_patch.elf', '::instrument(')
        T.stub_return_const(self.m, 0x08118380, self.TRANSPORT)
        self.pre_level = None
        addr=symbol(directory / 'np_patch.elf','::leveled(')
        def capture(u,*_):
            self.pre_level=struct.unpack('<171H',u.mem_read(u.reg_read(UC_ARM_REG_R0),342))
        self.uc.hook_add(UC_HOOK_CODE,capture,begin=addr,end=addr)

    def generate(self, what, seed, note=60, scale=0, root=0, bpm=120, kit=None, degree=0):
        u = self.uc
        u.mem_write(self.PATCH, bytes(352))
        u.mem_write(self.GEN, struct.pack('<I', seed or 1))
        u.mem_write(self.CTX, struct.pack('<HBBiI', scale, root, degree, note, self.KIT if kit else 0))
        if kit:
            u.mem_write(self.KIT, struct.pack('<8i', *kit))
        u.mem_write(self.TRANSPORT+4, struct.pack('<f', 2048*bpm/120))
        u.reg_write(UC_ARM_REG_R2, self.CTX)
        u.reg_write(UC_ARM_REG_R3, what)
        sp = u.reg_read(T.UC_ARM_REG_SP)
        self.m.call(self.instrument, self.PATCH, self.GEN, count=200000)
        assert u.reg_read(UC_ARM_REG_PC) == T.STOP
        assert u.reg_read(T.UC_ARM_REG_SP) == sp
        return struct.unpack('<171H', u.mem_read(self.PATCH, 342)), self.m.u32(self.GEN)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--before', default='build/loudness-p71-baseline')
    ap.add_argument('--after', default='build')
    ap.add_argument('--packing-only', action='store_true')
    ap.add_argument('--live', action='store_true', help='also exercise mod-note, normal-patch and drum-kit UI paths')
    args = ap.parse_args()
    old, new = Generator(args.before), Generator(args.after)
    model=json.loads(Path('docs/loudness-model.json').read_text())
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/bench'))
    from fit_levels import features
    scenarios = [dict(), dict(note=36, scale=0xAB5, root=0, bpm=90),
                 dict(note=72, scale=0x5AD, root=9, bpm=180),
                 dict(note=48, scale=0xAB5, bpm=120, degree=1,
                      kit=(12, -8, -12, 20, 7, 32767, 30000, 36)),
                 dict(note=84, scale=0x5AD, root=2, bpm=240,
                      kit=(-12, 8, 24, 30, 13, 45000, 60000, 40))]
    changes, calls = {}, 0
    t = time.monotonic()
    for scene in scenarios:
        for what in range(54):
            for draw in range(50):
                seed = ((draw+1)*0x9E3779B9 + what*19031) & 0xffffffff
                a, arng = old.generate(what, seed, **scene)
                b, brng = new.generate(what, seed, **scene)
                assert arng == brng, (what, seed, 'RNG consumption changed')
                delta = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
                if args.packing_only:
                    assert not delta, (what, seed, scene, delta)
                else:
                    assert all(i in (9*9,17*9,17*9+1,17*9+4) for i in delta), (what, seed, delta)
                    assert 9*9 not in delta or what in (3,4,52), (what,seed,'unrelated filter changed')
                    if what in (3, 4):
                        assert b[9*9] <= (186-scene.get('note', 60))*256
                        assert b[9*9] <= a[9*9]
                    if what == 52:
                        assert b[9*9] >= a[9*9]
                        depth = lambda raw: 149*((raw-32768)/32768)**3
                        xm, xe = depth(b[9*9+1]), depth(b[9*9+4])
                        low = min(-abs(xm), -abs(xm+xe)) if b[9*9+8] else -abs(xm)+min(xe, 0)
                        low += min(depth(b[12*9]), 0)
                        assert b[9*9]/256 + low >= b[0]/256 + 36 - 1/256, (seed, scene, 'sub filter floor')
                    # Check the executed ARM leveler against the fitted
                    # predictor, independently of its polynomial exp2.
                    before=new.pre_level
                    assert before is not None
                    f=features(before)
                    # Training is C4. For other generation notes, use the
                    # same absolute-pitch correction as the hardware model.
                    sounds=lambda slot: before[slot*9]>=0x1800 or before[slot*9+4]>32768 or before[slot*9+1]!=32768
                    pitch=min([before[s*9]/256 for s,vol in ((0,3),(4,7)) if sounds(vol)] or [before[0]/256])
                    f['low_pitch']=min((pitch+scene.get('note',60)-60-57)/24,0)
                    pred=model['intercept_encoding']['bytes'][what]/8-64
                    pred+=sum(w*f[n] for n,w in zip(model['features'],model['weights']))
                    wanted=min(1,10**(max(-48,model['target']-pred)/20))*65535
                    base=before[17*9]
                    assert base>0
                    for loc in (1,4):
                        d=before[17*9+loc]-32768
                        if d:
                            wanted=min(wanted,base*((32768 if d<0 else 32767)/abs(d))**3)
                    assert abs(b[17*9]-wanted)<=2+.012*wanted,(what,seed,scene,b[17*9],wanted)
                    k=b[17*9]/base
                    for loc in (1,4):
                        d0=(before[17*9+loc]-32768)/32768
                        d1=(b[17*9+loc]-32768)/32768
                        assert abs(d1**3-k*d0**3)<.0005,(what,seed,loc,'modulation gain changed')
                if delta:
                    changes[what] = changes.get(what, 0)+1
                calls += 1
    print('%d paired ARM draws, five note/scale/tempo/kit contexts: PASS (%.1fs)' %
          (calls, time.monotonic()-t))
    print('Changed recipes:', changes)
    if not args.packing_only:
        assert set(changes)==set(range(54)), 'the gain correction must cover all 54 recipes'
    if args.live:
        assert Path(args.after).resolve() == Path('build').resolve(), '--live uses the current build'
        live_check()


def live_check():
    import test_modnote_random as M
    import test_patch_random as P
    addr = symbol('build/np_patch.elf', '::audible_filter(')
    def watch(uc):
        calls = []
        uc.hook_add(UC_HOOK_CODE, lambda u, a, s, d: calls.append(u.reg_read(UC_ARM_REG_R1)),
                    begin=addr, end=addr)
        uc.ctl_flush_tb()
        return calls
    r = M.Rig()
    calls = watch(r.uc)
    r.randomize(r.index(41), force=4)
    assert calls == [3], calls
    rec = {(s, l): v for n, s, l, v in r.records()[0] if n == r.index(41)}
    assert rec[(9, 0)] <= (129-3)*256
    r.play(41, 10, 10)
    assert calls == [3], 'playing a saved note must not rebalance it'
    calls.clear()
    m = r.m
    m.press(26); m.settle(30); m.press(25); m.settle(30)
    m.press(35); m.settle(60); m.release(35); m.settle(30)
    m.release(25); m.settle(30); m.release(26); m.settle(30)
    assert len(calls) == 8 and calls[3:5] == [3, 4], calls
    recs = r.records()[0]
    for col in (3, 4):
        note = r.index(41)//8*8+col
        cut = next(v for n, s, l, v in recs if n == note and s == 9 and l == 0)
        assert cut <= (129-3)*256
    p = P.Rig()
    calls = watch(p.uc)
    p.diag(P.D_MNRAND_FORCE, 4)
    p.chord(34)
    assert calls == [3], calls
    assert p.patch()[(9, 0)] <= (186-p.ui(0x94))*256
    print('Installed paths: mod-note, normal patch, eight-note drum kit; playback leaves saved sounds alone: PASS')


if __name__ == '__main__':
    main()
