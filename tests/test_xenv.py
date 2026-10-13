#!/usr/bin/env python3
"""Execute the installed envelope hooks, not a host-side replacement.

Allocation/peripherals isolated by RandomRig; stock constructors, envelope
state machines, destinations and the patched RC model execute as ARM
instructions.  Covers the seven X-envelope sites and the main ADSR publish
site, each against a host mirror of np_env_rc_step (patch/np_xenv.S).
"""
import math
import struct
from pathlib import Path
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import *
from test_scale_random import RandomRig, ENGINE
from test_patch import BASE, BLOB, STOP

SITES = (0x0811C69E, 0x0811129A, 0x08110ABA, 0x0811271E,
         0x08112952, 0x08112F34, 0x0811255A)
INLINE_OWNER = {0x0811271E: 0x48, 0x08112952: 0xAAC, 0x08112F34: 0x17A8,
                0x0811255A: 0x48}
PREVIOUS = 0x0811255A       # OSC1 reads last tick's level, no step
MAIN = 0x08112DDA           # main ADSR: s17 -> engine+0x2F2C
MAIN_STORED = 0x08112DEA    # after the stock vstr s17,[r3]
MAIN_FILTER = 0x0811319A    # vfma s16,s15,s17: cutoff += amount^3 * env
XENV_CONSTS = 0x2000E540    # DIAG+0x140: nine constants, copied by np_early
NCONSTS = 9
ENV_STATE = 0x2000E580      # DIAG+0x180: 21 entries x 32 bytes
ENTRY = 32
OWNER = 0x20012000
K_MAIN = 3.6
K_XENV = 4.32


def sf(uc, reg):
    return struct.unpack('<f', struct.pack('<I', uc.reg_read(reg)))[0]


def bits(value):
    return struct.unpack('<I', struct.pack('<f', value))[0]


def f32r(x):
    return struct.unpack('<f', struct.pack('<f', x))[0]


def fma(a, b, c):
    return f32r(math.fma(a, b, c))


# FPSCR's cumulative exception bits (IOC DZC OFC UFC IXC IDC) are set by
# ordinary FP arithmetic, ours included; the stock never reads FPSCR into a
# register (475 `vmrs APSR_nzcv`, no `vmrs rN`), so they are unobservable
# and the thunks do not round-trip them.  Everything else -- the NZCV
# condition bits included, which the model never writes -- must match.
FPSCR_STICKY = 0x9F


def reg_equal(a, b, reg):
    va, vb = a.reg_read(reg), b.reg_read(reg)
    if reg == UC_ARM_REG_FPSCR:
        va &= ~FPSCR_STICKY; vb &= ~FPSCR_STICKY
    return va == vb


def entry(index):
    return ENV_STATE + ENTRY*index


def install_constants(m):
    """What np_early does at boot: the constants, flash -> DTCM.  The rigs
    never run np_early, so do it by hand from the header."""
    master, dtcm, state = struct.unpack('<3I', m.uc.mem_read(BLOB+0x120, 12))
    assert (dtcm, state) == (XENV_CONSTS, ENV_STATE)
    m.uc.mem_write(dtcm, bytes(m.uc.mem_read(master, NCONSTS*4)))
    m.uc.mem_write(ENV_STATE, bytes(ENTRY*21))


C = [f32r(v) for v in (math.log(4), 4/3, 1e-6, 1/32, 1/6, 1/24, 1/120, K_MAIN, K_XENV)]


def onepole_coeff(z):
    """1-exp(-z) as np_xenv.S computes it, float32 op by op."""
    z = min(z, 16.0) if z == z else 0.0
    z = max(z, 0.0)
    c = f32r(z*C[3])
    p = fma(-c, C[6], C[5])
    p = fma(-c, p, C[4])
    p = fma(-c, p, 0.5)
    p = fma(-c, p, 1.0)
    c = f32r(c*p)
    for _ in range(5): c = f32r(c*f32r(2-c))
    return c


