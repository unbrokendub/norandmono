/*
 * np_lfo.cpp -- X-Mod LFO fixes, and the fourth X-Mod type.
 *
 * Two defects, see docs/audit.md finding G:
 *
 *  G2  The division index is computed as (unsigned)(value * 15.0f) against a
 *      15 entry table.  A parameter value of exactly 1.0 yields index 15 and
 *      reads one float past the end of the table (0x0815F35C = 7.9e34), so
 *      the rate collapses to ~0 and the LFO stops at the top of its range.
 *
 *   np_mod_tick   replaces the per-slot handler at 0x0811CC70.  The
 *                 sequencer calls it at a pattern wrap, but the external
 *                 MIDI-clock handler also calls it on every F8 pulse.  It
 *                 retriggers only at a real boundary of the selected
 *                 division.
 *
 * The stock rate refresh was initially missed because it is indirect.  Both
 * internal tempo updates and the external F8 estimator call 0x08116C9C,
 * which walks local parameter 5 for all 19 slots; that dispatch reaches the
 * LFOSpeed handler and setRate.  np_mod_tick must not repeat it for every
 * synced slot, and the old main-loop poll is retained only as a no-op ABI
 * entry for already-written diagnostics.
 *
 * Stock used `pos % (uint16_t)division`.  That test is live for MIDI clock,
 * but truncating the fractional triplet/quintuplet divisions makes their
 * retrigger vanish for hundreds of pulses.  The rational boundary test
 * below preserves the intended gate without that drift.
 *
 * H  A fourth X-Mod type: an FM operator ratio.  Stock has three -- free
 *    LFO, synced LFO, audio rate -- and audio rate places the modulator at
 *    an arbitrary pitch that does not move with the keyboard, so an FM
 *    timbre dialled in on one note is a different timbre on the next.
 *
 *    Type 3 is what an FM synthesiser actually offers: the modulator sits
 *    at a fixed *ratio* to Oscillator 1's live pitch.  The rate knob stops
 *    being a frequency and becomes a ratio selector, quantised to 128 exact
 *    values.  Taking the base from the live VCO1 coordinate matters: it
 *    includes pitch bend, glide, parameter locks and tuning, so the ratio
 *    does not fall behind the carrier when any of those moves it.
 *
 *    Both handlers are replaced outright rather than patched in place: the
 *    stock dispatch is `cmp #1 / cmp #2 / cmp #0 / else rate = 0`, and a
 *    fourth arm does not fit between the existing instructions.  Modes 0, 1
 *    and 2 are reproduced from 0x0811BBA8 and 0x0811BD48, with the two
 *    corrections noted at np_xmod_apply.
 *
 * Freestanding: no libc, no libgcc helpers, hard float, calls into the stock
 * image by absolute address.
 */

#include <stdint.h>

/* What X-Mod type 3 hangs its ratio on: 0 the played note, 1 VCO1's live
 * pitch coordinate.  np_patch.S reads the same switch and the two must
 * agree -- the handler and the DSP splice compute the same position, one
 * when a knob moves and one every frame. */
#ifndef NP_XMOD_FOLLOW
#define NP_XMOD_FOLLOW 0
#endif

/* How many detents the rate knob has in ratio mode, and the two tables that
 * give them meaning.  Defined below, declared here because the position
 * calculation needs them and the tables are more readable at the end. */
constexpr unsigned kRatioCount = 128u;
extern "C" const float np_ratio_value[kRatioCount];
extern "C" const float np_ratio_offset[kRatioCount];

