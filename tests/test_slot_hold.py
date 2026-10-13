#!/usr/bin/env python3
"""Patch Y: a modulation slot is evaluated once per audio block.

The stock runs every slot's evaluator on all eight samples of the block
whatever the slot does.  The wrapper evaluates it on the first sample and
holds the level for the other seven -- but a held call has to move the slot
as far as eight would, or every LFO and envelope in it runs at an eighth of
its rate.  That is what this checks: the call count, the state advance, that
the scaled increments are put back exactly, and that an audio-rate slot is
never held.  Plus Y2, the setRate guard.
"""
import struct
import sys
import collections

from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0

sys.path.insert(0, 'tests')
from sysemu import Mono, VEC_EXTI2

LINAD = 0x0811108C
EXPAD = 0x08110894
SETRATE = 0x08156910
AUDIO_FLAG = 0x25                     # owner+0x25: audio rate
XENV_INC = (0x1FC, 0x200, 0x204)      # attack / decay / release
WAVE_STEP = (0x060, 0x08C, 0x0B0, 0x0D4, 0x0F8)
RAMP = 0x210                          # the X-Env attack ramp
PHASE = 0x058                         # the sine LFO phase

ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print("  %-52s ok" % label)
    else:
        fail += 1
        print("  %-52s FAIL got %r want %r" % (label, got, want))


def close(label, got, want, tol):
    check("%s (%.8g ~ %.8g)" % (label, got, want), abs(got - want) <= tol, True)