class RcModel:
    """Host mirror of np_env_rc_step: an RC charge clocked by the stock
    attack ramp, then one first-order step towards the stage's target.
    Reads the same fields the ARM code does, relative to `base`.

    The step runs on the first sample of each eight-sample block, over the
    whole block (1-e^-8z), and the seven samples after it walk the straight
    line from where the block began to where it ends."""
    def __init__(self, k):
        self.k = f32r(k)
        # prev is the last stage seen + 1; 0 is a cleared entry, so the first
        # attack tick after boot is a (re)trigger, not a continuation
        self.y = 0.0; self.prev = 0; self.idle_decay = False
        self.y0 = 0.0; self.l0 = 0.0
        self.xlast = 0.0
        # E_FLAGS bits 29..31 and E_DY
        self.tick = 0; self.dy = 0.0

    def step(self, uc, base, raw):
        tick, self.tick = self.tick, (self.tick+1) & 7
        stage = uc.mem_read(base+0x31, 1)[0]
        # between boundaries the level walks the block's line -- unless the
        # stage machine moved since the last full step, which is taken on
        # the sample it happens (a drum's note-on must not wait for the
        # boundary)
        # ...or a sub-tick retrigger landed in decay: the stage stays 1 and
        # only the decay accumulator jumps back up, which is taken at once too
        retrig = stage == 1 and bits(struct.unpack('<f', uc.mem_read(base+0x24, 4))[0]) > bits(self.xlast)
        if tick and stage + 1 == self.prev and not retrig:
            self.y = min(max(f32r(self.y+self.dy), 0.0), 1.0)
            return self.y
        self.tick = 1                    # every full step is sample 0 of a block
        rd = lambda off: struct.unpack('<f', uc.mem_read(base+off, 4))[0]
        y = start = min(max(self.y, 0.0), 1.0)
        if stage == 0:
            if self.prev != 1:
                # (re)trigger from any stage: the charge continues from the
                # current level, as the stock ramp does (no snap to zero)
                self.y0, self.l0 = y, f32r(raw-rd(0x0C))
            self.prev = 1; self.idle_decay = False
            invsp = f32r(1/max(f32r(1-self.l0), C[2]))
            # the ramp where this block ends, seven increments on
            u = f32r(fma(rd(0x0C), 7.0, f32r(raw-self.l0))*invsp)
            c = onepole_coeff(f32r(u*C[0]))
            y = fma(f32r(c*f32r(1-self.y0)), C[1], self.y0)
        elif stage <= 4:
            if stage == 1:
                # a retrigger whose attack fits inside one tick never shows
                # stage 0; the decay accumulator jumping back up is its trace
                x = rd(0x24)
                if self.prev != 2 or x > self.xlast: y = 1.0
                self.xlast = x
                target = 0.0 if uc.mem_read(base+0x2F, 1)[0] else rd(0x18)
                z = f32r(rd(0x10)*self.k)
            elif stage == 2:
                target, z = rd(0x18), f32r(rd(0x10)*self.k)
            elif stage == 3:
                target, z = 0.0, f32r(rd(0x14)*self.k)
            else:
                if bits(y) >> 23 < 107:      # below 2^-20 is silence
                    self.y = self.dy = 0.0
                    self.prev = 5            # records idle, as .Lsilent does
                    return 0.0
                self.idle_decay = self.prev == 2 or (self.prev == 5 and self.idle_decay)
                target, z = 0.0, f32r(rd(0x10 if self.idle_decay else 0x14)*self.k)
            start = y                        # the peak snap moves it
            self.prev = stage+1
            y = fma(f32r(target-y), onepole_coeff(f32r(z*8.0)), y)
        else:
            self.y, self.dy = raw, 0.0
            return raw
        y = min(max(y, 0.0), 1.0)
        self.dy = f32r(f32r(y-start)*0.125)
        self.y = min(max(f32r(start+self.dy), 0.0), 1.0)
        return self.y


class EnvelopeRig(RandomRig):
    def __init__(self, linear=False):
        super().__init__()
        self.linear = linear
        self.samples = []
        self.main = []
        self.models = {}
        uc = self.m.uc
        install_constants(self.m)
        image = Path('build/firmware.bin').read_bytes()
        if linear:
            for site in SITES + (MAIN,): uc.mem_write(site, image[site-BASE:site-BASE+4])
        for site in SITES:
            uc.hook_add(UC_HOOK_CODE, self.before, begin=site, end=site)
            uc.hook_add(UC_HOOK_CODE, self.after, begin=site+4, end=site+4)
        uc.hook_add(UC_HOOK_CODE, self.main_before, begin=MAIN, end=MAIN)
        uc.hook_add(UC_HOOK_CODE, self.main_after, begin=MAIN_STORED, end=MAIN_STORED)
        uc.hook_add(UC_HOOK_CODE, self.main_filter, begin=MAIN_FILTER, end=MAIN_FILTER)

    def model(self, index, k):
        if index not in self.models: self.models[index] = RcModel(k)
        return self.models[index]

    def owner_at(self, uc, site):
        r4 = uc.reg_read(UC_ARM_REG_R4)
        return r4 + INLINE_OWNER.get(site, 0)

    def before(self, uc, address, size, data):
        owner = self.owner_at(uc, address)
        slot = uc.mem_read(owner+4, 1)[0]
        index = 1 + (slot if slot <= 18 else 19)
        m = self.model(index, K_XENV)
        if address == PREVIOUS:
            raw = struct.unpack('<f', uc.mem_read(owner+0x230, 4))[0]
            expected = raw if self.linear else m.y
        else:
            raw = sf(uc, UC_ARM_REG_S15)
            expected = raw if self.linear else m.step(uc, owner+0x1F0, raw)
        self.pending = (address, raw, expected, index)

    def after(self, uc, address, size, data):
        site, raw, expected, index = self.pending
        assert site+4 == address
        value = sf(uc, UC_ARM_REG_S10 if site == PREVIOUS else UC_ARM_REG_S15)
        assert abs(value-expected) < 2e-5, (hex(site), raw, value, expected)
        if not self.linear and site != PREVIOUS:
            stored = struct.unpack('<f', uc.mem_read(entry(index), 4))[0]
            assert stored == value
        self.samples.append((site, raw, value))

    def main_before(self, uc, address, size, data):
        raw = sf(uc, UC_ARM_REG_S17)
        engine = uc.reg_read(UC_ARM_REG_R4)
        # the thunk's 60-byte frame; the site's SP is 8-byte aligned
        assert uc.reg_read(UC_ARM_REG_SP) % 8 == 0
        self.main_raw = raw
        self.main_expected = raw if self.linear else self.model(0, K_MAIN).step(uc, engine, raw)

    def main_after(self, uc, address, size, data):
        raw = self.main_raw
        engine = uc.reg_read(UC_ARM_REG_R5) - 0x2000
        published = struct.unpack('<f', uc.mem_read(engine+0x2F2C, 4))[0]
        assert abs(published-self.main_expected) < 2e-5, (len(self.main), raw, published, self.main_expected)
        assert sf(uc, UC_ARM_REG_S17) == published
        # the state machine's own level stays raw (the RC model never writes it)
        assert struct.unpack('<f', uc.mem_read(engine+0x40, 4))[0] == raw
        if not self.linear:
            assert struct.unpack('<f', uc.mem_read(entry(0), 4))[0] == published
        self.main.append((raw, published))

    def main_filter(self, uc, address, size, data):
        # the filter ENV AMOUNT term multiplies whatever is in s17 here
        assert sf(uc, UC_ARM_REG_S17) == self.main[-1][1]

    def init_owner(self, constructor, slot, dest, mode, amount, owner=OWNER):
        uc = self.m.uc
        uc.reg_write(UC_ARM_REG_S0, bits(126))
        self.call(constructor, owner, slot)
        self.f32(owner+0x124, 0.25)
        self.f32(owner+0x164, 0.65)  # nonzero LFO contribution for routing
        self.f32(owner+0x284, amount)
        self.f32(owner+0x1FC, 1/32)  # attack 32 ticks
        self.f32(owner+0x200, 1/128) # decay 128 ticks
        self.f32(owner+0x204, 1/80)  # release
        uc.mem_write(owner+0x26, bytes([dest]))
        uc.mem_write(owner+0x10C, bytes([min(mode, 2), 2]))
        uc.mem_write(owner+0x25, bytes([mode == 3]))
        uc.mem_write(owner+0x234, b'\x00')
        uc.mem_write(owner+0x21D, b'\x01')