namespace {

/* ---- stock entry points (Thumb bit set, they are called indirectly) ---- */
typedef void  (*SetRateFn)(void *lfo, float rate);
typedef void *(*InstanceFn)(void);
typedef void  (*RetrigFn)(void *lfo);

constexpr uintptr_t kSetRate   = 0x08156910u | 1u;  /* void(lfo*, float)    */
constexpr uintptr_t kTransport = 0x08118380u | 1u;  /* transport singleton  */
constexpr uintptr_t kProject   = 0x0811621Cu | 1u;  /* project singleton    */
constexpr uintptr_t kParamValue = 0x08116C64u | 1u; /* float(project*, idx) */
constexpr uintptr_t kRandom     = 0x08147984u | 1u; /* xorshift32, u32      */
typedef uint32_t (*RandomFn)(void);

/* MultiWaveOscFast: the selected waveform at +0xE5, and, for waveform 4,
 * the sample-and-hold value it holds between wraps at +0xE0. */
constexpr unsigned kWaveform  = 0x0E5u;
constexpr unsigned kHeldValue = 0x0E0u;
constexpr uint8_t  kWaveSH    = 4u;
/* Where sample and hold becomes white noise: the top ten of the rate knob's
 * 127 positions.  See the note in np_wave_shape for why this is keyed to the
 * knob and not to the resulting frequency. */
constexpr float    kNoiseKnob = 117.0f / 127.0f;
constexpr float    kRandScale = 2.0f / 4294967296.0f;   /* u32 -> [-1, 1) */

/* Slot 4 of the LFO sub-object's vtable is where the X-Mod type goes.  It
 * is genuinely virtual -- SineOscFast, TriangleOscNaive, SquareOscNaive,
 * SawtoothOscNaive, SAndHOsc and MultiWaveOscFast each put something
 * different there -- and only MultiWaveOscFast's (0x081562C8) stores the
 * mode at lfo+0xE4, which is the owner+0x10C every reader uses.  A mod
 * slot's LFO is a MultiWaveOscFast (its slot 5 is kDefaultRetrig below),
 * but dispatch through the vtable anyway, exactly as the stock handler
 * does at 0x0811BD60. */
constexpr unsigned kSetModeSlot = 4u;

/* Nonzero while np_slot_hold has a held slot's evaluator on the stack.
 * np_patch.S owns the word (D_BLOCKMODE, exported as the absolute symbol
 * NP_BLOCK_MODE); np_xenv.S reads the same one.  Bound to the symbol, not a
 * copied offset: a copied 0x13E0 went stale when the hold table changed
 * shape and silently left held LFOs at an eighth of their rate. */
extern "C" volatile uint32_t NP_BLOCK_MODE;

typedef void  (*SetModeFn)(void *lfo, unsigned mode);
typedef float (*ParamValueFn)(void *project, unsigned index);

/* vtable slot 5 of the LFO sub-object; anything else is an override and
 * must be called instead of doing the reset inline, exactly as the stock
 * handler does at 0x0811CCD6. */
constexpr uintptr_t kDefaultRetrig = 0x0811BA3Du;

/* Eleven sync divisions.  Ten come from the stock table of fifteen at
 * 0x0815F320; dropped by choice: 2/1, 1/4 dotted, the 1/16 quintuplet, the
 * 1/32 triplet and 1/64 -- the extremes nobody reached for and the two
 * awkward ones.  The eleventh, 1/8 dotted, is not in the stock table at all;
 * it is added here.  In knob order:
 *
 *     1/1.  1/1  1/2.  1/2  1/4  1/8.  1/4T  1/8  1/8T  1/16  1/32
 *
 * eleven detents of 9.1 % each instead of fifteen of 6.7 %, and every one
 * of them has a whole number of MIDI pulses per cycle, so the fractional
 * realignment case is gone.
 *
 * The values are engine ticks per cycle (a quarter note is 1024).  The ten
 * stock ones are the same floats as the stock table, bit for bit: 2048/3
 * and 1024/3 round to exactly the stock 682.6667 and 341.3333.
 *
 * 1/8 dotted sits between 1/4 and 1/4T, so the centre of each of the old
 * ten 10 % detents still selects the division it did; only positions near
 * the edges of 1/4 and 1/4T move to it. */
constexpr unsigned kDivCount = 11u;
const float kDivTicks[kDivCount] = {
    6144.0f, 4096.0f, 3072.0f, 2048.0f, 1024.0f, 768.0f,
    2048.0f / 3.0f, 512.0f, 1024.0f / 3.0f, 256.0f, 128.0f,
};

/* Where a division's boundaries fall, counted in MIDI clock pulses.
 *
 * The F8 handler at 0x08118B00 keeps a double accumulator at transport+0x18,
 * truncates it to u32 and passes that as `pos`, then adds 42.666666... to
 * it (the literal at 0x08118D68 is exactly 128/3).  So one quarter note is
 * 24 * 128/3 = 1024 engine ticks, and each period is division / (128/3).
 * All eleven are whole numbers; 1/8 dotted is 768 ticks, 18 pulses. */
const uint16_t kPulseNumerator[kDivCount] = {
    144u, 96u, 72u, 48u, 24u, 18u, 16u, 12u, 8u, 6u, 3u,
};

/* The accumulator advances by 128/3 per pulse and `pos` is its floor, so
 * the pulse index comes back exactly: pos is within one of n * 128/3, and
 * 3/128 is far smaller than half a unit. */
constexpr uint32_t kPulseAdvNum = 128u;
constexpr uint32_t kPulseAdvDen = 3u;

/* transport: float tick rate used as the numerator of the rate expression */
constexpr unsigned kTransportRate = 0x04u;

constexpr unsigned kSlotOwner  = 0x08u;   /* slot -> owner object          */

/* fields of the owner object */
constexpr unsigned kMode    = 0x10Cu;     /* uint8, 1 = synced             */
constexpr unsigned kValue   = 0x184u;     /* float, 0..1 division selector  */
/* LFOTimeCParam sits at owner+0x198, so its value is at owner+0x1A4.  That
 * parameter *is* the X-Mod type -- it is what the UI edits and what the
 * evaluator recomputes -- so it is the authoritative source.  The mode byte
 * is not: np_xmod_time deliberately clamps it to 2 for the ratio type. */
constexpr unsigned kTypeValue = 0x1A4u;   /* float, type as n / 65536       */
constexpr unsigned kDelta   = 0x190u;     /* float, queued encoder delta    */
constexpr unsigned kPending = 0x194u;     /* uint32, queued delta count     */
constexpr unsigned kLfo     = 0x28u;      /* sub-object setRate operates on */
constexpr unsigned kFlagA   = 0x114u;
constexpr unsigned kFlagB   = 0x074u;

/* 257 floats each, indexed by position * 256.
 *   kSlowTable  free-running LFO rates
 *   kFreqTable  audio rates: entry i is MIDI note i * 127/256, so entry 0 is
 *               8.1758 Hz (MIDI 0) and entry 256 is 12543.85 Hz (MIDI 127).
 *               A position of 1/127 is therefore exactly one semitone. */
const float *const kSlowTable = reinterpret_cast<const float *>(0x08172F94u);
const float *const kFreqTable = reinterpret_cast<const float *>(0x0817379Cu);
constexpr unsigned kTableSteps = 256u;

/* X-Mod types, as stored in the LFOTime parameter (value = n / 65536) and
 * mirrored into owner+0x10C by the LFO's own setMode. */
enum : unsigned { kTypeFree = 0u, kTypeSync = 1u, kTypeAudio = 2u,
                  kTypeAudioKey = 3u };

/* owner+0x25 tells the per-block DSP loop at 0x08112C70 which slots to
 * recompute.  Stock writes 0 or 1; 2 is ours and means "and add the note". */
enum : uint8_t { kAudioOff = 0u, kAudioFixed = 1u, kAudioKeyed = 2u };

constexpr unsigned kSlotId  = 0x004u;     /* uint8, which of the 19 dests   */
constexpr unsigned kAudioOn = 0x025u;     /* uint8, see kAudio* above       */
constexpr unsigned kValueOf = 0x00Cu;     /* CParam: float value            */
constexpr unsigned kOwnerOf = 0x008u;     /* CParam: owning slot            */

/* project fields, all floats */
constexpr unsigned kPitchSlot0 = 0x2F60u; /* read by the slot 0 branch      */
constexpr unsigned kPitchOther = 0x2F20u; /* live VCO1 pitch coordinate     */
/* The played note lives in a pair of fields, one per oscillator.  Which of
 * the two receives an incoming note depends on the path the note handler at
 * 0x081170E8 takes: with nothing held both get it, under legato 0x5FCC keeps
 * the previous note and 0x5FD0 takes the new one, and on the slide path the
 * two are swapped.  The evaluator reads 0x5FD0 for VCO2Tune and 0x5FCC for
 * everything else (`cmp r1,#4 / beq` at 0x08115864), so during legato the
 * oscillators really do hold different notes.
 *
 * Reading one field alone is therefore wrong on half the transitions.  When
 * the two agree the answer is unambiguous; when they disagree the note that
 * actually just played is the one np_note_hook recorded.  See np_patch.S. */
constexpr unsigned kNoteVco1  = 0x5FCCu;  /* VCO1's note slot               */
constexpr unsigned kNoteVco2  = 0x5FD0u;  /* VCO2's note slot               */
constexpr unsigned kTranspose = 0x6004u;  /* added to the note before use   */
const float *const kNoteHeld = reinterpret_cast<const float *>(0x2000E4B4u);

/* No reference note is subtracted anywhere in ratio mode.  A position of
 * note/127 is exactly that MIDI note in the frequency table -- the table
 * spans MIDI 0..127 over its 256 steps -- so ratio 1.0 lands on the played
 * note and every other entry is an exact interval from it.  An anchor
 * would be a constant offset in position, which is the one thing that
 * would stop 1.0 from meaning unison.
 *
 * The 1/126 is for the other base: VCO1's live coordinate is 126 *
 * position, so dividing by 126 reconstructs the position exactly. */
constexpr float kSemitone = 1.0f / 127.0f;
constexpr float kPitchDiv = 1.0f / 126.0f;

constexpr float kPhaseStart = -0.5f;
const unsigned kPhase[6] = { 0x34u, 0x58u, 0x84u, 0xA8u, 0xCCu, 0xF0u };

/* ---- raw field access ------------------------------------------------- */
inline uint8_t  &u8 (void *p, unsigned o) { return *reinterpret_cast<uint8_t  *>(reinterpret_cast<char *>(p) + o); }
inline uint32_t &u32(void *p, unsigned o) { return *reinterpret_cast<uint32_t *>(reinterpret_cast<char *>(p) + o); }
inline float    &f32(void *p, unsigned o) { return *reinterpret_cast<float    *>(reinterpret_cast<char *>(p) + o); }
inline void    *&pp (void *p, unsigned o) { return *reinterpret_cast<void    **>(reinterpret_cast<char *>(p) + o); }
inline void     *lfo_of(void *owner)      { return reinterpret_cast<char *>(owner) + kLfo; }

/* Apply any encoder delta the UI queued, exactly as the stock handler does
 * at 0x0811CC7C, then return the division for the resulting index.
 * Returns 0 when the slot is not in synced mode. */
inline float division_of(void *owner, bool consume_delta,
                         unsigned *selected_index = nullptr)
{
    if (u8(owner, kMode) != 1u)
        return 0.0f;

    if (consume_delta && u32(owner, kPending) != 0u) {
        f32(owner, kValue) += f32(owner, kDelta);
        u32(owner, kPending) -= 1u;
    }

    float v = f32(owner, kValue);
    /* G2: the stock code lets an index of 15 through and reads past the
     * table.  Clamp instead. */
    if (!(v > 0.0f))
        v = 0.0f;
    unsigned idx = static_cast<unsigned>(v * static_cast<float>(kDivCount));
    if (idx >= kDivCount)
        idx = kDivCount - 1u;
    if (selected_index != nullptr)
        *selected_index = idx;

    return kDivTicks[idx];
}

inline bool division_boundary(uint32_t pos, unsigned idx)
{
    /* Do not spell this as (pos * 3 + 64) / 128.  `pos` is a cumulative
     * transport position and that product wraps after 33,554,432 MIDI
     * clocks (about eight days at 120 BPM), making every division jump to
     * the wrong phase.  Split quotient and remainder so every intermediate
     * remains in range for the full uint32_t lifetime of the transport. */
    const uint32_t pulse =
        (pos / kPulseAdvNum) * kPulseAdvDen +
        ((pos % kPulseAdvNum) * kPulseAdvDen + kPulseAdvNum / 2u) /
            kPulseAdvNum;
    /* Fractions are reduced: gcd(24,5) and gcd(3,2) are both one.  Their
     * common-grid condition therefore reduces to pulse % numerator == 0;
     * retaining the denominator would only compile another UDIV into this
     * 24-PPQN hot path. */
    return pulse % kPulseNumerator[idx] == 0u;
}

inline float transport_rate()
{
    void *t = reinterpret_cast<InstanceFn>(kTransport)();
    return f32(t, kTransportRate);
}

inline void *project()
{
    return reinterpret_cast<InstanceFn>(kProject)();
}

/* Interpolated read of one of the 257-entry tables, position in 0..1.
 *
 * Stock clamps by branching on the sign and on a compare against 1.0, which
 * for a position of exactly 1.0 falls into the interpolating path and reads
 * entry 257 with a zero weight.  Reaching one entry past the table is
 * harmless there but pointless, so the ends are returned directly.  A NaN
 * position fails `p > 0` and yields entry 0, matching what the stock
 * VCVT produces for one. */
inline float table_at(const float *tbl, float p)
{
    if (!(p > 0.0f))
        return tbl[0];
    if (p >= 1.0f)
        return tbl[kTableSteps];

    float x = p * static_cast<float>(kTableSteps);
    unsigned i = static_cast<unsigned>(x);
    float frac = x - static_cast<float>(i);
    return tbl[i] + (tbl[i + 1u] - tbl[i]) * frac;
}

/* Position of an audio-rate modulator in the frequency table.
 *
 * The two pitch terms are reproduced from the stock handlers even though
 * both fields are dead in this firmware -- nothing but the zero-initialiser
 * at 0x08147612 ever writes project+0x2F20 or project+0x2F60.  They cost
 * one load and keep the handler equivalent if a later revision starts
 * filling them in.  See the withdrawn G3 note in tools/patch.py. */
/* Which ratio the rate knob is pointing at.  The knob is a normalised
 * parameter, so 64 detents is `value * 64` with the top one clamped; a
 * value of exactly 1.0 would otherwise index one past the table. */
inline unsigned ratio_index(float rate)
{
    if (!(rate > 0.0f))
        return 0u;
    unsigned i = static_cast<unsigned>(rate * static_cast<float>(kRatioCount));
    return i >= kRatioCount ? kRatioCount - 1u : i;
}

inline float audio_position(void *owner, float rate, bool ratio_mode)
{
    void *pr = project();

    /* Type 3: the ratio is taken against the played note and nothing else.
     *
     * The stock audio-rate path hangs the modulator off VCO1's live pitch
     * coordinate, and so did an earlier revision of this mode.  That is the
     * right answer when VCO1 is the carrier -- it brings glide, pitch bend
     * and parameter locks with it -- and the wrong one the moment it is
     * not.  Silence VCO1 and put the FM on VCO2, which is an ordinary way
     * to use it, and turning VCO1's tune knob drags the modulator around
     * for no reason the player can see.
     *
     * Following the note instead makes the mode self-contained: nothing
     * but the keyboard, the transpose and the ratio knob moves it.  The
     * cost is that it does not follow a glide -- the note field holds the
     * endpoints, not the sweep -- and that it ignores the carrier's own
     * tuning, so a detuned VCO2 shifts what "ratio 1" means against it.
     *
     * NP_XMOD_FOLLOW=1 restores the VCO1 behaviour; see tools/patch.py. */
#if NP_XMOD_FOLLOW
    if (ratio_mode)
        return np_ratio_offset[ratio_index(rate)]
             + f32(pr, kPitchOther) * kPitchDiv;
#else
    if (ratio_mode) {
        float a = f32(pr, kNoteVco1);
        float note = (a == f32(pr, kNoteVco2)) ? a : *kNoteHeld;
        return np_ratio_offset[ratio_index(rate)]
             + (note + f32(pr, kTranspose)) * kSemitone;
    }
#endif

    /* Type 2, reproduced from the stock handlers. */
    float p = rate - 0.5f;
    if (u8(owner, kSlotId) == 0u)
        p += reinterpret_cast<ParamValueFn>(kParamValue)(pr, 0u)
           + f32(pr, kPitchSlot0) * kPitchDiv;
    else
        p += f32(pr, kPitchOther) * kPitchDiv;
    return p;
}

/* The body shared by both X-Mod handlers.  `mode` is the X-Mod type and
 * `self` the CParam whose value changed; the rate always comes from the
 * slot's LFOSpeed parameter at owner+0x184, never from `self`.
 *
 * Two deliberate differences from the stock code:
 *
 *   - Stock LFOTimeCParam::vfn2 computes its audio-rate position from
 *     `self->value`, which for that class is the X-Mod *type* (0, 1, 2
 *     stored as n/65536), not the rate -- so the position it lands on is
 *     ~ -0.5 whatever the knob says.  It is invisible because the per-block
 *     loop at 0x08112C70 recomputes the rate correctly a moment later.
 *     Using the rate here makes the two agree.
 *   - The synced division index is clamped to the table (finding G2).
 */
void xmod_apply(void *self, unsigned mode)
{
    void *owner = pp(self, kOwnerOf);
    float rate = f32(owner, kValue);        /* owner+0x184 = LFOSpeed value */
    float out;

    switch (mode) {
    case kTypeFree:
        u8(owner, kAudioOn) = kAudioOff;
        out = table_at(kSlowTable, rate);
        break;

    case kTypeSync: {
        u8(owner, kAudioOn) = kAudioOff;
        float v = rate;
        if (!(v > 0.0f))
            v = 0.0f;
        unsigned idx = static_cast<unsigned>(v * static_cast<float>(kDivCount));
        if (idx >= kDivCount)
            idx = kDivCount - 1u;
        /* through kDivTicks, exactly as division_of does -- indexing the
         * stock table directly here would make the parameter handler and
         * the clock tick disagree about what a knob position means */
        out = transport_rate() / kDivTicks[idx];
        break;
    }

    case kTypeAudio:
    case kTypeAudioKey: {
        bool keyed = (mode == kTypeAudioKey);
        u8(owner, kAudioOn) = keyed ? kAudioKeyed : kAudioFixed;
        out = table_at(kFreqTable, audio_position(owner, rate, keyed));
        break;
    }

    default:
        /* setMode rejects malformed values and leaves the old mode byte in
         * place.  Do not leave the old audio-rate marker armed in that case:
         * the per-block DSP loop would otherwise keep applying the prior
         * audio-rate calculation until a valid mode arrived. */
        u8(owner, kAudioOn) = kAudioOff;
        out = 0.0f;
        break;
    }

    reinterpret_cast<SetRateFn>(kSetRate)(lfo_of(owner), out);
}

} /* namespace */

