/* Envelopes as RC networks, for stock V2.2.0r.
 *
 * Every envelope in the stock -- the main ADSR at engine+0x0C..+0x44 and
 * the fourteen X-envelopes at owner+0x1FC..+0x234 -- is the same
 * generated state machine of linear ramps: attack adds +0x0C per tick
 * from the level it was (re)triggered at until it reaches 1; decay walks
 * +0x24 from 1 to 0 and publishes S + x(1-S); sustain holds S; release
 * scales the level at note-off by 1 - phase; stage 4 idles at 0.  The
 * X-Env's fields sit at the same offsets plus 0x1F0 (+0x1FC/+0x200/+0x204
 * increments, +0x208 sustain, +0x21F "gate shorter than attack" AD flag,
 * +0x221 stage, +0x230 level), so one routine serves all fifteen.
 *
 * Analogue ADSRs -- CEM/AS3310, the Minimoog, Roland's IR3R01, ARP,
 * Doepfer -- are RC networks instead: the attack charges towards a voltage
 * above the peak and a comparator ends it at 67..83 % of the way, decay
 * and release discharge exponentially towards sustain and zero, and the
 * sustain voltage itself is linear.  np_env_rc_step reproduces that on a
 * level y of its own per envelope, driven by the stock stage machine so
 * every knob keeps its timing:
 *
 *   attack   The raw ramp runs from the level it was (re)triggered at,
 *            L0, to 1; its progress u = (raw-L0)/(1-L0) is the attack's
 *            clock.  y = y0 + (1-y0) (1-4^-u)/(1-4^-1): an RC charge from
 *            where y was, ending at 75 % of its asymptote -- between the
 *            3310's 71 % and the IR3R01's 83 % -- and reaching 1 exactly
 *            when the ramp does, however short a retrigger made this
 *            attack.  The stock switches to decay on the tick its ramp
 *            reaches 1 and publishes that tick as decay, so the first
 *            decay tick starts from the peak: the comparator ending the
 *            attack, a sub-2 % correction for any attack over a
 *            millisecond and the whole step for an instant one, where the
 *            ramp itself jumps.
 *   decay    y -> S (or 0 in the short-gate case), e^-k per decay time.
 *            k = 3.6 for the main ADSR (within 2.7 % of the target when
 *            the stage ends), 4.32 for the X-Envs -- the "20 % more"
 *            asked for after listening.
 *   sustain  y keeps converging on S at the decay rate, so a moved
 *            sustain knob is followed through the "decay resistor".
 *   release  y -> 0 at the release rate; stage 4 continues the discharge
 *            at the rate of the stage it came from, so a short-gate note
 *            dies at its decay rate however long release is set.
 *
 * Every stage change is a change of target, not of level, so y never
 * jumps except at the peak.  The stock levels and timers are untouched:
 * save/load, undo and the CV-out envelope mode see nothing new.
 *
 * State lives in DTCM (DIAG+0x180: 32 bytes per envelope, entry 0 the
 * main ADSR, 1..19 the mod slots by owner+0x04, 20 a scratch entry for an
 * out-of-range index; cleared every boot), as do the constants
 * (DIAG+0x140, copied from np_xenv_consts by np_early): DTCM is zero-wait
 * and costs no cache line.  (This used to claim the D-cache was off and a
 * pc-relative literal was an uncached flash read -- WRONG: the stock enables
 * both caches, `SCB_CCR = 0x00030000` written at 0x0810FE62, so a literal is
 * an ordinary D-cache hit.  DTCM is still the better place for a constant
 * read 112 times per audio block, but the margin is a cycle, not ten.)
 * The step runs
 * once per eight-sample block and interpolates between -- see the note at
 * np_env_rc_step -- and the one-pole coefficient 1-e^-z is cached per entry
 * keyed on z, so a decay block is a compare and one FMA and only an attack
 * evaluates the series.  One division per attack block, no table, no heap,
 * no interrupt masking, no vcmp: clamps use vmaxnm/vminnm, so FPSCR's bits
 * are never written (its cumulative bits are unobservable -- the stock
 * never reads FPSCR into a register).  See docs/patches.md (X) and
 * docs/envelope-cost.md.
 */
.syntax unified
.cpu cortex-m7
.fpu fpv5-d16
.thumb
#include "np_diag.h"
.equ DIAG, 0x2000E400
#ifndef NP_EV_LOG
#define NP_EV_LOG 1
#endif
.section .text.np_xenv, "ax", %progbits