def test_step_direct():
    """np_env_rc_step called directly (r0 base, r1 entry, s12 k, s15 raw)
    through scripted stage sequences the audio fixture does not reach: a
    1 ms attack, the short-gate AD path, idle-rate selection, an instant
    attack, a moved sustain knob, release, an unknown stage, poisoned RAM,
    and the X-Env constant against the main one."""
    p = EnvelopeRig()
    uc = p.m.uc
    fn = p.m.u32(BLOB+0x114)
    arg = ENGINE+0x44
    for k, index in ((K_MAIN, 0), (K_XENV, 7)):
        model = RcModel(k)
        ent = entry(index)
        def call(stage, raw, short=0):
            uc.mem_write(arg+0x31, bytes([stage]))
            uc.mem_write(arg+0x2F, bytes([short]))
            want = model.step(uc, arg, raw)
            uc.reg_write(UC_ARM_REG_S12, bits(k))
            uc.reg_write(UC_ARM_REG_S15, bits(raw))
            p.call(fn, arg, ent)
            got = sf(uc, UC_ARM_REG_S15)
            assert abs(got-want) < 2e-5, (k, stage, raw, got, want)
            assert struct.unpack('<f', uc.mem_read(ent, 4))[0] == got
            return got
        def align(stage, raw, short=0):
            """Run out the current block so the next call is a boundary.
            The fillers repeat the stage the model is in -- a stage change
            is taken on the sample it happens, so a filler in the NEW stage
            would start it early -- and are therefore holds."""
            cur = model.prev - 1 if model.prev else stage
            while model.tick: call(cur, raw, short)
        p.f32(arg+0x0C, 1/32); p.f32(arg+0x10, 1/50); p.f32(arg+0x14, 1/400)
        p.f32(arg+0x18, 0.6)
        uc.mem_write(ent, bytes(ENTRY))
        # 1 ms attack from silence: convex, 2/3 at half time, 98 % on the last tick
        ys = [call(0, (i+1)/32) for i in range(31)]
        assert 0.65 < ys[15] < 0.68 and 0.98 < ys[30] < 0.99, (ys[15], ys[30])
        # short gate: decay to zero (+0x2F), then idle at the *decay* rate
        align(1, 1.0, short=1)
        ys = []
        for i in range(49):
            p.f32(arg+0x24, 1-(i+1)/50); ys.append(call(1, 1-(i+1)/50, short=1))
        c1 = onepole_coeff(f32r(k/50))
        # a block is eight of those steps and its fall is spread over its
        # eight samples, so the boundary sample shows an eighth of it; by
        # the last sample the block has landed exactly where per-sample
        # stepping would, (1-c8) being (1-c1)^8 to the bit
        c8 = onepole_coeff(f32r(f32r(k/50)*8.0))
        assert abs(ys[0]-(1-c8/8)) < 2e-5, (ys[0], 1-c8/8)
        assert ys[-1] < math.exp(-k*0.96)*1.05   # 48 samples, six blocks
        assert all(a > b for a, b in zip(ys, ys[1:]))
        # idle keeps discharging at the decay rate, not the release rate:
        # run the decay's block out, then one whole idle block, whose two
        # endpoints are exactly eight per-sample steps apart
        align(4, 0.0)
        y0 = struct.unpack('<f', uc.mem_read(ent, 4))[0]
        for _ in range(8): y_idle = call(4, 0.0)
        ratio = y_idle/y0
        assert abs(ratio-(1-c1)**8) < 1e-3, (ratio, (1-c1)**8)
        assert abs(ratio-(1-onepole_coeff(f32r(k/400)))**8) > 0.3  # not release
        # the cached coefficient is keyed on z, a whole block of it
        zkey, coef = struct.unpack('<If', uc.mem_read(ent+20, 8))
        assert zkey == bits(f32r(f32r(f32r(k)*f32r(1/50))*8.0)), hex(zkey)
        assert abs(coef-c8) < 2e-7, (coef, c8)
        # instant attack: the ramp jumped straight to decay; y snaps to the peak
        align(1, 0.99)
        p.f32(arg+0x24, 0.99)
        y = call(1, 0.99)
        assert abs(y-(1-0.4*c8/8)) < 2e-5, y
        # a retrigger whose attack fits in one tick: stage stays 1, only the
        # decay accumulator jumps back up -- y must restart from the peak
        for i in range(20): p.f32(arg+0x24, 0.99-(i+1)/50); y = call(1, 0.6+0.4*(0.99-(i+1)/50))
        assert y < 0.85
        align(1, 0.998)
        p.f32(arg+0x24, 0.995)
        y = call(1, 0.998)
        assert abs(y-(1-0.4*c8/8)) < 2e-5, y
        for i in range(30): p.f32(arg+0x24, 0.995-(i+1)/50); y = call(1, 0.6+0.4*(0.995-(i+1)/50))
        # decay towards S, then the sustain knob moves and y follows it
        for i in range(49): p.f32(arg+0x24, 1-(i+1)/50); y = call(1, 0.6+0.4*(1-(i+1)/50))
        assert abs(y-0.6) < 0.02
        for _ in range(20): y = call(2, 0.6)
        assert abs(y-0.6) < 0.004
        p.f32(arg+0x18, 0.9)
        align(2, 0.9)
        ys = [call(2, 0.9) for _ in range(60)]
        assert ys[0] > 0.6 and ys[-1] > 0.88 and all(b > a for a, b in zip(ys, ys[1:]))
        # release from sustain, exponential: half-life well before the ramp's
        align(3, 0.9)
        ys = [call(3, 0.9*(1-(i+1)/400)) for i in range(400)]
        half = next(i for i, v in enumerate(ys) if v < 0.45)
        assert abs(half-400*math.log(2)/k) < 3 and ys[-1] < 0.03, (half, ys[-1])
        # unknown stage passes the raw level through and adopts it as state
        align(7, 0.31)
        assert call(7, 0.31) == f32r(0.31)
        # poisoned state is treated as silence
        align(3, 0.5)
        uc.mem_write(ent, struct.pack('<I', 0x7FC00000))
        model.y = 0.0
        assert call(3, 0.5) == 0.0
    # the 20 % steeper X-Env decay: k = 4.32 falls faster than k = 3.6
    assert onepole_coeff(f32r(K_XENV/50)) > onepole_coeff(f32r(K_MAIN/50))*1.19
    print('np_env_rc_step direct, k = 3.6 and 4.32: attack, short-gate AD, idle'
          ' rate, coefficient cache, instant attack, sustain follow, release, guards OK')