/* The ratio each detent selects, in two banks of 64.
 *
 * Detents 0..63 are 0.25 to 16.00 in quarters -- the Dirtywave M8 grid.
 * Every one is a rational p/4, so every one is harmonic: the sidebands all
 * land on multiples of carrier/4, carrier/2 or the carrier itself, and the
 * tone keeps a definite pitch.  This is the half that does basses, keys,
 * plucks and anything that has to sit in a chord.
 *
 * Detents 64..127 are the irrational columns of the Yamaha four-operator
 * coarse set -- n * m for m in {sqrt2, pi/2, sqrt3} -- sorted.  n runs in
 * halves from 0.5 upward, the first sixty-four of the sorted grid, topping
 * out at 12*sqrt2 = 16.97.
 *
 * Halving the step rather than extending n upward is deliberate.  Carrying
 * the old whole-number n up to forty-eight entries would have reached 24.04,
 * and since `position` is clamped to [0,1] that flattens out above MIDI 72;
 * the half grid keeps the two banks balanced instead: bank A tops at 16.00
 * and flattens above MIDI 79.0, this one tops at 16.97 and flattens above
 * MIDI 78.0, so neither is the weak link.  A quarter grid would have been
 * denser still -- sixty entries at or below ratio 8 rather than thirty --
 * but it caps at 8.49 and gives up the bright metallic half entirely.
 * These are the ratios a quarter grid cannot express at all: with no
 * rational relation between carrier and modulator the reflected sidebands
 * never coincide with the upright ones, there is no common divisor and so
 * no fundamental.  That is what a bell, a gong or struck metal is.  The
 * harmonic column of the Yamaha set is left out because the first bank
 * already covers it, and the top of this bank now reaches 24.04, which
 * clamps against the top of the frequency table above MIDI 72.
 *
 * The table is therefore NOT monotonic: detent 63 is 16.00 and detent 64 is
 * 0.7071.  Turning past the middle of the knob drops the pitch and changes
 * the character.  That is the point of it, not a mistake. */