/* Event log (np_patch.S np_ev_put_isr): r1 = this envelope's state entry,
 * whose index (0 main ADSR, slot + 1 for an X-Env) is the bit.  Only on
 * the rare paths -- a new attack, a decay restart -- never per sample.
 * np_ev_put_isr touches r0-r3, r12 and no FP register; r4/r5 are ours.
 * This routine runs in the audio handler only, hence the unmasked entry. */
.macro EV_LOG type
#if NP_EV_LOG
    push {r0, r1, r2, r3, r12, lr}
    movw r0, #:lower16:NP_ENV_STATE
    movt r0, #:upper16:NP_ENV_STATE
    subs r1, r1, r0
    lsrs r1, r1, #5
    movs r0, #\type
    bl np_ev_put_isr
    pop {r0, r1, r2, r3, r12, lr}
#endif
.endm

/* DTCM constant block, word offsets; np_xenv_consts below is the master. */
.equ K_LN4,   0        /* ln 4: attack ends at 75 % of its asymptote     */
.equ K_43,    4        /* 1 / (1 - 4^-1)                                 */
.equ K_EPS,   8        /* smallest attack span before u is forced to 1   */
.equ K_1_32,  12       /* series argument scale                          */
.equ K_1_6,   16
.equ K_1_24,  20
.equ K_1_120, 24
.equ K_MAIN,  28       /* main ADSR decay/release constant, 3.6          */
.equ K_XENV,  32       /* X-Env decay/release constant, 4.32             */

/* State entry, 32 bytes. */
.equ E_Y,     0        /* level                                          */
.equ E_FLAGS, 4        /* bits 0..7 stage seen last tick + 1 (0: nothing seen yet,
                          as a cleared entry reads); bit 8 idle-from-decay;
                          bits 29..31 this sample's index within the block */
.equ E_Y0,    8        /* level when the attack began                    */
.equ E_L0,    12       /* stock ramp level when the attack began         */
.equ E_DY,    16       /* level change per sample across this block      */
.equ E_ZKEY,  20       /* z the cached coefficient was computed for      */
.equ E_COEF,  24       /* 1 - e^-z                                       */
.equ E_XLAST, 28       /* decay accumulator (+0x24) seen last decay tick */

/* The block position, bits 29..31: adding one wraps at eight because the
 * carry leaves the word. */
.equ E_TICK,  (1 << 29)

/* 1 - e^-z, z >= 0 (NaN -> 0), monotone and free of cancellation: the
 * series on z/32 (<= 0.5), then five doublings 1-e^-2u = c(2-c).
 * s13 in/out; s10, s11 clobbered; r2 = constant block. */