def test_slots():
    count = 0
    for constructor, renderer, site, slot in (
            (0x0811D0DC, 0x0811C464, SITES[0], 0),
            (0x0811CF60, 0x0811108C, SITES[1], 1),
            (0x0811D268, 0x08110894, SITES[2], 8)):
        for dest in (0, 1):
            for mode in range(4):
                for amount in (0.2, 0.5, 0.8):
                    a, b = EnvelopeRig(True), EnvelopeRig()
                    for p in (a, b): p.init_owner(constructor, slot, dest, mode, amount)
                    for tick in range(240):
                        for p in (a, b):
                            if tick in (70, 155): p.m.uc.mem_write(OWNER+0x21D, b'\x01')
                            if tick == 110: p.m.uc.mem_write(OWNER+0x21E, b'\x01')
                            before = len(p.samples)
                            p.call(renderer, OWNER)
                            assert len(p.samples) == before+1
                            assert p.samples[-1][0] == site
                        # All envelope state, triggers, stage counters and
                        # raw values stay bit-identical after every tick.
                        assert a.m.uc.mem_read(OWNER+0x1F0, 0x48) == b.m.uc.mem_read(OWNER+0x1F0, 0x48)
                        if amount == 0.5:
                            assert a.m.uc.mem_read(OWNER+8, 8) == b.m.uc.mem_read(OWNER+8, 8)
                    raw = [r for _, r, _ in b.samples]
                    y = [v for _, _, v in b.samples]
                    assert max(raw) > 0.9 and min(raw) < 0.1
                    assert any(abs(r-v) > 0.2 for r, v in zip(raw, y))
                    # convex attack (32 ticks from silence): 2/3 at half time
                    assert 0.64 < y[15] < 0.69, y[15]
                    count += 1
    print('Real slot renderers: routing/mode/signed amount/retrigger/release:', count, 'x 240 ticks')