extern "C" const float np_ratio_value[kRatioCount] = {
    0.25f,         0.5f,          0.75f,         1.0f, /* 0.25, 0.5, 0.75, 1 */
    1.25f,         1.5f,          1.75f,         2.0f, /* 1.25, 1.5, 1.75, 2 */
    2.25f,         2.5f,          2.75f,         3.0f, /* 2.25, 2.5, 2.75, 3 */
    3.25f,         3.5f,          3.75f,         4.0f, /* 3.25, 3.5, 3.75, 4 */
    4.25f,         4.5f,          4.75f,         5.0f, /* 4.25, 4.5, 4.75, 5 */
    5.25f,         5.5f,          5.75f,         6.0f, /* 5.25, 5.5, 5.75, 6 */
    6.25f,         6.5f,          6.75f,         7.0f, /* 6.25, 6.5, 6.75, 7 */
    7.25f,         7.5f,          7.75f,         8.0f, /* 7.25, 7.5, 7.75, 8 */
    8.25f,         8.5f,          8.75f,         9.0f, /* 8.25, 8.5, 8.75, 9 */
    9.25f,         9.5f,          9.75f,         10.0f, /* 9.25, 9.5, 9.75, 10 */
    10.25f,        10.5f,         10.75f,        11.0f, /* 10.25, 10.5, 10.75, 11 */
    11.25f,        11.5f,         11.75f,        12.0f, /* 11.25, 11.5, 11.75, 12 */
    12.25f,        12.5f,         12.75f,        13.0f, /* 12.25, 12.5, 12.75, 13 */
    13.25f,        13.5f,         13.75f,        14.0f, /* 13.25, 13.5, 13.75, 14 */
    14.25f,        14.5f,         14.75f,        15.0f, /* 14.25, 14.5, 14.75, 15 */
    15.25f,        15.5f,         15.75f,        16.0f, /* 15.25, 15.5, 15.75, 16 */
    0.707106769f,  0.785398185f,  0.866025388f,  1.41421354f, /* 0.5*sqrt2, 0.5*pi/2, 0.5*sqrt3, 1*sqrt2 */
    1.57079637f,   1.73205078f,   2.12132025f,   2.3561945f, /* 1*pi/2, 1*sqrt3, 1.5*sqrt2, 1.5*pi/2 */
    2.59807611f,   2.82842708f,   3.14159274f,   3.46410155f, /* 1.5*sqrt3, 2*sqrt2, 2*pi/2, 2*sqrt3 */
    3.53553391f,   3.92699075f,   4.2426405f,    4.33012724f, /* 2.5*sqrt2, 2.5*pi/2, 3*sqrt2, 2.5*sqrt3 */
    4.71238899f,   4.94974756f,   5.19615221f,   5.497787f, /* 3*pi/2, 3.5*sqrt2, 3*sqrt3, 3.5*pi/2 */
    5.65685415f,   6.06217766f,   6.28318548f,   6.36396122f, /* 4*sqrt2, 3.5*sqrt3, 4*pi/2, 4.5*sqrt2 */
    6.92820311f,   7.06858349f,   7.07106781f,   7.7781744f, /* 4*sqrt3, 4.5*pi/2, 5*sqrt2, 5.5*sqrt2 */
    7.79422855f,   7.85398149f,   8.48528099f,   8.6393795f, /* 4.5*sqrt3, 5*pi/2, 6*sqrt2, 5.5*pi/2 */
    8.66025448f,   9.19238853f,   9.42477798f,   9.52627945f, /* 5*sqrt3, 6.5*sqrt2, 6*pi/2, 5.5*sqrt3 */
    9.89949512f,   10.2101765f,   10.3923044f,   10.6066017f, /* 7*sqrt2, 6.5*pi/2, 6*sqrt3, 7.5*sqrt2 */
    10.995574f,    11.2583303f,   11.3137083f,   11.7809725f, /* 7*pi/2, 6.5*sqrt3, 8*sqrt2, 7.5*pi/2 */
    12.0208149f,   12.1243553f,   12.566371f,    12.7279224f, /* 8.5*sqrt2, 7*sqrt3, 8*pi/2, 9*sqrt2 */
    12.9903812f,   13.3517685f,   13.435029f,    13.8564062f, /* 7.5*sqrt3, 8.5*pi/2, 9.5*sqrt2, 8*sqrt3 */
    14.137167f,    14.1421356f,   14.7224321f,   14.8492422f, /* 9*pi/2, 10*sqrt2, 8.5*sqrt3, 10.5*sqrt2 */
    14.9225655f,   15.5563488f,   15.5884571f,   15.707963f, /* 9.5*pi/2, 11*sqrt2, 9*sqrt3, 10*pi/2 */
    16.2634563f,   16.454483f,    16.4933605f,   16.970562f, /* 11.5*sqrt2, 9.5*sqrt3, 10.5*pi/2, 12*sqrt2 */
};