def main(image='build/firmware-patched.bin'):
    blob = open(image, 'rb').read()
    guard = struct.unpack_from('<I', blob, 0x75200 + 0x160)[0] & ~1

    m = Mono(image)
    m.boot()
    m.factory_defaults()
    m.settle(150, audio=1)
    uc = m.uc
    f32 = lambda a: struct.unpack('<f', bytes(uc.mem_read(a, 4)))[0]

    m.press(34)
    m.settle(100, audio=1)

    # which slot objects the evaluators are called for
    owners = []
    h = uc.hook_add(UC_HOOK_CODE,
                    lambda u, a, s, d: owners.append(u.reg_read(UC_ARM_REG_R0)),
                    begin=LINAD, end=LINAD)
    m.inject(VEC_EXTI2)
    uc.hook_del(h)
    slots = sorted(set(owners))
    check("LinAD slots reached", len(slots) > 0, True)
    target = slots[0]
    check("the slot is not audio rate", uc.mem_read(target + AUDIO_FLAG, 1)[0], 0)

    # ---- call count ---------------------------------------------------
    calls = collections.Counter()
    for addr, name in ((LINAD, 'linad'), (EXPAD, 'expad')):
        uc.hook_add(UC_HOOK_CODE,
                    (lambda k: lambda u, a, s, d: calls.update([k]))(name),
                    begin=addr, end=addr)
    N = 8
    for _ in range(N):
        m.inject(VEC_EXTI2)
    per_block = calls['linad'] / float(N) / len(slots)
    close("a held slot is evaluated once per block", per_block, 1.0, 0.01)

    # ---- the increments come back exactly -----------------------------
    before = [f32(target + off) for off in XENV_INC + WAVE_STEP]
    m.inject(VEC_EXTI2)
    after = [f32(target + off) for off in XENV_INC + WAVE_STEP]
    check("every scaled increment is restored bit for bit", after, before)

    # ---- the state advances eight samples per block -------------------
    inc_attack = f32(target + 0x1FC)
    step_sine = f32(target + 0x060)
    check("the fixture's increments are nonzero",
          inc_attack != 0.0 and step_sine != 0.0, True)
    ramp = []
    phase = []
    for _ in range(12):
        m.inject(VEC_EXTI2)
        ramp.append(f32(target + RAMP))
        phase.append(f32(target + PHASE))
    # the ramp wraps at 1.0 and the phase at +-0.5; use a stretch that does
    # not wrap, and compare against eight of the per-sample increment
    d_ramp = [b - a for a, b in zip(ramp, ramp[1:]) if b > a]
    d_phase = [b - a for a, b in zip(phase, phase[1:]) if b > a]
    check("the ramp advanced", len(d_ramp) > 4, True)
    close("ramp advance per block is eight increments",
          sum(d_ramp) / len(d_ramp), 8.0 * inc_attack, 8.0 * inc_attack * 1e-3)
    check("the LFO phase advanced", len(d_phase) > 4, True)
    close("LFO phase advance per block is eight steps",
          sum(d_phase) / len(d_phase), 8.0 * step_sine, 8.0 * step_sine * 1e-3)

    # ---- an audio-rate slot is never held -----------------------------
    uc.mem_write(target + AUDIO_FLAG, b'\x01')
    calls.clear()
    for _ in range(N):
        m.inject(VEC_EXTI2)
    audio_rate_calls = calls['linad'] / float(N)
    held_calls = (len(slots) - 1) * 1.0
    close("an audio-rate slot keeps all eight calls",
          audio_rate_calls - held_calls, 8.0, 0.01)
    uc.mem_write(target + AUDIO_FLAG, b'\x00')

    # ---- Y2: setRate only when its input moved ------------------------
    m.pot_values[8] = 40000
    m.settle(20, audio=1)
    for _ in range(3):
        m.tap(2, audio=1)
        m.settle(25, audio=1)
    m.pot_values[18] = 60000
    m.settle(150, audio=1)
    sr = collections.Counter()
    for addr, name in ((guard, 'guard'), (SETRATE, 'real')):
        uc.hook_add(UC_HOOK_CODE,
                    (lambda k: lambda u, a, s, d: sr.update([k]))(name),
                    begin=addr, end=addr)
    for _ in range(N):
        m.inject(VEC_EXTI2)
    check("the setRate guard is on the per-sample path", sr['guard'] > 0, True)
    check("and it skips every call while the rate is unchanged", sr['real'], 0)
    # move the rate under it: the guard must let the real one through.  The
    # object is whichever LFO the audio-rate walk hands it, so take it from
    # the guard itself rather than guessing.
    seen = []
    hk = uc.hook_add(UC_HOOK_CODE,
                     lambda u, a, s, d: seen.append(u.reg_read(UC_ARM_REG_R0)),
                     begin=guard, end=guard)
    m.inject(VEC_EXTI2)
    uc.hook_del(hk)
    check("the guard's object was observed", len(seen) > 0, True)
    sr.clear()
    for lfo in set(seen):
        uc.mem_write(lfo + 8, struct.pack('<f', 0.123456))
    m.inject(VEC_EXTI2)
    check("a changed rate reaches the real setRate", sr['real'] > 0, True)

    # ---- Y3: the audio-rate walk collects once per block -------------
    walk_words = 0x2000E400 + 0xF00
    count = struct.unpack('<I', bytes(uc.mem_read(walk_words + 4, 4)))[0]
    check("the compact list holds the one audio-rate slot", count, 1)
    listed = struct.unpack('<I', bytes(uc.mem_read(walk_words + 16, 4)))[0]
    check("and it is a slot object flagged audio rate",
          uc.mem_read(listed + AUDIO_FLAG, 1)[0] != 0, True)
    # flip a second slot to audio rate: the next block's collect must see it
    uc.mem_write(target + AUDIO_FLAG, b'\x01')
    for _ in range(9):
        m.inject(VEC_EXTI2)
    count2 = struct.unpack('<I', bytes(uc.mem_read(walk_words + 4, 4)))[0]
    check("a mode change is picked up by the next block's walk", count2, 2)
    uc.mem_write(target + AUDIO_FLAG, b'\x00')
    for _ in range(9):
        m.inject(VEC_EXTI2)
    count3 = struct.unpack('<I', bytes(uc.mem_read(walk_words + 4, 4)))[0]
    check("and dropped again when it reverts", count3, 1)

    # ---- a gate flag set mid-block is taken on the next sample ---------
    # (the sequencer's interrupt lands between samples; a drum's X-Env
    # attack must start on its note, not at the next block boundary)
    uc.mem_write(target + AUDIO_FLAG, b'\x00')
    for _ in range(3):
        m.inject(VEC_EXTI2)                       # settle the counters
    evals = []
    hk2 = uc.hook_add(UC_HOOK_CODE,
                      lambda u, a, s, d: evals.append(True) if u.reg_read(UC_ARM_REG_R0) == target else None,
                      begin=LINAD, end=LINAD)
    # inject one block with the gate flag raised part-way through: hook the
    # DSP body's per-sample entry and raise the flag on its 4th call
    body_calls = [0]
    def raise_gate(u, a, s, d):
        body_calls[0] += 1
        if body_calls[0] == 4:
            uc.mem_write(target + 0x21D, b'\x01')
    hk3 = uc.hook_add(UC_HOOK_CODE, raise_gate, begin=0x08112488, end=0x08112488)
    m.inject(VEC_EXTI2)
    uc.hook_del(hk2); uc.hook_del(hk3)
    check("a held slot evaluates on the sample its gate flag is raised",
          len(evals), 2)                          # the boundary, then the gate
    check("...and the evaluator consumed the flag", uc.mem_read(target + 0x21D, 1)[0], 0)
    # the next block is back to one evaluation
    evals.clear()
    hk2 = uc.hook_add(UC_HOOK_CODE,
                      lambda u, a, s, d: evals.append(True) if u.reg_read(UC_ARM_REG_R0) == target else None,
                      begin=LINAD, end=LINAD)
    m.inject(VEC_EXTI2)
    uc.hook_del(hk2)
    check("...and the block after is held again", len(evals), 1)

    # ---- a held slot's output is a line, not a staircase ---------------
    # (a zero-order hold put a 250 us step on every held modulation)
    uc.mem_write(target + AUDIO_FLAG, b'\x00')
    slot_ix = uc.mem_read(target + 4, 1)[0]
    m.pot_values[slot_ix] = 40000; m.settle(30, audio=1)
    m.pot_values[18] = 62000; m.settle(300, audio=1)                 # open the amount
    for off in WAVE_STEP:
        uc.mem_write(target + off, struct.pack('<f', 0.004))           # a fast LFO
    m.settle(5, audio=1)
    from unicorn.arm_const import UC_ARM_REG_S0 as _S0_, UC_ARM_REG_LR as _LR_
    wrap = struct.unpack_from('<I', blob, 0x75200 + 0x158)[0] & ~1
    vals = []; ret = {}
    def on_wrap(u, a, s, d):
        if u.reg_read(UC_ARM_REG_R0) == target:
            ret['lr'] = u.reg_read(_LR_) & ~1
    def on_ret(u, a, s, d):
        if ret.get('lr') == a:
            vals.append(struct.unpack('<f', struct.pack('<I', u.reg_read(_S0_)))[0]); ret['lr'] = None
    hw = uc.hook_add(UC_HOOK_CODE, on_wrap, begin=wrap, end=wrap)
    hr = [uc.hook_add(UC_HOOK_CODE, on_ret, begin=r, end=r) for r in
          (0x0811185C, 0x08111D0C, 0x081127D8, 0x08112BF8, 0x08112D1A, 0x08112D2C, 0x08113182, 0x081131CC)]
    NB = 16
    for _ in range(NB):
        m.inject(VEC_EXTI2)
    uc.hook_del(hw)
    for h_ in hr: uc.hook_del(h_)
    check("captured eight samples per block from the held slot", len(vals), NB * 8)
    # judge only blocks where the output is off the clamps and moving
    blocks = [vals[b*8:(b+1)*8] for b in range(NB)]
    live = [b for b in range(NB) if 0.05 < min(blocks[b]) and max(blocks[b]) < 0.95 and max(blocks[b]) - min(blocks[b]) > 1e-3]
    check("some blocks have the modulation moving off the clamps (%d)" % len(live), len(live) >= 3, True)
    ripple = 0.0; seam_ratio = 0.0
    for b in live:
        st = [y - x for x, y in zip(blocks[b], blocks[b][1:])]
        ripple = max(ripple, max(st) - min(st))
        if b + 1 in live:
            seam = blocks[b+1][0] - blocks[b][-1]
            seam_ratio = max(seam_ratio, abs(seam) / max(1e-9, max(abs(x) for x in st)))
    check("uniform steps inside a block (max ripple %.2e)" % ripple, ripple < 1e-4, True)
    check("no jump at a block boundary (seam/step %.2f)" % seam_ratio, seam_ratio < 2.0, True)

    # ---- the block-mode word carries the SAMPLE COUNT, not a flag ------
    # np_wave_shape multiplies the LFO phase step by it.  While it said "1"
    # and np_wave_shape multiplied by a hardcoded 8, a gate event mid-block
    # advanced every held slot's phase a whole extra block: 2.00x the right
    # rate with one note per block, 3.00x with two.
    BLOCKMODE = 0x2000E400 + 0x13DC
    uc.mem_write(target + AUDIO_FLAG, b'\x00')
    for _ in range(4):
        m.inject(VEC_EXTI2)
    seen = []
    hk = uc.hook_add(UC_HOOK_CODE,
                     lambda u, a, s, d: seen.append(
                         struct.unpack('<I', bytes(u.mem_read(BLOCKMODE, 4)))[0])
                     if u.reg_read(UC_ARM_REG_R0) == target else None,
                     begin=LINAD, end=LINAD)
    nb = [0]
    def gate_at4(u, a, s, d):
        nb[0] += 1
        if nb[0] == 4:
            uc.mem_write(target + 0x21D, b'\x01')
    hb = uc.hook_add(UC_HOOK_CODE, gate_at4, begin=0x08112488, end=0x08112488)
    m.inject(VEC_EXTI2)
    uc.hook_del(hk); uc.hook_del(hb)
    check("a boundary call covers eight samples", seen[0] if seen else None, 8)
    check("the gate call covers the samples that are left (1..7), not 8",
          len(seen) > 1 and 1 <= seen[1] <= 7, True)
    check("and the block-mode word is cleared after the call",
          struct.unpack('<I', bytes(uc.mem_read(BLOCKMODE, 4)))[0], 0)

    # ---- the LFO phase advances at its true rate across gate events -----
    # a slow step, so the phase does not wrap (+-0.5) over the long run
    for off in WAVE_STEP:
        uc.mem_write(target + off, struct.pack('<f', 2.0e-5))
    m.inject(VEC_EXTI2)
    ph_addr = target + 0x058
    step = f32(target + 0x060)
    def phase_rate(nblocks, gate_every):
        for _ in range(4):
            m.inject(VEC_EXTI2)
        # The phase lives in [-0.5, 0.5) and wraps.  Sum the per-block
        # advances, each unwrapped (one block moves it by ~1.6e-4, far below
        # a half turn), instead of differencing the end points: those gave
        # -1561 % whenever the run happened to start near the wrap, which
        # depends on how far main() got before this point -- not on the DSP.
        travelled, prev = 0.0, f32(ph_addr)
        for b in range(nblocks):
            fire = gate_every and ((b + 1) % gate_every == 0)
            n = [0]
            def hook(u, a, s, d):
                n[0] += 1
                if fire and n[0] == 4:
                    uc.mem_write(target + 0x21D, b'\x01')
            hb2 = uc.hook_add(UC_HOOK_CODE, hook, begin=0x08112488, end=0x08112488)
            m.inject(VEC_EXTI2)
            uc.hook_del(hb2)
            now = f32(ph_addr)
            d = now - prev
            travelled += d - round(d)
            prev = now
        return travelled / (nblocks * 8.0 * step)
    close("no gate events: the phase runs at exactly its rate",
          phase_rate(40, 0), 1.0, 0.01)
    # a gate every block is 4,000 notes/second -- far past anything musical,
    # and the worst case for the residual double-count of the samples the
    # boundary call already advanced.  It was 2.00x before the count fix.
    r = phase_rate(40, 1)
    check("a gate every block stays well under the old 2.00x (%.2f)" % r,
          r < 1.75, True)
    # at a musical rate (10 notes/s = a gate every 400 blocks) the residual
    # is a fraction of a percent
    r = phase_rate(400, 100)
    check("40 notes/second drifts under 1 %% (%.2f %%)" % ((r - 1) * 100),
          abs(r - 1.0) < 0.01, True)

    # ---- a silent idle envelope records its stage and then holds -------
    # It used to leave whichever stage it had seen last, so the hold test
    # never matched and every envelope at rest ran the whole routine on
    # every sample -- 40.9 instructions a call instead of twelve.
    ENV_STATE = 0x2000E400 + 0x180        # entry 0 = the main ADSR
    E_FLAGS, E_Y, E_DY = 4, 0, 16
    m.release(34)
    m.settle(1500, audio=1)               # idle, discharging toward zero
    # drop it below the 2^-20 silence threshold and let one block run: the
    # silent exit must snap to zero, record idle and leave nothing to walk
    uc.mem_write(ENV_STATE + E_Y, struct.pack('<f', 1.0e-8))
    m.inject(VEC_EXTI2)
    check("a level under 2^-20 snaps to exactly zero", f32(ENV_STATE + E_Y), 0.0)
    flags = struct.unpack('<I', bytes(uc.mem_read(ENV_STATE + E_FLAGS, 4)))[0]
    check("...and records idle, so the next samples hold", flags & 0xFF, 5)
    check("...with a zero slope", f32(ENV_STATE + E_DY), 0.0)
    for _ in range(3):
        m.inject(VEC_EXTI2)
    check("...and it stays at zero", f32(ENV_STATE + E_Y), 0.0)
    # a new note must still leave silence immediately
    m.press(34)
    m.settle(30, audio=1)
    check("a new note leaves the silent hold", f32(ENV_STATE + E_Y) > 0.0, True)
    m.release(34)
    m.settle(50, audio=1)

    print()
    if fail:
        print("FAILED: %d check(s)" % fail)
        raise SystemExit(1)
    print("PASS -- slots evaluate once per block at the right rate (%d checks)" % ok)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'build/firmware-patched.bin')