def test_abi():
    gprs = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
            UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
            UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11,
            UC_ARM_REG_R12, UC_ARM_REG_LR, UC_ARM_REG_SP,
            UC_ARM_REG_APSR, UC_ARM_REG_FPSCR, UC_ARM_REG_PRIMASK]
    fps = [globals()['UC_ARM_REG_S'+str(i)] for i in range(32)]
    arg = ENGINE+0x44
    for site in SITES:
        owner = arg + INLINE_OWNER.get(site, 0)
        for stage, x in ((0, 0.3), (1, 0.7), (3, 0.5), (4, 0.0)):
            b, a = EnvelopeRig(), EnvelopeRig(True)
            for p in (b, a):
                uc = p.m.uc
                # Activate the M-class FP context before seeding FPSCR;
                # otherwise Unicorn's first FP instruction resets it.
                uc.reg_write(UC_ARM_REG_S15, bits(0.5))
                uc.reg_write(UC_ARM_REG_S12, bits(K_XENV))
                p.call(p.m.u32(BLOB+0x114), arg, entry(20))
                for i, reg in enumerate(gprs[:14]): uc.reg_write(reg, 0x12340000+i*4)
                uc.reg_write(UC_ARM_REG_R4, arg if site in INLINE_OWNER else owner)
                for i, reg in enumerate(fps): uc.reg_write(reg, bits(0.1+i/64))
                uc.mem_write(owner+4, bytes([5]))
                uc.mem_write(owner+0x1F0+0x31, bytes([stage]))
                p.f32(owner+0x1FC, 1/32); p.f32(owner+0x200, 1/64); p.f32(owner+0x204, 1/60)
                p.f32(owner+0x208, 0.4)
                p.f32(owner+0x230, x)
                p.f32(entry(6), 0.55)
                uc.reg_write(UC_ARM_REG_S15, bits(x))
                p.f32(owner+0x1DC, 0.25)
                uc.mem_write(owner+0x26, b'\x01')
                if p is a:
                    # stock instruction: the value passes through; seed the
                    # patched result so everything else must be identical
                    if site == PREVIOUS:
                        p.f32(owner+0x230, sf(b.m.uc, UC_ARM_REG_S10))
                    else:
                        uc.reg_write(UC_ARM_REG_S15, b.m.uc.reg_read(UC_ARM_REG_S15))
                uc.reg_write(UC_ARM_REG_APSR, 0xA8000000)
                uc.reg_write(UC_ARM_REG_FPSCR, 0x50000010)
                uc.reg_write(UC_ARM_REG_PRIMASK, 0)
                uc.emu_start(site | 1, site+4, count=2000)
                assert uc.reg_read(UC_ARM_REG_PC) == site+4
            for reg in gprs + fps:
                assert reg_equal(a.m.uc, b.m.uc, reg), (hex(site), stage, reg,
                    hex(a.m.uc.reg_read(reg)), hex(b.m.uc.reg_read(reg)))
            result = sf(b.m.uc, UC_ARM_REG_S10 if site == PREVIOUS else UC_ARM_REG_S15)
            assert 0 <= result <= 1
            if site == PREVIOUS: assert result == f32r(0.55)
    print('All seven installed X-Env splices: registers/stack/APSR/FPSCR/PRIMASK preserved: 28 OK')
    for stage, level in ((0, 0), (1, 0.5), (2, 0.4), (3, 0.9), (4, 0.01), (7, 0.3)):
        b, a = EnvelopeRig(), EnvelopeRig(True)
        for p in (b, a):
            uc = p.m.uc
            uc.reg_write(UC_ARM_REG_S15, bits(0.5))
            uc.reg_write(UC_ARM_REG_S12, bits(K_MAIN))
            p.call(p.m.u32(BLOB+0x114), arg, entry(20))
            for i, reg in enumerate(gprs[:14]): uc.reg_write(reg, 0x12340000+i*4)
            uc.reg_write(UC_ARM_REG_R4, arg)
            uc.reg_write(UC_ARM_REG_R5, arg+0x2000)
            uc.mem_write(arg+0x31, bytes([stage]))
            p.f32(arg+0x0C, 1/24); p.f32(arg+0x10, 1/64); p.f32(arg+0x14, 1/60)
            p.f32(arg+0x18, 0.4)
            p.f32(entry(0), level)
            for i, reg in enumerate(fps): uc.reg_write(reg, bits(0.1+i/64))
            uc.reg_write(UC_ARM_REG_S17, bits(0.25))
            if p is a:
                uc.reg_write(UC_ARM_REG_S17, b.m.uc.reg_read(UC_ARM_REG_S17))
            uc.reg_write(UC_ARM_REG_APSR, 0xA8000000)
            uc.reg_write(UC_ARM_REG_FPSCR, 0x50000010)
            uc.reg_write(UC_ARM_REG_PRIMASK, 0)
            uc.emu_start(MAIN | 1, MAIN+4, count=2000)
            assert uc.reg_read(UC_ARM_REG_PC) == MAIN+4
        for reg in gprs + fps:
            assert reg_equal(a.m.uc, b.m.uc, reg), ('main', stage, reg)
        assert a.m.uc.reg_read(UC_ARM_REG_R3) == arg+0x2F2C
        got = sf(b.m.uc, UC_ARM_REG_S17)
        if stage == 7:
            assert got == f32r(0.25)      # unknown stage: raw passes through
        else:
            assert got != f32r(0.25) and 0 <= got <= 1
    print('Main ADSR splice: registers/stack/APSR/FPSCR/PRIMASK preserved, six stages OK')