/* The same ratios as table positions: 12 * log2(R) / 127.  Precomputed --
 * there is no logf in a freestanding build, and an FM ratio is worth
 * nothing unless it is exact.  tests/test_patch.py checks every entry
 * against its ratio, and both banks against their own construction. */
extern "C" const float np_ratio_offset[kRatioCount] = {
    -0.188976377f, -0.0944881886f, -0.0392161421f, 0.0f, /* 0.25, 0.5, 0.75, 1 */
    0.0304184034f, 0.0552720465f, 0.0762855038f, 0.0944881886f, /* 1.25, 1.5, 1.75, 2 */
    0.110544093f,  0.124906592f,  0.137899056f,  0.149760231f, /* 2.25, 2.5, 2.75, 3 */
    0.160671473f,  0.1707737f,    0.180178642f,  0.188976377f, /* 3.25, 3.5, 3.75, 4 */
    0.197240576f,  0.205032289f,  0.212402597f,  0.219394773f, /* 4.25, 4.5, 4.75, 5 */
    0.226045743f,  0.232387245f,  0.238446802f,  0.24424842f, /* 5.25, 5.5, 5.75, 6 */
    0.249813184f,  0.255159646f,  0.260304332f,  0.265261889f, /* 6.25, 6.5, 6.75, 7 */
    0.270045459f,  0.274666816f,  0.279136658f,  0.283464581f, /* 7.25, 7.5, 7.75, 8 */
    0.287659287f,  0.291728765f,  0.295680285f,  0.299520463f, /* 8.25, 8.5, 8.75, 9 */
    0.303255439f,  0.306890786f,  0.310431719f,  0.313882977f, /* 9.25, 9.5, 9.75, 10 */
    0.317249f,     0.320533931f,  0.323741555f,  0.326875418f, /* 10.25, 10.5, 10.75, 11 */
    0.329938889f,  0.332934976f,  0.33586666f,   0.338736624f, /* 11.25, 11.5, 11.75, 12 */
    0.3415474f,    0.344301373f,  0.347000808f,  0.34964785f, /* 12.25, 12.5, 12.75, 13 */
    0.352244467f,  0.354792506f,  0.357293844f,  0.359750062f, /* 13.25, 13.5, 13.75, 14 */
    0.362162828f,  0.364533633f,  0.366863906f,  0.36915502f, /* 14.25, 14.5, 14.75, 15 */
    0.371408254f,  0.373624861f,  0.375805974f,  0.377952754f, /* 15.25, 15.5, 15.75, 16 */
    -0.0472440943f, -0.0329294987f, -0.0196080711f, 0.0472440943f, /* 0.5*sqrt2, 0.5*pi/2, 0.5*sqrt3, 1*sqrt2 */
    0.0615586899f, 0.0748801157f, 0.102516145f,  0.116830736f, /* 1*pi/2, 1*sqrt3, 1.5*sqrt2, 1.5*pi/2 */
    0.130152166f,  0.14173229f,   0.156046882f,  0.169368312f, /* 1.5*sqrt3, 2*sqrt2, 2*pi/2, 2*sqrt3 */
    0.172150686f,  0.186465278f,  0.197004333f,  0.199786708f, /* 2.5*sqrt2, 2.5*pi/2, 3*sqrt2, 2.5*sqrt3 */
    0.211318925f,  0.218017787f,  0.224640355f,  0.232332379f, /* 3*pi/2, 3.5*sqrt2, 3*sqrt3, 3.5*pi/2 */
    0.236220479f,  0.245653808f,  0.250535071f,  0.252276391f, /* 4*sqrt2, 3.5*sqrt3, 4*pi/2, 4.5*sqrt2 */
    0.2638565f,    0.266590983f,  0.266638875f,  0.279631346f, /* 4*sqrt3, 4.5*pi/2, 5*sqrt2, 5.5*sqrt2 */
    0.279912412f,  0.280953467f,  0.291492522f,  0.293945938f, /* 4.5*sqrt3, 5*pi/2, 6*sqrt2, 5.5*pi/2 */
    0.294274896f,  0.302403748f,  0.305807114f,  0.307267368f, /* 5*sqrt3, 6.5*sqrt2, 6*pi/2, 5.5*sqrt3 */
    0.31250599f,   0.31671834f,   0.319128543f,  0.321910918f, /* 7*sqrt2, 6.5*pi/2, 6*sqrt3, 7.5*sqrt2 */
    0.326820582f,  0.330039769f,  0.330708653f,  0.33622551f, /* 7*pi/2, 6.5*sqrt3, 8*sqrt2, 7.5*pi/2 */
    0.338972867f,  0.340142012f,  0.345023245f,  0.346764565f, /* 8.5*sqrt2, 7*sqrt3, 8*pi/2, 9*sqrt2 */
    0.349546939f,  0.353287458f,  0.354134887f,  0.358344674f, /* 7.5*sqrt3, 8.5*pi/2, 9.5*sqrt2, 8*sqrt3 */
    0.361079156f,  0.361127079f,  0.366608888f,  0.367778033f, /* 9*pi/2, 10*sqrt2, 8.5*sqrt3, 10.5*sqrt2 */
    0.368449479f,  0.37411952f,   0.374400586f,  0.37544167f, /* 9.5*pi/2, 11*sqrt2, 9*sqrt3, 10*pi/2 */
    0.380179077f,  0.381770909f,  0.382092625f,  0.385980695f, /* 11.5*sqrt2, 9.5*sqrt3, 10.5*pi/2, 12*sqrt2 */
};