.macro COEFF
    vmov.f32 s10, #16.0
    vminnm.f32 s13, s13, s10
    vsub.f32 s10, s10, s10
    vmaxnm.f32 s13, s13, s10
    vldr s10, [r2, #K_1_32]
    vmul.f32 s13, s13, s10          /* c = z/32                       */
    vldr s11, [r2, #K_1_24]
    vldr s10, [r2, #K_1_120]
    vfms.f32 s11, s13, s10          /* 1/24 - c/120                   */
    vldr s10, [r2, #K_1_6]
    vfms.f32 s10, s13, s11          /* 1/6 - c(...)                   */
    vmov.f32 s11, #0.5
    vfms.f32 s11, s13, s10
    vmov.f32 s10, #1.0
    vfms.f32 s10, s13, s11
    vmul.f32 s13, s13, s10          /* c(1 - c(1/2 - c(1/6 - ...)))   */
    vmov.f32 s10, #2.0
    vsub.f32 s11, s10, s13
    vmul.f32 s13, s13, s11
    vsub.f32 s11, s10, s13
    vmul.f32 s13, s13, s11
    vsub.f32 s11, s10, s13
    vmul.f32 s13, s13, s11
    vsub.f32 s11, s10, s13
    vmul.f32 s13, s13, s11
    vsub.f32 s11, s10, s13
    vmul.f32 s13, s13, s11
.endm

/* np_env_rc_step: r0 = stage-machine base (fields at +0x0C..+0x40),
 * r1 = state entry, s12 = decay constant k, s15 = this tick's raw level.
 * Returns y in s15 and stores it.  Clobbers r1-r4 (r4 saved here),
 * s8-s11, s13, s14, APSR.  No vcmp, no memory outside the two blocks.
 *
 * Once per block of eight samples, not once per sample.  The DSP renders
 * eight samples per 250 us interrupt and calls this once per sample per
 * envelope: fifteen envelopes, 112 calls, and at 68 instructions each that
 * measured 21 % of the whole audio block -- which is the margin the main
 * loop needs to keep up with the panel, and that shortfall is the UI freeze.
 *
 * So the full step runs on the first sample of a block and solves the whole
 * block: the one-pole coefficient becomes 1 - e^-8z, exactly eight of the
 * per-sample steps, and the change it produces is spread over the eight
 * samples as a straight line.  The other seven are a load, an add and a
 * store.  What that costs is the curvature inside one block -- a chord
 * instead of an arc over 250 us, third order in the step -- and a group
 * delay of one block, the same for every envelope and every stage, so
 * nothing drifts out of phase with anything else.  An envelope fast enough
 * to curve measurably inside 250 us is a click either way.
 *
 * The block position lives in the top three bits of E_FLAGS and the line's
 * slope in E_DY, the word E_INVSP used to hold: 1/(1 - L0) belongs to the
 * attack alone, it is a pure function of E_L0, and the attack now runs eight
 * times less often, so it recomputes it instead of caching it.
 */
.thumb_func
.align 2
.global np_env_rc_step
np_env_rc_step:
    push {r4, r5}
    /* np_eval_linad/np_eval_expad set this when the call they are wrapping
     * stands for a whole eight-sample block: the stage increment has been
     * scaled by eight for them, so this routine must not scale it again,
     * must step on every call rather than one in eight, and has no
     * intra-block line to walk. */
    movw r5, #:lower16:NP_BLOCK_MODE
    movt r5, #:upper16:NP_BLOCK_MODE
    ldr r5, [r5]
    ldr r3, [r1, #E_FLAGS]
    cbnz r5, 12f                    /* a block per call: always step    */
    lsrs r4, r3, #29                /* this sample's index in the block */
    add r3, r3, #E_TICK             /* advance it; eight wraps to zero  */
    str r3, [r1, #E_FLAGS]
    beq 12f                         /* lsrs set Z: zero is the boundary */
    /* Between boundaries, hold -- unless the stage machine moved since
     * the last full step.  A note-on lands wherever the sequencer's
     * interrupt puts it, and waiting for the boundary made a drum's
     * attack sit on the previous note's tail for up to seven samples and
     * then squeeze into one block.  Taking the transition here costs a
     * byte load and a compare on the hold path and gives the stock's own
     * one-sample latency back. */
    ldrb r4, [r0, #0x31]
    adds r4, #1                     /* stage + 1, as E_FLAGS records it */
    uxtb r2, r3
    cmp r4, r2
    bne 12f                         /* the stage moved: take it now     */
    /* ...and a retrigger that never shows a stage change.  With an attack
     * shorter than a tick the stock runs trigger, attack and the switch to
     * decay inside one tick, so a note landing in decay leaves the stage at
     * 1 and only moves the decay accumulator back up (.Ldecay detects it at
     * the next full step).  Waiting for that step held a drum's new note on
     * the old one's falling tail for up to seven samples -- measured 3..6
     * late at mid-block hits, on the main ADSR and the inline X-Envs alike.
     * Non-negative floats order as integers. */
    cmp r4, #2
    bne .Lhold
    ldr r4, [r0, #0x24]
    ldr r2, [r1, #E_XLAST]
    cmp r4, r2
    bls .Lhold                      /* still falling: nothing new       */
12:

    movw r2, #:lower16:NP_XENV_CONSTS
    movt r2, #:upper16:NP_XENV_CONSTS
    vldr s9, [r1, #E_Y]
    vmov.f32 s14, #1.0
    vsub.f32 s13, s14, s14
    vmaxnm.f32 s9, s9, s13          /* cold RAM, NaN -> 0             */
    vminnm.f32 s9, s9, s14
    vmov.f32 s8, s9                 /* the level this block starts at */
    ldrb r4, [r0, #0x31]            /* stage                          */
    uxtb r3, r3                     /* r3 = last stage + 1            */
    cmp r4, #1
    beq .Ldecay                     /* the common cases first; a tbb  */
    cmp r4, #4                      /* would read its table from flash */
    beq .Lidle
    cmp r4, #0
    beq .Lattack
    cmp r4, #2
    beq .Lsustain
    cmp r4, #3
    bne .Lunknown

.Lrelease:
    vldr s13, [r0, #0x14]
    vsub.f32 s14, s13, s13
    movs r3, #4
    b .Lonepole

.Lattack:
    cmp r3, #1
    beq 1f                          /* attack continues               */
    EV_LOG EV_RC_ATTACK
    vldr s13, [r0, #0x0C]           /* (re)trigger: the ramp left     */
    vsub.f32 s13, s15, s13          /* from raw - inc this tick       */
    /* (Re)trigger level: the RC charge continues from where the envelope
     * is now, exactly as the stock's own ramp does and as an analogue
     * ADSR's capacitor does.  p26 forced this to zero to give drums a
     * punchy retrigger, but on the main VCA envelope a jump to zero from
     * a ringing release is a downward CLICK, and it got louder the longer
     * the release -- which is what was heard.  So: never snap.  The
     * drum "not starting from zero" was the 7-sample block-grouping dead
     * time, fixed separately by taking the stage change on its own
     * sample; that fix stays.  y0 is the current level. */
    vstr s9, [r1, #E_Y0]
    vstr s13, [r1, #E_L0]
1:  movs r3, #1
    orr r3, r3, #E_TICK             /* sample zero of this block done  */
    str r3, [r1, #E_FLAGS]
    vldr s13, [r1, #E_L0]
    vmov.f32 s14, #1.0
    vsub.f32 s14, s14, s13          /* span = 1 - L0                  */
    vldr s11, [r2, #K_EPS]
    vmaxnm.f32 s14, s14, s11
    vmov.f32 s11, #1.0
    vdiv.f32 s14, s11, s14          /* 1 / span, once per block       */
    vsub.f32 s13, s15, s13
    /* The one-pole stages are exact at a block boundary -- 1-e^-8z is
     * eight of the per-sample steps -- but the attack is a function of the
     * ramp, not a recurrence, so take the ramp where the block ends: seven
     * more increments, which is the eighth sample.  Without this a 1 ms
     * attack lags by a quarter of itself. */
    vldr s11, [r0, #0x0C]
    vmov.f32 s10, #7.0
    cbz r5, 14f
    /* The increment is scaled by the N samples this call covers, so the
     * end of the stretch is (N-1)/N of it -- np_slot_hold published that;
     * a fixed 0.875 was only right when N was always eight. */
    movw r4, #:lower16:NP_BLOCK_FRAC
    movt r4, #:upper16:NP_BLOCK_FRAC
    vldr s10, [r4]
14: vfma.f32 s13, s11, s10
    vmul.f32 s13, s13, s14          /* u where the block ends         */
    vldr s14, [r2, #K_LN4]
    vmul.f32 s13, s13, s14          /* z = u ln 4                     */
    COEFF
    vldr s14, [r1, #E_Y0]
    vmov.f32 s11, #1.0
    vsub.f32 s11, s11, s14
    vmul.f32 s13, s13, s11          /* (1 - y0)(1 - 4^-u)             */
    vldr s11, [r2, #K_43]
    vfma.f32 s14, s13, s11          /* y0 + ... / (1 - 4^-1)          */
    vmov.f32 s15, s14
    b .Lclamp

.Ldecay:
    /* A retrigger with an attack shorter than a tick never shows stage 0:
     * the stock runs trigger, attack and the switch to decay inside one
     * tick, and the only trace is its decay accumulator +0x24 jumping back
     * up towards 1 (it is strictly decreasing otherwise).  Non-negative
     * floats order as integers, so compare without vcmp.               */
    ldr r4, [r0, #0x24]
    ldr r2, [r1, #E_XLAST]
    str r4, [r1, #E_XLAST]
    cmp r3, #2
    bne 8f                          /* stage change: the peak         */
    cmp r4, r2
    bls 9f                          /* still falling: no retrigger    */
    EV_LOG EV_RC_DECAY              /* a restart seen only as +0x24 rising */
8:  vmov.f32 s9, #1.0               /* the attack ended at the peak   */
9:  movw r2, #:lower16:NP_XENV_CONSTS
    movt r2, #:upper16:NP_XENV_CONSTS
    ldrb r4, [r0, #0x2F]            /* short gate: decay runs to 0    */
    vldr s14, [r0, #0x18]           /* S                              */
    cbz r4, 2f
    vsub.f32 s14, s14, s14
2:  vldr s13, [r0, #0x10]
    movs r3, #2
    b .Lonepole

.Lsustain:
    vldr s14, [r0, #0x18]
    vldr s13, [r0, #0x10]
    movs r3, #3
    b .Lonepole

.Lidle:
    vmov r4, s9
    lsrs r4, r4, #23                /* s9 is clamped >= 0: its exponent */
    cmp r4, #107                    /* below 2^-20 is silence           */
    blo .Lsilent                    /* (exactly 0 lands here too)       */
    vmov r4, s9
    cmp r3, #2                      /* to discharge, flags can stay   */
    beq 3f                          /* straight from decay            */
    cmp r3, #5
    bne 4f
    ldr r4, [r1, #E_FLAGS]
    tst r4, #0x100                  /* idle, and it began from decay  */
    beq 4f
3:  vldr s13, [r0, #0x10]           /* came from decay: its rate      */
    mov r3, #0x105
    b 5f
4:  vldr s13, [r0, #0x14]
    movs r3, #5
5:  vsub.f32 s14, s13, s13

.Lonepole:
    orr r3, r3, #E_TICK             /* sample zero of this block done  */
    str r3, [r1, #E_FLAGS]
    vmov.f32 s8, s9                 /* after the peak snap, if any    */
    vmul.f32 s13, s13, s12          /* z = k inc                      */
    cbnz r5, 13f                    /* inc already covers the block   */
    vmov.f32 s10, #8.0
    vmul.f32 s13, s13, s10          /* eight samples in one step      */
13:
    vmov r4, s13
    ldr r3, [r1, #E_ZKEY]
    cmp r4, r3
    beq 6f
    COEFF
    str r4, [r1, #E_ZKEY]
    vstr s13, [r1, #E_COEF]
    b 7f
6:  vldr s13, [r1, #E_COEF]
7:  vsub.f32 s14, s14, s9           /* target - y                     */
    vfma.f32 s9, s14, s13
    vmov.f32 s15, s9

/* s15 is where the block ends and s8 where it began: keep the slope and
 * return the first of its eight samples. */
.Lclamp:
    vmov.f32 s14, #1.0
    vsub.f32 s13, s14, s14
    vmaxnm.f32 s15, s15, s13
    vminnm.f32 s15, s15, s14
    cbnz r5, 15f                    /* one call, one block: no slope  */
    vsub.f32 s15, s15, s8
    vmov.f32 s14, #0.125
    vmul.f32 s15, s15, s14          /* dy                             */
    vstr s15, [r1, #E_DY]
    vadd.f32 s15, s15, s8
    b .Lstore
15: vsub.f32 s14, s15, s15
    vstr s14, [r1, #E_DY]
    b .Lstore

/* ...and the other seven. */
.Lhold:
    vldr s15, [r1, #E_Y]
    vldr s14, [r1, #E_DY]
    vadd.f32 s15, s15, s14

.Lstore:
    vmov.f32 s14, #1.0
    vsub.f32 s13, s14, s14
    vmaxnm.f32 s15, s15, s13
    vminnm.f32 s15, s15, s14
    vstr s15, [r1, #E_Y]
    pop {r4, r5}
    bx lr

.Lunknown:
    vmov.f32 s14, #1.0
    vsub.f32 s14, s14, s14
    vstr s14, [r1, #E_DY]           /* nothing to interpolate         */
    vstr s15, [r1, #E_Y]            /* adopt the raw level            */
    /* The stage is deliberately NOT recorded here: an unknown stage is a
     * pass-through of this tick's raw level, so every sample has to run. */
    b .Lcount1

.Lsilent:
    vmov.f32 s15, #1.0
    vsub.f32 s15, s15, s15          /* +0.0                           */
    vstr s15, [r1, #E_DY]
    vstr s15, [r1, #E_Y]            /* and it stays there             */
    /* Record idle, so the seven samples after this one take the hold path
     * and add a zero slope to a zero level.  Without it the silent exit
     * left the stage it had seen last, the hold test never matched, and
     * every envelope at rest ran the whole routine every sample: measured
     * 40.9 instructions a call against the hold path's twelve, 1,716 per
     * block with nothing playing. */
    ldr r3, [r1, #E_FLAGS]
    and r3, r3, #0x100              /* keep idle-from-decay           */
    orr r3, r3, #5                  /* idle seen: stage 4 + 1         */
    orr r3, r3, #E_TICK
    str r3, [r1, #E_FLAGS]
    pop {r4, r5}
    bx lr

.Lcount1:                           /* a full step done: sample 0 of a block */
    ldr r3, [r1, #E_FLAGS]
    bic r3, r3, #(7 << 29)
    orr r3, r3, #E_TICK
    str r3, [r1, #E_FLAGS]
    pop {r4, r5}
    bx lr

/* np_env_entry: r1 = owner+0x04 slot index -> r1 = its state entry
 * (1..19; anything else lands on the scratch entry 20).  Clobbers r2. */
.thumb_func
.align 2
.global np_env_entry
np_env_entry:
    cmp r1, #18
    it hi
    movhi r1, #19
    adds r1, #1
    movw r2, #:lower16:NP_ENV_STATE
    movt r2, #:upper16:NP_ENV_STATE
    add.w r1, r2, r1, lsl #5
    bx lr

/* np_env_xenv: r0 = owner+0x1F0, r1 = slot index, s15 = raw -> s15 = y.
 * The X-Env step: entry lookup, k = 4.32, then the common routine. */
.thumb_func
.align 2
.global np_env_xenv
np_env_xenv:
    /* np_env_entry inlined: it is seven instructions and this is the hot
     * path, called once per sample for every X-Env destination. */
    cmp r1, #18
    it hi
    movhi r1, #19
    adds r1, #1
    movw r2, #:lower16:NP_ENV_STATE
    movt r2, #:upper16:NP_ENV_STATE
    add.w r1, r2, r1, lsl #5
    movw r2, #:lower16:NP_XENV_CONSTS
    movt r2, #:upper16:NP_XENV_CONSTS
    vldr s12, [r2, #K_XENV]
    b np_env_rc_step                /* tail call: it returns to our caller */

/* Flash master of the DTCM constant block; np_early copies NP_XENV_NCONSTS
 * words to NP_XENV_CONSTS on every boot. */
.align 2
.global np_xenv_consts
np_xenv_consts:
    .float 1.3862943611198906       /* ln 4                           */
    .float 1.3333333333333333       /* 4/3                            */
    .float 1e-6
    .float 0.03125
    .float 0.16666666666666666
    .float 0.041666666666666664
    .float 0.008333333333333333
    .float 3.6                      /* main ADSR                      */
    .float 4.32                     /* X-Env                          */

/* Eight installed sites.  The step clobbers r0-r3 (r4 it saves), lr
 * through bl, s9-s15 and APSR; a site saves r0-r3, lr, APSR and s8-s14
 * (56-byte frame, eight-byte aligned), s15 being the sample in and out.
 * Original instructions are replayed below.  At the three per-slot
 * evaluator sites r4 is the owner; at the three inlined copies in the
 * DSP block r4 is the engine and the owners sit at +0x48 (OSC1), +0xAAC
 * (OSC2) and +0x17A8 (cutoff).
 */
.macro RC_SAVE
    push {r0, r1, r2, r3, lr}
    mrs r3, apsr
    push {r2, r3}                   /* r2 pads to 8-byte alignment    */
    vpush {s8-s14}
.endm
.macro RC_RESTORE
    vpop {s8-s14}
    pop {r2, r3}
    msr APSR_nzcvq, r3
    pop {r0, r1, r2, r3, lr}
.endm
.macro XENV_STEP owner_off
    RC_SAVE
    mov r0, r4
.if (\owner_off & 0xFF00)
    add.w r0, r0, #(\owner_off & 0xFF00)
.endif
.if (\owner_off & 0xFF)
    add.w r0, r0, #(\owner_off & 0xFF)
.endif
    ldrb r1, [r0, #4]               /* slot index                     */
    add.w r0, r0, #0x1F0            /* the X-Env's stage machine      */
    bl np_env_xenv
    RC_RESTORE
.endm

.thumb_func
.align 2
.global np_xenv_q
np_xenv_q:
    XENV_STEP 0
    vldr s14, [r4, #0x1dc]
    b.w 0x0811C6A2

.thumb_func
.align 2
.global np_xenv_l
np_xenv_l:
    XENV_STEP 0
    ldrb.w r2, [r4, #0x26]
    b.w 0x0811129E

.thumb_func
.align 2
.global np_xenv_e
np_xenv_e:
    XENV_STEP 0
    ldrb.w r2, [r4, #0x26]
    b.w 0x08110ABE

.thumb_func
.align 2
.global np_xenv_osc1
np_xenv_osc1:
    XENV_STEP 0x48
    vadd.f32 s12, s13, s12
    b.w 0x08112722

.thumb_func
.align 2
.global np_xenv_osc2
np_xenv_osc2:
    XENV_STEP 0xAAC
    addw r3, r4, #0xc84
    b.w 0x08112956

.thumb_func
.align 2
.global np_xenv_filter
np_xenv_filter:
    XENV_STEP 0x17A8
    add.w r3, r4, #0x1980
    b.w 0x08112F38

/* OSC1 also uses the previous sample before advancing its envelope.  The
 * stateful equivalent of "the previous raw sample" is the level published
 * last tick: read the entry, no step.  s10 is the destination; its saved
 * slot is overwritten so the vpop delivers it.
 */
.thumb_func
.align 2
.global np_xenv_osc1_previous
np_xenv_osc1_previous:
    vldr s10, [r4, #0x278]
    push {r0, r1, r2, r3, lr}
    mrs r3, apsr
    push {r2, r3}
    vpush {s8-s15}
    ldrb r1, [r4, #0x4C]            /* OSC1 owner's slot index        */
    bl np_env_entry
    vldr s15, [r1, #E_Y]
    vstr s15, [sp, #8]              /* the saved s10                  */
    vpop {s8-s15}
    pop {r2, r3}
    msr APSR_nzcvq, r3
    pop {r0, r1, r2, r3, lr}
    b.w 0x0811255E

/* Main ADSR (engine+0x0C..+0x44), published once per tick at
 * engine+0x2F2C from s17 at 0x08112DE6.  Everything downstream reads that
 * one value: the filter ENV AMOUNT term (vfma s16,s15,s17 at 0x0811319A,
 * register), the VCA level (vldr from +0x2F2C at 0x081131D4, memory) and
 * the CV-out envelope mode.  Replacing s17 here, after the raw level was
 * already stored to +0x40 at 0x08112DA4, therefore reaches all of them
 * consistently while the state machine keeps its linear timing.  Entry 0,
 * k = 3.6.  Only the cbz at 0x08112DA8 lands on this address; nothing
 * branches into the displaced instruction.
 */
.thumb_func
.align 2
.global np_env_main
np_env_main:
    push {r0, r1, r2, r3, lr}
    mrs r3, apsr
    push {r2, r3}
    vpush {s8-s15}
    mov r0, r4
    movw r1, #:lower16:NP_ENV_STATE
    movt r1, #:upper16:NP_ENV_STATE
    movw r2, #:lower16:NP_XENV_CONSTS
    movt r2, #:upper16:NP_XENV_CONSTS
    vldr s12, [r2, #K_MAIN]
    vmov.f32 s15, s17
    bl np_env_rc_step
    /* CH: an instrument switch is fading this note out (np_patch.S).
     * The envelope runs on underneath; what is published is its level at
     * the fade's start, falling in a straight line to 0 over the fade, and
     * on its last sample every envelope is put at rest at 0. */
    ldr r0, =(DIAG + D_CHOKE_N)
    ldr r1, [r0]
    cbz r1, 9f
    ldr r2, [r0, #(D_CHOKE_STARTED - D_CHOKE_N)]
    cbnz r2, 1f
    vstr s15, [r0, #(D_CHOKE_L - D_CHOKE_N)]
    movs r2, #1
    str r2, [r0, #(D_CHOKE_STARTED - D_CHOKE_N)]
1:  subs r1, #1
    str r1, [r0]
    ldr r2, =np_choke_samples
    ldr r2, [r2]
    vmov s13, r2
    vcvt.f32.u32 s13, s13
    vmov s14, r1
    vcvt.f32.u32 s14, s14
    vdiv.f32 s14, s14, s13               /* (n - 1) / N                    */
    vldr s13, [r0, #(D_CHOKE_L - D_CHOKE_N)]
    vmul.f32 s15, s13, s14
    cbnz r1, 9f
    bl np_choke_reset_envs
9:  vmov.f32 s17, s15
    vpop {s8-s15}
    pop {r2, r3}
    msr APSR_nzcvq, r3
    pop {r0, r1, r2, r3, lr}
    addw r3, r5, #0xf2c
    b.w 0x08112DDE
    .ltorg