def test_audio_block():
    # Real audio argument and complete engine constructor. Execute the block
    # from its true entry through cutoff output (before physical DAC I/O).
    # GPIO and the oscillator waveform-to-CV mapper are isolated. The
    # latter uses FP64 unsupported by Unicorn M7; its slot renderers are
    # separately exercised above. The three inline envelopes run here.
    arg = ENGINE+0x44
    owners = (arg+0x48, arg+0xAAC, arg+0x17A8)
    a, b = EnvelopeRig(True), EnvelopeRig()
    for p in (a, b):
        p.call(0x081473F4, arg)
        p.m.uc.mem_write(0x081029CC, b'\x70\x47')
        p.m.uc.mem_write(0x08111848, b'\x70\x47')
        # Unicorn M7 lacks VRINTA here. This rounds the output-mode knob,
        # not envelope data; that knob is exactly zero in this fixture.
        def output_mode_round(uc, address, size, data):
            assert sf(uc, UC_ARM_REG_S14) == 0
            uc.reg_write(UC_ARM_REG_S15, 0)
            uc.reg_write(UC_ARM_REG_PC, (address+size)|1)
        p.m.uc.hook_add(UC_HOOK_CODE, output_mode_round, begin=0x081124D0, end=0x081124D0)
        for owner, slot in zip(owners, (0, 4, 9)):
            p.init_owner(0x0811D0DC, slot, 0, 0, 0.8, owner)
        p.f32(owners[0]+0x124, 0)  # exercise previous-sample OSC1 path too
        # main ADSR: fast stages, sustain 0.4, ENV AMOUNT knob at 0.9 so
        # the filter term is live; the stock lookup flag must be off.
        assert p.m.uc.mem_read(arg+0x44, 1) == b'\x00'
        p.f32(arg+0x0C, 1/24); p.f32(arg+0x10, 1/64); p.f32(arg+0x14, 1/60)
        p.f32(arg+0x18, 0.4)
        p.f32(arg+0x1F74+0x124, 0.9)
    gate = {0: (1, 0, 1), 100: (0, 1, 0), 150: (1, 0, 1), 200: (0, 1, 0), 225: (1, 0, 0)}
    for tick in range(240):
        for p in (a, b):
            uc = p.m.uc
            for owner in owners:
                if tick in (70, 155): uc.mem_write(owner+0x21D, b'\x01')
                if tick == 110: uc.mem_write(owner+0x21E, b'\x01')
            if tick in gate:
                trig, off, held = gate[tick]
                uc.mem_write(arg+0x2D, bytes([trig]))
                uc.mem_write(arg+0x2E, bytes([off]))
                uc.mem_write(arg+0x33, bytes([held]))
            sp = uc.reg_read(UC_ARM_REG_SP)
            uc.reg_write(UC_ARM_REG_R0, arg)
            uc.reg_write(UC_ARM_REG_LR, STOP|1)
            try:
                uc.emu_start(0x08112489, 0x081131C6, count=20000)
            except Exception:
                print('audio failed at', hex(uc.reg_read(UC_ARM_REG_PC)), flush=True)
                raise
            assert uc.reg_read(UC_ARM_REG_PC) == 0x081131C6, hex(uc.reg_read(UC_ARM_REG_PC))
            # We stopped before the stock epilogue / unrelated DAC code.
            assert uc.reg_read(UC_ARM_REG_SP) == sp-64
            uc.reg_write(UC_ARM_REG_SP, sp)
        for owner in owners:
            assert a.m.uc.mem_read(owner+0x1F0, 0x48) == b.m.uc.mem_read(owner+0x1F0, 0x48)
        # main ADSR increments, stage, triggers, timers and raw level: identical
        assert a.m.uc.mem_read(arg+0x0C, 0x3C) == b.m.uc.mem_read(arg+0x0C, 0x3C)
        assert len(a.main) == len(b.main) == tick+1
        assert a.main[-1][0] == b.main[-1][0]
    seen = {s for s, _, _ in b.samples}
    assert set(SITES[3:]) <= seen, [hex(s) for s in seen]
    raw = [r for r, _ in b.main]
    y = [v for _, v in b.main]
    assert max(raw) > 0.95 and min(raw) < 0.05
    assert [round(v, 6) for _, v in a.main] == [round(r, 6) for r in raw]
    # RC attack: convex -- two thirds of the way at half the attack time.
    # The stock switches to decay on the tick its ramp reaches 1 and that
    # transition is taken on that very tick: the decay's first sample comes
    # off the snapped peak of 1.0, so the peak itself is never published.
    assert 0.64 < y[11] < 0.69 and 0.97 < y[22] < 0.99, (y[11], y[22])
    assert 0.96 < y[23] < 0.98 and y[24] < y[23], (y[23], y[24])
    # sustain is linear: the level settles on S itself, not on a curve of it
    assert abs(y[99]-0.4) < 0.012 and all(v > raw_ - 1e-6 for v, raw_ in zip(y[90:100], raw[90:100]))
    # decay/release are exponential: faster than the linear ramp early on
    assert y[40] < raw[40] and y[110] < raw[110]
    # retrigger from mid-release continues from the residual and meets the
    # peak with the stock's shortened ramp; nothing -- the peak snap
    # included -- makes a step bigger than the attack's own first block
    steps = [q-p for p, q in zip(y, y[1:])]
    assert max(steps) < 0.13 and min(steps) > -0.04, (max(steps), min(steps))
    assert max(steps[:224]) < 0.08
    # a gate opening on any tick is taken on that tick; out of release the
    # attack (re)triggers and the RC charge CONTINUES from the release
    # residual, as the stock ramp and an analogue cap do -- no snap to zero,
    # so no downward step at the retrigger (that snap was the click that was
    # heard on the VCA envelope, reverted after p27).
    for gate_on in (150, 225):
        assert y[gate_on-1] > 0.0, gate_on                      # a tail to continue
        # the retrigger sample does not jump down
        assert y[gate_on] >= y[gate_on-1] - 1e-3, (gate_on, y[gate_on-1], y[gate_on])
        # and the attack then rises toward the peak
        assert all(q >= p - 1e-3 for p, q in zip(y[gate_on:gate_on+8], y[gate_on+1:gate_on+9]))
        assert y[gate_on+8] > y[gate_on], (gate_on, y[gate_on], y[gate_on+8])
    # and both attacks end the same way: the decay's first sample off the peak
    for peak in (23, 238):
        assert 0.96 < y[peak] < 0.98 and y[peak-1] > 0.88, (peak, y[peak-1], y[peak])
    # the inlined OSC1 X-Env (32-tick attack, k = 4.32 decay) is convex too,
    # and the previous-sample read returns the level published last tick
    osc1 = [(r, v) for s, r, v in b.samples if s == 0x0811271E]
    prev = [v for s, _, v in b.samples if s == PREVIOUS]
    assert 0.64 < osc1[15][1] < 0.69, osc1[15]
    assert prev[1:] == [v for _, v in osc1[:-1]]
    print('True audio-block entry -> OSC1/OSC2/filter inline X-Envs + main ADSR'
          ' (RC attack/decay/sustain/release/retrigger vs host model): 240 ticks OK')