extern "C" void np_mod_tick(void *slot, uint32_t pos)
{
    void *owner = pp(slot, kSlotOwner);
    unsigned division_index = 0u;
    /* Preserve CParam's stock ramp semantics: one queued interpolation step
     * per invocation.  This hook is reached once per external MIDI F8, i.e.
     * 24 times per quarter note.  Deferring the step to a musical boundary
     * makes the control response depend on the selected division and can
     * delay one step by 192 clocks (eight quarter notes at 2/1). */
    float division = division_of(owner, true, &division_index);
    if (division <= 0.0f)
        return;                       /* not a synced slot */

    const bool boundary = division_boundary(pos, division_index);

    /* MIDI clock reaches this function on every F8.  Rate refresh happens
     * later in the stock F8 path through the indirect local-5 parameter
     * walker; only phase restart belongs here. */
    if (!boundary)
        return;

    /* original phase reset, including the override dispatch */
    void *lfo = lfo_of(owner);
    void *sub = pp(lfo, 0u);
    void *slot5 = pp(sub, 5u * sizeof(void *));
    if (reinterpret_cast<uintptr_t>(slot5) != kDefaultRetrig) {
        reinterpret_cast<RetrigFn>(slot5)(lfo);
        return;
    }

    for (unsigned i = 0u; i < 6u; ++i)
        f32(owner, kPhase[i]) = kPhaseStart;
    u8(owner, kFlagA) = 1u;
    u8(owner, kFlagB) = 1u;

    /* Sample and hold picks a new value only where its phase wraps past
     * +0.5.  Resetting the phase to -0.5 on the musical boundary takes that
     * wrap away: when the rate is one cycle per division -- which is the
     * whole point of syncing -- the reset and the wrap fall on the same
     * moment, the reset wins, and the held value never changes again.  That
     * is a synced S&H sitting silent on one level.
     *
     * Stepping it here is also what it should do musically: the new value
     * lands on the division, not at whatever phase the oscillator drifted
     * to.  Same generator and same scaling as the stock wrap handler at
     * 0x08156778, so the two are indistinguishable apart from timing. */
    if (u8(lfo, kWaveform) == kWaveSH) {
        uint32_t r = reinterpret_cast<RandomFn>(kRandom)();
        f32(lfo, kHeldValue) = static_cast<float>(r) * kRandScale - 1.0f;
    }
}

/* Compatibility entry retained in the NPAT header.  An older revision ran
 * an extra rate poll from every main-loop iteration after overlooking the
 * stock 0x08116C9C indirect parameter walk. */
extern "C" void np_lfo_poll(void)
{
}

/* ------------------------------------------------------------------------
 * Replaces monosynth::LFOTimeCParam::vfn2 at 0x0811BD48 -- the X-Mod TYPE
 * parameter.  Its value carries the type as n/65536; the wrap count for
 * this parameter lives at 0x0815E8D6 and the patch tool raises it from 3
 * to 4 so the encoder can reach type 3.
 * --------------------------------------------------------------------- */
extern "C" void np_xmod_time(void *self)
{
    void *owner = pp(self, kOwnerOf);
    unsigned mode =
        static_cast<unsigned>(f32(self, kValueOf) * 65536.0f) & 0xFFu;

    /* Tell the LFO core -- but never that the mode is 3.
     *
     * owner+0x10C is read in five places across the image (the evaluator's
     * two audio-rate tests on the local-5 path, LFOSpeedCParam's dispatch,
     * the sync retrigger's `== 1`, and the per-slot tick).  Stock only ever
     * stores 0, 1 or 2 there, and the stock LFOTime handler does not write
     * it at all.  Publishing a fourth value puts a number no stock consumer
     * was written against into a field they all read.
     *
     * So the ratio type presents itself to every one of them as an ordinary
     * audio-rate slot, and the distinction lives only in owner+0x25 -- a
     * byte whose sole reader is the per-block DSP loop, which is ours.
     * np_xmod_speed recovers the real type from the pair. */
    void *lfo = lfo_of(owner);
    void *vt = pp(lfo, 0u);
    const unsigned published = (mode == kTypeAudioKey) ? kTypeAudio : mode;
    reinterpret_cast<SetModeFn>(
        pp(vt, kSetModeSlot * sizeof(void *)))(lfo, published);

    /* stock advances the rate parameter's ramp here, once per call */
    if (u32(owner, kPending) != 0u) {
        u32(owner, kPending) -= 1u;
        f32(owner, kValue) += f32(owner, kDelta);
    }

    xmod_apply(self, mode);
}

/* ------------------------------------------------------------------------
 * Replaces monosynth::LFOSpeedCParam::vfn2 at 0x0811BBA8 -- the X-Mod RATE
 * parameter.  Same body, but the type is read back from where the LFO core
 * stored it rather than decoded from this parameter's value.
 * --------------------------------------------------------------------- */
extern "C" void np_xmod_speed(void *self)
{
    void *owner = pp(self, kOwnerOf);
    /* Read the type from the type parameter, exactly as np_xmod_time does.
     *
     * Deriving it from owner+0x10C and owner+0x25 instead -- the mode byte
     * says "audio", the marker says "and it is the ratio one" -- looks
     * equivalent and is not.  It is a one-way latch: the marker is only ever
     * written once the type has already been recognised, so a single moment
     * where the pair is inconsistent collapses the slot to plain audio rate
     * with no way back.  A slot that falls back that way puts its modulator
     * at `knob - 0.5 + VCO1 pitch`, which for a low knob is a slow LFO, and
     * an envelope on its depth then reads as a pitch envelope rather than as
     * FM.  Taking the value from the parameter that holds it has no such
     * state to lose. */
    unsigned mode = static_cast<unsigned>(
        f32(owner, kTypeValue) * 65536.0f) & 0xFFu;
    xmod_apply(self, mode);
}

/* ------------------------------------------------------------------------
 * Band limiting for the modulator's other three waveforms.
 *
 * Stock waveform 1..3 are `TriangleOscNaive`, `SquareOscNaive` and
 * `SawtoothOscNaive` -- naive is the original author's word and it is
 * accurate: the saw and the square are sampled straight through their
 * discontinuities, so every harmonic above Nyquist folds back into the
 * band.  At the 32 kHz sample rate this firmware runs, an audio-rate saw
 * at 1.8 kHz carries about as much aliasing as it does harmonics.
 *
 * polyBLEP replaces the two samples either side of a step with the
 * integral of a band-limited impulse, which is what removes most of it;
 * polyBLAMP does the same for the triangle's corners, where the value is
 * continuous but the slope is not.  Measured against an ideal band-limited
 * waveform, over 110 Hz to 3.5 kHz:
 *
 *     sawtooth   16 dB less alias energy
 *     square     16 dB
 *     triangle    4 dB   (it starts 20 dB cleaner: a corner rolls off as
 *                         1/n^2 where a step rolls off as 1/n)
 *
 * The correction only touches samples within one phase step of a
 * discontinuity, so a slow LFO -- where dt is a millionth -- is untouched,
 * and the divisions run about twice per cycle rather than per sample.
 *
 * A rate of zero gives dt = 0, and then `t < dt` and `t > 1 - dt` are both
 * false and nothing divides.  Negative rates would be equally harmless; no
 * source in this firmware produces one.
 * --------------------------------------------------------------------- */

namespace {

/* Phase convention, shared with the stock code: the oscillator keeps p in
 * [-0.5, 0.5) and each waveform is written in terms of t = p + 0.5. */
inline float phase01(float p) { return p + 0.5f; }
inline float wrap01(float t) { return t >= 1.0f ? t - 1.0f : t; }

/* The naive shapes, exactly as the stock renderers computed them.  They are
 * the band-limited versions' own starting point, and np_wave_shape uses them
 * on their own above kBlepMaxStep. */
inline float saw_naive(float t)    { return t + t - 1.0f; }
inline float square_naive(float t) { return t < 0.5f ? 1.0f : -1.0f; }
inline float tri_naive(float t)    { return 4.0f * __builtin_fabsf(t - 0.5f) - 1.0f; }

/* Above this phase step np_wave_shape drops the correction and renders the
 * naive shape.  The correction is what a fast modulator costs: it is arranged
 * as one unconditional expression per discontinuity so the polyBLEP branches
 * stay cheap, and that expression runs on every sample whatever the step --
 * measured at 69 instructions per triangle and 67 per square against 41 and
 * 45 without it, of a whole audio block of 35 thousand.
 *
 * dt = f / 32 kHz, so 0.06 is about 1.9 kHz.  Nothing an LFO or a synced
 * modulator can reach: only plain audio rate and the X-Mod ratio, which is
 * the load the freeze showed up under.  A modulator's aliases land as
 * slightly misplaced sidebands on the carrier rather than as tones of their
 * own, and above 1.9 kHz it has at most eight partials below Nyquist to
 * misplace.  Sweeping the ratio knob through the threshold is a step in
 * brightness, not a click -- the shape is continuous in time on both sides.
 *
 * This is deliberately *not* inside np_saw_value and friends: the
 * MultiWaveOscFast splices call those for the two main oscillators, where 1.9
 * kHz is an ordinary note and its aliases are the audible kind. */
constexpr float kBlepMaxStep = 0.06f;

inline float poly_blep(float t, float dt)
{
    if (t < dt) {
        float x = t / dt;
        return x + x - x * x - 1.0f;
    }
    if (t > 1.0f - dt) {
        float x = (t - 1.0f) / dt;
        return x * x + x + x + 1.0f;
    }
    return 0.0f;
}

inline float poly_blamp(float t, float dt)
{
    if (t < dt) {
        float x = t / dt - 1.0f;
        return -x * x * x * (1.0f / 3.0f);
    }
    if (t > 1.0f - dt) {
        float x = (t - 1.0f) / dt + 1.0f;
        return x * x * x * (1.0f / 3.0f);
    }
    return 0.0f;
}

} /* namespace */

/* stock: y = 2p, one downward step of -2 at the wrap */
extern "C" float np_saw_value(float p, float dt)
{
    float t = phase01(p);
    return saw_naive(t) - poly_blep(t, dt);
}

/* Stock is `y = p > 0 ? 1 : -1`, which after a retrigger starts the cycle
 * low: the beat lands on the falling edge and the rising edge sits in the
 * middle of the division.  Inverted here so the beat is the rising edge.
 *
 * That swaps which discontinuity is which, so the two polyBLEP corrections
 * swap sign with it: the step at t = 0 is now upward and the one at t = 0.5
 * downward. */
extern "C" float np_square_value(float p, float dt)
{
    float t = phase01(p);
    return square_naive(t) + poly_blep(t, dt) - poly_blep(wrap01(t + 0.5f), dt);
}

/* stock: y = 4|p| - 1, corners at t = 0 and t = 0.5, slope stepping by -+8 */
extern "C" float np_tri_value(float p, float dt)
{
    float t = phase01(p);
    return tri_naive(t)
         - 8.0f * dt * (poly_blamp(t, dt) - poly_blamp(wrap01(t + 0.5f), dt));
}