def test_note_every_step():
    """The reported case: instant attack, long decay, a note on every step.
    The stock retriggers inside one tick (stage never shows 0, only +0x24
    jumps back up); the RC level must restart from the peak each time, for
    the main ADSR and for the inlined X-Envs alike."""
    arg = ENGINE+0x44
    owners = (arg+0x48, arg+0xAAC, arg+0x17A8)
    p = EnvelopeRig()
    uc = p.m.uc
    p.call(0x081473F4, arg)
    uc.mem_write(0x081029CC, b'\x70\x47'); uc.mem_write(0x08111848, b'\x70\x47')
    def output_mode_round(uc, address, size, data):
        uc.reg_write(UC_ARM_REG_S15, 0); uc.reg_write(UC_ARM_REG_PC, (address+size)|1)
    uc.hook_add(UC_HOOK_CODE, output_mode_round, begin=0x081124D0, end=0x081124D0)
    for owner, slot in zip(owners, (0, 4, 9)):
        p.init_owner(0x0811D0DC, slot, 0, 0, 0.8, owner)
        p.f32(owner+0x1FC, 1.0)      # instant attack
        p.f32(owner+0x200, 1/2000)   # long decay
        uc.mem_write(owner+0x21D, b'\x00')
    p.f32(arg+0x0C, 1.0); p.f32(arg+0x10, 1/2000); p.f32(arg+0x14, 1/60); p.f32(arg+0x18, 0.0)
    # on block boundaries: the step runs once per eight samples, so that is
    # where a sub-tick retrigger is seen.  The spacing is 40 ticks either
    # way, which is five whole blocks.
    steps = (8, 48, 88, 128)
    for tick in range(160):
        if tick in steps:
            uc.mem_write(arg+0x2D, b'\x01'); uc.mem_write(arg+0x33, b'\x01')
            for owner in owners: uc.mem_write(owner+0x21D, b'\x01')
        sp = uc.reg_read(UC_ARM_REG_SP)
        uc.reg_write(UC_ARM_REG_R0, arg); uc.reg_write(UC_ARM_REG_LR, STOP|1)
        uc.emu_start(0x08112489, 0x081131C6, count=20000)
        assert uc.reg_read(UC_ARM_REG_PC) == 0x081131C6
        uc.reg_write(UC_ARM_REG_SP, sp)
        stage = uc.mem_read(arg+0x31, 1)[0]
        assert stage == (4 if tick < steps[0] else 1), (tick, stage)
    y = [v for _, v in p.main]
    osc1 = [v for s_, _, v in p.samples if s_ == 0x0811271E]
    for series, k in ((y, K_MAIN), (osc1, K_XENV)):
        c = onepole_coeff(f32r(f32r(k)*f32r(1/2000)))
        # one block is eight of those steps, and the block's fall is then
        # spread over its eight samples: the boundary sample is the first
        # eighth of it.  (1-c8) = (1-c)^8 to within float32, so a boundary
        # five blocks later is exactly where per-sample stepping left it.
        c8 = onepole_coeff(f32r(f32r(f32r(k)*f32r(1/2000))*8.0))
        for t in steps:
            # each note restarts the RC decay from the peak ...
            assert abs(series[t]-(1-c8/8)) < 2e-5, (t, series[t], 1-c8/8)
            # ... after having fallen for the whole previous step
            if t > steps[0]: assert series[t-1] < (1-c)**38, (t, series[t-1])
    print('Note on every step, instant attack, long decay: ADSR and X-Env restart from the peak: 4 x 2 OK')