/* ------------------------------------------------------------------------
 * np_wave_shape -- the band-limited modulator, for the slot evaluators.
 *
 * H2/H3 replace the renderers inside MultiWaveOscFast, but that is not where
 * a modulation slot's oscillator is actually rendered.  Each of the three
 * per-slot evaluators -- QuantizedLinAD 0x0811C464 (slots 0, 4, 9), LinAD
 * 0x0811108C (1,2,5,6,10,11,12,17) and ExpAD 0x08110894 (3,7,8, X-Mod is 8)
 * -- carries its own inlined copy of all five waveforms, and *two* copies at
 * that, one per arm of the X-Envelope branch.  Six copies, all naive: the
 * sine is the parabola `x * (2 - |x|)` at 3.8 % THD, the square a bare sign
 * test, the saw `p + p`.  They share only the phase and step fields with
 * MultiWaveOscFast, so band-limiting its methods never reached them.
 *
 * This replaces all six.  `wave` is the value the dispatch had in r3 and
 * `owner` the slot; the phase advance, the wrap and the S&H behaviour are
 * reproduced from the stock blocks exactly, including that S&H stores its
 * phase before testing and that an out-of-range waveform yields 0.0 (every
 * one of the six bail targets loads that same constant).
 * --------------------------------------------------------------------- */

/* The modulator's sine: the same least-squares degree-7 polynomial on the
 * folded quarter wave that np_sine uses for the main oscillators, so an FM
 * operator's partials are clean to the noise floor of float32 (THD
 * 0.00005 %, third harmonic below -160 dB).
 *
 * History: p16 replaced this with the parabola plus one shaping term (THD
 * 0.078 %) to buy 16 instructions when it ran 80 times a block.  Since p19
 * a held slot renders once per block and only an audio-rate slot pays per
 * sample, so the polynomial costs about 200 instructions a block per
 * audio-rate sine -- ~1 % -- and it was put back.
 *
 * The wrap into [-1, 1) is flag-free (see the note that was here for the
 * parabola: u - 2*rint(u/2) is bit-identical to the compare over the
 * reachable range).  The fold to the quarter wave keeps its two compares:
 * a branch-free copysign costs as much as they do. */
extern "C" float np_sine_value(float p)
{
    float u = p + p - 0.5f;                /* -cos(2*pi*p) = sin(pi*u)   */
    u -= 2.0f * __builtin_rintf(0.5f * u); /* into [-1, 1)               */
    if (u > 0.5f)                          /* fold: sin(pi*u) is         */
        u = 1.0f - u;                      /* symmetric about u = +-0.5  */
    else if (u < -0.5f)
        u = -1.0f - u;
    const float x = u + u;                 /* [-1, 1]: sin(pi*x/2)       */
    const float x2 = x * x;
    float y = -0.004352813f;
    y = 0.079464867f + y * x2;
    y = -0.645906016f + y * x2;
    y = 1.570792380f + y * x2;
    return y * x;
}

extern "C" float np_wave_shape(void *owner, unsigned wave)
{
    if (wave > 4u)
        return 0.0f;                       /* what the stock bail loads */

    const unsigned po = kPhase[wave + 1u]; /* 0x58 0x84 0xA8 0xCC 0xF0 */
    float step = f32(owner, po + 8u);
    /* np_slot_hold evaluates a held slot once per eight-sample block and
     * raises this flag around the call: one advance here has to cover the
     * eight the stock would have made.  Reading the flag costs three
     * instructions; scaling all five step fields around the call, as the
     * wrapper first did, cost thirty.  dt scales with it, so a held slot
     * drops the band limiting from 240 Hz instead of 1.9 kHz -- it is a
     * control signal there, not audio, and that only makes it cheaper. */
    /* np_slot_hold publishes how many samples this one call stands for --
     * eight at a block boundary, fewer when a gate event mid-block made it
     * evaluate early.  Multiplying by a hardcoded 8 instead advanced the
     * phase a whole extra block on every note. */
    if (const uint32_t n = NP_BLOCK_MODE)
        step *= static_cast<float>(n);
    float p = f32(owner, po) + step;

    if (wave == kWaveSH) {
        /* stock stores the advanced phase first, then tests it */
        f32(owner, po) = p;
        void *lfo = lfo_of(owner);
        const bool wrapped = !(p < 0.5f);
        if (wrapped)
            f32(owner, po) = p - 1.0f;

        /* H5: sample and hold reaches white noise at the top of the knob.
         *
         * S&H draws on a phase wrap, so its update rate *is* the modulator
         * frequency, and that is capped twice over: the frequency table
         * stops at 12543.9 Hz and setRate clamps the phase step to 0.5
         * regardless.  Wide open that is a step every 2.55 samples -- close
         * to noise, but still carrying the comb colouring and the
         * sample-to-sample correlation of a stepped signal.  Drawing on
         * *every* sample is the end of that road, and it is white noise.
         *
         * The trigger is the knob, not the phase step, and that matters.
         * An audio-rate slot's table position is `knob - 0.5 + VCO1's
         * position`, so the frequency a full knob produces moves with the
         * played note: about 2.8 kHz at C2, 10.0 kHz at C4, and only from
         * about C5 does it clamp at the top of the table.  A threshold on
         * the step is therefore unreachable across most of the keyboard --
         * the first version of this used 0.35 and simply never fired.
         *
         * Ten of the knob's 127 positions, so the last 8 % of its travel,
         * and the same gesture whatever note is held.  Everything below is
         * bit-for-bit the stock behaviour.
         *
         * Plain audio rate only.  In ratio mode the same knob picks one of
         * 128 exact ratios and the top ten of those are real intervals worth
         * keeping; free and synced LFOs use it for a period. */
        if (wrapped || (u8(owner, kAudioOn) == kAudioFixed &&
                        f32(owner, kValue) >= kNoiseKnob)) {
            uint32_t r = reinterpret_cast<RandomFn>(kRandom)();
            const float v = static_cast<float>(r) * kRandScale - 1.0f;
            f32(lfo, kHeldValue) = v;
            return v;
        }
        return f32(lfo, kHeldValue);
    }

    /* The stock wrap, `p > 0.5 ? p-1 : p < -0.5 ? p+1 : p`, as one
     * flag-free expression -- see np_sine_value for why the compares are
     * worth removing.  setRate clamps the step to +-0.5 and p was in
     * [-0.5, 0.5], so p is in [-1, 1] here and rint(p) is exactly the 0/+-1
     * the compares would have subtracted, the two boundaries included. */
    p -= __builtin_rintf(p);
    f32(owner, po) = p;

    /* the sine needs neither the step nor t: leave before they are built */
    if (wave == 0u)
        return np_sine_value(p);

    const float dt = step < 0.0f ? -step : step;
    if (dt > kBlepMaxStep) {
        const float t = phase01(p);
        switch (wave) {
        case 1u: return tri_naive(t);
        case 2u: return square_naive(t);
        default: return saw_naive(t);
        }
    }
    switch (wave) {
    case 1u: return np_tri_value(p, dt);
    case 2u: return np_square_value(p, dt);
    default: return np_saw_value(p, dt);
    }
}