def test_note_mid_block():
    """The drum case test_note_every_step deliberately avoids: an instant
    attack retriggered while the previous note is still in decay, landing
    in the middle of the envelope's eight-sample block.  The stage never
    leaves 1 -- only the decay accumulator jumps back up -- so before this
    was handled on the hold path the RC level kept falling until the next
    boundary: measured 3..6 samples late on the main ADSR and the inline
    X-Envs.  The new note must be on its own sample, from the peak."""
    arg = ENGINE+0x44
    owners = (arg+0x48, arg+0xAAC, arg+0x17A8)
    p = EnvelopeRig()
    uc = p.m.uc
    p.call(0x081473F4, arg)
    uc.mem_write(0x081029CC, b'\x70\x47'); uc.mem_write(0x08111848, b'\x70\x47')
    def output_mode_round(uc, address, size, data):
        uc.reg_write(UC_ARM_REG_S15, 0); uc.reg_write(UC_ARM_REG_PC, (address+size)|1)
    uc.hook_add(UC_HOOK_CODE, output_mode_round, begin=0x081124D0, end=0x081124D0)
    for owner, slot in zip(owners, (0, 4, 9)):
        p.init_owner(0x0811D0DC, slot, 0, 0, 0.8, owner)
        p.f32(owner+0x1FC, 1.0)      # instant attack
        p.f32(owner+0x200, 1/300)    # decay longer than the step
        uc.mem_write(owner+0x21D, b'\x00')
    p.f32(arg+0x0C, 1.0); p.f32(arg+0x10, 1/300); p.f32(arg+0x14, 1/60); p.f32(arg+0x18, 0.0)
    # the first note comes out of idle (a stage change); the rest land in
    # decay at every offset inside the block.  A full step re-phases the
    # envelope's block, so the offset of a note is its distance from the
    # previous one, mod 8: spacings 41..47 and 40 cover 1..7 and 0.
    steps = [8]
    for spacing in (41, 42, 43, 44, 45, 46, 47, 40):
        steps.append(steps[-1] + spacing)
    steps = tuple(steps)
    for tick in range(steps[-1] + 20):
        if tick in steps:
            uc.mem_write(arg+0x2D, b'\x01'); uc.mem_write(arg+0x33, b'\x01')
            for owner in owners: uc.mem_write(owner+0x21D, b'\x01')
        sp = uc.reg_read(UC_ARM_REG_SP)
        uc.reg_write(UC_ARM_REG_R0, arg); uc.reg_write(UC_ARM_REG_LR, STOP|1)
        uc.emu_start(0x08112489, 0x081131C6, count=20000)
        assert uc.reg_read(UC_ARM_REG_PC) == 0x081131C6
        uc.reg_write(UC_ARM_REG_SP, sp)
        assert uc.mem_read(arg+0x31, 1)[0] == (4 if tick < steps[0] else 1), tick
    y = [v for _, v in p.main]
    osc1 = [v for s_, _, v in p.samples if s_ == 0x0811271E]
    for series, name in ((y, 'main'), (osc1, 'osc1')):
        for t in steps[1:]:
            # falling right up to the note, the peak on the note's own sample
            assert series[t-1] < series[t-2], (name, t)
            assert series[t] > 0.95 and series[t] - series[t-1] > 0.2, \
                (name, t, series[t-1], series[t])
    assert {(b - a) % 8 for a, b in zip(steps, steps[1:])} == set(range(8))
    print('Instant attack retriggered mid-decay at every block offset: '
          'main ADSR and inline X-Env take it on the note sample: %d notes OK'
          % (len(steps) - 1))


if __name__ == '__main__':
    test_note_mid_block()
    test_note_every_step()
    test_step_direct()
    test_abi()
    test_slots()
    test_audio_block()
