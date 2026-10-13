/* R1: an instrument-aware mod-note randomizer (TEMPO+FUNC+note in mod-note
 * mode).
 *
 * The stock handler (0x0814476C) gives every continuous parameter of slots
 * 0, 2-4, 6-12 and the base of 13-16 an independent value, skewed toward the
 * top, and leaves waveforms, X-Mod types, X-Env routing, OutVol, Sync and
 * whatever the mod note held before untouched.  Thirty-odd unrelated
 * knobs at once is noise: every patch is a detuned, half-modulated glitch.
 *
 * This one first picks an instrument (kick, snare, hat, bass, pad, ...) by
 * weight, then builds that instrument from a recipe: the parameters that
 * make it what it is get values from musically chosen ranges, everything
 * else stays at the mod-note defaults.  Ranges are in the instrument's own
 * units (semitones, milliseconds, cutoff in semitones) and converted to raw
 * knob values with the curves measured in sysemu -- docs/patches.md R1.
 *
 * Event flow is the stock one: the handler owns the RandModNotePatchData,
 * clears the mod note (ClearModNoteData, so nothing of the old sound
 * survives) and then dispatches one ModNoteParamData per parameter that
 * differs from the reset value, synchronously through 0x081576D4 and with
 * the request's +4 word, exactly as the stock handler tags its own.
 * Runs in main (project event service), never in an interrupt.
 */
#include <stdint.h>
#include "np_diag.h"

extern "C" unsigned np_ui_pressed(void *ui, unsigned key);

namespace {
using Byte = uint8_t;

constexpr uintptr_t kDiag = 0x2000E400u;
constexpr uintptr_t kRandPatchVt = 0x08170188u;  /* ProjectEvent<RandModNotePatchData> */
constexpr uintptr_t kParamVt = 0x08164274u;      /* ProjectEvent<ModNoteParamData>     */
constexpr uintptr_t kClearVt = 0x0816489Cu;      /* ProjectEvent<ClearModNoteData>     */
constexpr uintptr_t kPatchParamVt = 0x0815EE7Cu; /* ProjectEvent<PatchParamData>       */
constexpr uintptr_t kDefaults = 0x0815E8A0u;     /* 19 x u16, what a reset base holds  */
constexpr uintptr_t kDwtCyccnt = 0xE0001004u;

using Manager = void *(*)();
using Alloc = void *(*)(unsigned);
using Dispatch = void (*)(void *, void **);
using Rng = uint32_t (*)();
using Deleter = void (*)(void *);

inline volatile uint32_t &diag(unsigned offset)
{
    return *reinterpret_cast<volatile uint32_t *>(kDiag + offset);
}

void destroy(void *event)
{
    const uintptr_t vt = *static_cast<const uint32_t *>(event);
    reinterpret_cast<Deleter>(reinterpret_cast<const uint32_t *>(vt)[1])(event);
}

/* ---- the parameter space ---------------------------------------------- */
enum Slot : unsigned {
    VCO1_TUNE, VCO1_FINE, VCO1_WAVE, VCO1_VOL, VCO2_TUNE, VCO2_FINE,
    VCO2_WAVE, VCO2_VOL, TZFM, CUTOFF, RESO, COLOR, FENV, ATTACK, DECAY,
    RELEASE, SUSTAIN, VOLUME, SYNC, kSlots
};
/* local 2 is the X-Env DECAY and local 3 its ATTACK (measured: local 3
 * moves the time to the peak, local 2 the fall after it) */
enum Local : unsigned {
    BASE, XMOD_AMT, XENV_DEC, XENV_ATK, XENV_AMT, XMOD_RATE, XMOD_WAVE,
    XMOD_TYPE, XENV_DEST, kLocals
};
/* X-Mod waveforms and types, as the WAVE and TYPE keys cycle them */
enum : unsigned { W_SINE, W_TRI, W_SQUARE, W_SAW, W_SH };
enum : unsigned { T_LFO, T_SYNCED, T_AUDIO, T_RATIO };

/* What 0x0811702C resets a mod note's bank to before its records replay:
 * the base from the defaults table, locals 1-5 centred, 6-8 zero.  A
 * parameter equal to this needs no record. */
unsigned reset_value(unsigned slot, unsigned local)
{
    if (local == BASE)
        return reinterpret_cast<const uint16_t *>(kDefaults)[slot];
    return local <= XMOD_RATE ? 0x8000u : 0u;
}

/* ---- units -> raw knob values ----------------------------------------
 * Every helper is constexpr and every recipe constant goes through K<>, so
 * the conversions happen in the compiler, not on the unit. */
template <unsigned V> struct K { static constexpr unsigned v = V; };
#define RAW(x) (K<(x)>::v)
/* p43: a recipe call whose arguments are all constants takes them from a
 * table in flash -- one load instead of eight arguments, four of them on
 * the stack: the bootloader caps the image at 512 KB (protocol.md 3.3) */
template <unsigned... V> struct Tbl { static constexpr uint16_t v[sizeof...(V)] = {V...}; };
#define KFILTER(...) (Tbl<__VA_ARGS__>::v)
#define KADSR(...) (Tbl<__VA_ARGS__>::v)
#define KXENV(...) (Tbl<__VA_ARGS__>::v)
/* consecutive bases: (slot, lo, hi) triples, applied in order */
#define KBASES(...) Tbl<__VA_ARGS__>::v, unsigned(sizeof(Tbl<__VA_ARGS__>::v) / 6)

constexpr unsigned clamp16(int v) { return v < 0 ? 0u : v > 0xFFFF ? 0xFFFFu : unsigned(v); }

/* VCO tune: the played note + st semitones (raw = (57 + st) * 256, kind 2,
 * measured: raw 0x3900 plays the note itself). */
constexpr unsigned tune(int st) { return clamp16((57 + st) * 256); }
/* DETUNE spans +-1 semitone around 0x7FFF. */
constexpr unsigned cents(int c) { return clamp16(0x7FFF + c * 32767 / 100); }
/* Cutoff coordinate c = raw / 256 + (note - 57) with FOLLOW on (+3 at the
 * C4 a mod-note key plays); c is semitones with 81 = 440 Hz:
 *   45 = 55 Hz, 57 = 110, 69 = 220, 81 = 440, 93 = 880, 105 = 1.76k,
 *   117 = 3.5k, 129 = 7k, 141 = 14k, 149 = top. */
constexpr unsigned cutoff(int c) { return clamp16((c - 3) * 256); }

/* Depth knobs (X-Mod amount, X-Env amount, ENV AMOUNT) are bipolar around
 * 0x8000 and cubic: depth = full * x^3 with x = (raw - 0x8000) / 0x8000.
 * Full scale is 126 semitones on a VCO tune, 149 cutoff semitones on the
 * cutoff and ENV AMOUNT, 1.0 on a normalised parameter. */
constexpr double cbrt_pos(double v)
{
    double x = v > 1.0 ? v : 1.0;
    for (int i = 0; i < 60; ++i)
        x = (2.0 * x + v / (x * x)) / 3.0;
    return x;
}
constexpr unsigned depth(double amount, double full)
{
    return amount >= 0
        ? clamp16(0x8000 + int(cbrt_pos(amount / full) * 32767.0 + 0.5))
        : clamp16(0x8000 - int(cbrt_pos(-amount / full) * 32768.0 + 0.5));
}
constexpr unsigned semis(double st) { return depth(st, 126.0); }
constexpr unsigned cuts(double c) { return depth(c, 149.0); }
constexpr unsigned norm(double d) { return depth(d, 1.0); }

/* Times, measured in sysemu: raw -> ms, interpolated linearly between
 * the measured points (the last ones above 2.5 s are extrapolated).
 * Main ADSR decay/release, peak to 10 % (the attack's 10..90 % is about
 * 1.3x the same raw; release has an 8 ms floor). */
struct Node { double ms; unsigned raw; };
constexpr Node kEnv[] = {
    {0.2, 0x0000}, {6.8, 0x2000}, {32.0, 0x4000}, {105.0, 0x6000},
    {287.0, 0x8000}, {695.0, 0xA000}, {1546.0, 0xC000}, {3300.0, 0xE000},
    {6500.0, 0xFFFF},
};
/* X-Env decay (local 2), peak to 10 % */
constexpr Node kXdec[] = {
    {0.2, 0x0000}, {5.7, 0x2000}, {13.1, 0x3000}, {26.7, 0x4000},
    {49.8, 0x5000}, {87.7, 0x6000}, {147.4, 0x7000}, {239.0, 0x8000},
    {377.0, 0x9000}, {580.0, 0xA000}, {1270.0, 0xC000}, {2600.0, 0xE000},
    {5000.0, 0xFFFF},
};
/* X-Env attack (local 3), start to peak */
constexpr Node kXatk[] = {
    {0.0, 0x0000}, {10.5, 0x2000}, {49.7, 0x4000}, {93.0, 0x5000},
    {164.0, 0x6000}, {276.0, 0x7000}, {449.0, 0x8000}, {1087.0, 0xA000},
    {2500.0, 0xC000}, {5500.0, 0xE000}, {11000.0, 0xFFFF},
};
template <unsigned N>
constexpr unsigned curve(const Node (&c)[N], double ms)
{
    if (ms <= c[0].ms)
        return c[0].raw;
    for (unsigned i = 1; i < N; ++i)
        if (ms <= c[i].ms)
            return c[i - 1].raw + unsigned((ms - c[i - 1].ms) / (c[i].ms - c[i - 1].ms)
                                           * double(c[i].raw - c[i - 1].raw) + 0.5);
    return c[N - 1].raw;
}
constexpr unsigned env(double ms) { return curve(kEnv, ms); }
constexpr unsigned xdec(double ms) { return curve(kXdec, ms); }
constexpr unsigned xatk(double ms) { return curve(kXatk, ms); }
/* Sustain is linear. */
constexpr unsigned level(double l) { return clamp16(int(l * 65535.0 + 0.5)); }

/* Free LFO (type 0) rate: 0.01 * 1000^(raw / 65536) Hz, read off the
 * setRate argument (100 s to 100 ms, not the manual's 40 s) */
constexpr Node kLfo[] = {
    {0.01, 0x0000}, {0.0237, 0x2000}, {0.0562, 0x4000}, {0.1334, 0x6000},
    {0.3162, 0x8000}, {0.750, 0xA000}, {1.778, 0xC000}, {4.217, 0xE000},
    {10.0, 0xFFFF},
};
constexpr unsigned lfo_hz(double hz) { return curve(kLfo, hz); }
/* Audio rate (type 2): the modulator sits (raw / 65536 - 0.5) * 127
 * semitones from VCO1's live pitch (np_lfo.cpp audio_position). */
constexpr unsigned audio_st(int st)
{
    return clamp16(0x8000 + (st * 65536 + (st < 0 ? -63 : 63)) / 127);
}
/* Ratio (type 3): detent d of the 128 is raw d * 512 + 256.  Bank A,
 * 0..63, is (d + 1) / 4 of the played note: d 3 = 1.0, d 15 = 4.0, d 55 =
 * 14.0; bank B, 64..127, the irrational Yamaha set (docs/ratio-table.md). */
constexpr unsigned ratio_detent(unsigned d) { return d * 512u + 256u; }
/* Synced (type 1): ten divisions, one per 10 % of the knob (np_lfo.cpp
 * kDivSelect); they run from the tempo whether or not the sequencer plays,
 * and restart at the pattern's start. */
enum Division : unsigned { D_1D, D_1, D_2D, D_2, D_4, D_4T, D_8, D_8T, D_16, D_32 };
constexpr unsigned division(unsigned d) { return d * 6554u + 3277u; }

/* VCO WAVE fades sine -> triangle -> square -> saw over the knob. */
constexpr unsigned kSine = 0x0000, kTri = 0x5555, kSquare = 0xAAAA, kSaw = 0xFFFF;

/* ---- randomness --------------------------------------------------------- */
struct Gen {
    uint32_t s;
    uint32_t next()
    {
        s ^= s << 13;
        s ^= s >> 17;
        s ^= s << 5;
        return s;
    }
    unsigned below(unsigned n) { return unsigned((uint64_t(next()) * n) >> 32); }
    /* inclusive, either order */
    unsigned exact(unsigned a, unsigned b)
    {
        return a <= b ? a + below(b - a + 1u) : b + below(a - b + 1u);
    }
    /* p49/p50: every drawn value's range 40 % wider -- a fifth of it more
     * on each side, within 0..0xFFFF (p49 +25 %, p50 another 15 %); a
     * VCO's tune and detune draw exact() */
    unsigned range(unsigned a, unsigned b)
    {
        if (a > b) {
            const unsigned t = a;
            a = b;
            b = t;
        }
        const unsigned w = (b - a) / 5u;
        return exact(a > w ? a - w : 0u, b + w < 0xFFFFu ? b + w : 0xFFFFu);
    }
    int irange(int a, int b) { return a + int(below(unsigned(b - a + 1))); }
    bool chance(unsigned percent) { return below(100u) < percent; }
    template <typename T, unsigned N>
    T pick(const T (&a)[N]) { return a[below(N)]; }
};

/* ---- what a recipe may take into account ------------------------------- */
/* p41 K1: what the members of a drum kit share, so the eight sound like one
 * machine: how long everything rings, how bright, how driven, one metal
 * for the hats and the cymbal, and the kick's note for the tom. */
struct Kit {
    int room;            /* raw added to every decay and release      */
    int bright;          /* cutoff semitones added                    */
    int drive;           /* raw added to the sounding VCO levels      */
    int metal_st;        /* VCO1 of the hats and cymbal, st           */
    int metal_iv;        /* ...VCO2 that far above it                 */
    unsigned metal_fine; /* ...VCO2's detune off the grid             */
    unsigned metal_tzfm; /* ...and their TZFM                         */
    int kick_note;       /* the kick's fundamental, MIDI, once built  */
};

/* p41 S1: the playing pattern's scale -- the one the mini keyboard shows,
 * set in the Scale page (root on the NOTE keys, scale on SEQ) -- and the
 * note this mod note plays at (UP+DOWN+note sets it; C4 by default). */
struct Context {
    uint16_t scale;      /* bit n: n semitones above the root is in it; 0 = chromatic */
    uint8_t root;
    int played;          /* MIDI */
    Kit *kit;            /* a kit being built, or null */
};

/* ---- the patch being built --------------------------------------------- */
inline int mod12(int v) { return ((v % 12) + 12) % 12; }
const int kFifth[] = {7};

struct Patch {
    uint16_t v[kSlots][kLocals];
    Gen *g;
    Context *cx;

    /* the played note + st is a tone of the scale (always, chromatic) */
    bool in_scale(int st) const
    {
        return !cx->scale || (cx->scale >> mod12(cx->played + st - cx->root) & 1u);
    }
    /* a pitch in [lo, hi] semitones from the played note; with a scale,
     * its root or (if the scale has one) its fifth, looking two semitones
     * further each way so the root is always there */
    int anchor(int lo, int hi)
    {
        if (!cx->scale)
            return g->irange(lo, hi);
        int c[20];
        unsigned n = 0;
        const bool fifth = cx->scale >> 7 & 1u;
        for (int st = lo - 2; st <= hi + 2 && n < 20; ++st) {
            const int d = mod12(cx->played + st - cx->root);
            if (d == 0 || (fifth && d == 7))
                c[n++] = st;
        }
        return n ? c[g->below(n)] : g->irange(lo, hi);
    }
    /* any tone of the scale in [lo, hi] */
    int tone(int lo, int hi)
    {
        int c[40];
        unsigned n = 0;
        for (int st = lo; st <= hi && n < 40; ++st)
            if (in_scale(st))
                c[n++] = st;
        return n ? c[g->below(n)] : g->irange(lo, hi);
    }
    /* an interval above (or below) `from` among the candidates that land in
     * the scale; failing all of them an octave, unison or fifth that does */
    template <unsigned N>
    int interval(int from, const int (&cand)[N])
    {
        int c[N];
        unsigned n = 0;
        for (int iv : cand)
            if (in_scale(from + iv))
                c[n++] = iv;
        if (n)
            return c[g->below(n)];
        static const int kSafe[] = {12, 0, 7, -12};
        for (int iv : kSafe)
            if (in_scale(from + iv))
                return iv;
        return 12;
    }

    void reset()
    {
        for (unsigned s = 0; s < kSlots; ++s)
            for (unsigned l = 0; l < kLocals; ++l)
                v[s][l] = uint16_t(reset_value(s, l));
    }
    void set(unsigned slot, unsigned local, unsigned raw) { v[slot][local] = uint16_t(clamp16(int(raw))); }
    void base(unsigned slot, unsigned raw) { set(slot, BASE, raw); }
    /* slots 0, 1, 4, 5: a VCO's tune and detune, the pitch -- exact */
    unsigned draw(unsigned slot, unsigned lo, unsigned hi)
    {
        return (slot & ~4u) <= 1u ? g->exact(lo, hi) : g->range(lo, hi);
    }
    void base(unsigned slot, unsigned lo, unsigned hi) { set(slot, BASE, draw(slot, lo, hi)); }

    __attribute__((noinline)) void bases(const uint16_t *t, unsigned n)
    {
        for (; n; --n, t += 3)
            set(t[0], BASE, t[1] == t[2] ? t[1] : draw(t[0], t[1], t[2]));
    }
    __attribute__((noinline)) void filter(const uint16_t *t) { filter(t[0], t[1], t[2], t[3], t[4], t[5], t[6], t[7]); }
    __attribute__((noinline)) void adsr(const uint16_t *t) { adsr(t[0], t[1], t[2], t[3], t[4], t[5], t[6], t[7]); }
    __attribute__((noinline)) void xenv(unsigned slot, const uint16_t *t) { xenv(slot, t[0], t[1], t[2], t[3], t[4], t[5]); }
    /* the shared main envelope (amp and filter) */
    void adsr(unsigned a_lo, unsigned a_hi, unsigned d_lo, unsigned d_hi,
              unsigned s_lo, unsigned s_hi, unsigned r_lo, unsigned r_hi)
    {
        base(ATTACK, a_lo, a_hi);
        base(DECAY, d_lo, d_hi);
        base(SUSTAIN, s_lo, s_hi);
        base(RELEASE, r_lo, r_hi);
    }
    /* a percussive shape: no sustain, and a release that matches the
     * decay so a short gate ends the sound the way a long one would */
    void hit(unsigned d_lo, unsigned d_hi)
    {
        base(ATTACK, 0);
        base(DECAY, d_lo, d_hi);
        base(SUSTAIN, 0);
        base(RELEASE, v[DECAY][BASE]);
    }
    /* X-Env: attack, decay and amount on one slot */
    void xenv(unsigned slot, unsigned a_lo, unsigned a_hi, unsigned d_lo,
              unsigned d_hi, unsigned amt_lo, unsigned amt_hi)
    {
        set(slot, XENV_ATK, g->range(a_lo, a_hi));
        set(slot, XENV_DEC, g->range(d_lo, d_hi));
        set(slot, XENV_AMT, g->range(amt_lo, amt_hi));
    }
    /* the same X-Env, exactly, on a second slot */
    void xenv_like(unsigned slot, unsigned from)
    {
        set(slot, XENV_ATK, v[from][XENV_ATK]);
        set(slot, XENV_DEC, v[from][XENV_DEC]);
        set(slot, XENV_AMT, v[from][XENV_AMT]);
    }
    /* X-Mod: waveform, type, rate and amount on one slot */
    void xmod(unsigned slot, unsigned wave, unsigned type, unsigned rate,
              unsigned amt)
    {
        set(slot, XMOD_WAVE, wave);
        set(slot, XMOD_TYPE, type);
        set(slot, XMOD_RATE, rate);
        set(slot, XMOD_AMT, amt);
    }
    /* a filter with its colour: 0 band-pass, 0x8000 low-pass, 0xFFFF
     * high-pass (between low and high pass is an all-pass) */
    void filter(unsigned c_lo, unsigned c_hi, unsigned r_lo, unsigned r_hi,
                unsigned col_lo, unsigned col_hi, unsigned e_lo, unsigned e_hi)
    {
        base(CUTOFF, c_lo, c_hi);
        base(RESO, r_lo, r_hi);
        base(COLOR, col_lo, col_hi);
        base(FENV, e_lo, e_hi);
    }
    /* noise: an S&H modulator at audio rate on a VCO's pitch, several
     * octaves above VCO1, randomises that VCO's pitch thousands of times a
     * second.  Mono has no noise source; this is the closest thing. */
    void noise(unsigned slot, unsigned amt_lo, unsigned amt_hi)
    {
        xmod(slot, W_SH, T_AUDIO, g->range(RAW(audio_st(24)), RAW(audio_st(48))),
             g->range(amt_lo, amt_hi));
    }
};

constexpr unsigned kLp = 0x8000;      /* COLOR: low-pass             */
constexpr unsigned kFenv0 = 0x7FFF;   /* ENV AMOUNT: no effect       */

/* ---- the instruments ------------------------------------------------------
 * The reference is the C4 a mod-note key plays: a tune of -24 is C2.  On a
 * sequencer step the whole instrument moves with the step's note.
 *
 * p42: wider.  Each instrument has several characters (a kick can be an 808
 * boom, a punch, a hard driven kick, a sub or a clicky techno kick), and
 * the ranges inside each run as far as still sounds like the instrument. */

void fm_index(Patch &p, Gen &g, unsigned detent, unsigned d_lo, unsigned d_hi,
              unsigned a_lo, unsigned a_hi);

/* ---- p43: techniques ------------------------------------------------------
 * Every slot's X-Mod is a full oscillator -- a free LFO, a synced one, an
 * audio-rate one (relative to VCO1) or a ratio of the played note -- and
 * its depth can ride an X-Env (dest 1).  The recipes use them as:
 *   op_fm      pitch FM: an operator on a VCO, an X-Env on its index
 *   am         gain at audio rate on a level: AM / ring sidebands, gating
 *   filter_fm  the cutoff swept at audio rate, note-tracked by a ratio
 *   wave_fm    a waveform flipped at audio rate: harmonics without a filter
 *   humanize   a small synced sample & hold: every step a little different
 * (measured in sysemu with tools/bench: each reaches its control value at
 * audio rate). */
bool untouched(const Patch &p, unsigned slot)
{
    return p.v[slot][XMOD_AMT] == 0x8000 && p.v[slot][XENV_AMT] == 0x8000;
}

/* the bank-A detent for a modulator `r4` quarters of a VCO that sits `st`
 * semitones from the played note (exact on whole octaves) */
unsigned harmonic(int st, unsigned r4)
{
    const int oct = st >= 0 ? st / 12 : -((-st + 11) / 12);
    int v = oct >= 0 ? int(r4) << oct : int(r4) >> (-oct);
    v -= 1;
    return v < 0 ? 0u : v > 63 ? 63u : unsigned(v);
}
/* bank B: the irrational Yamaha ratios, 0.71 .. ~12 */
unsigned metallic(Gen &g) { return 64u + g.below(48); }

void op_fm(Patch &p, Gen &g, unsigned slot, unsigned detent, unsigned i_lo, unsigned i_hi,
           unsigned d_lo, unsigned d_hi, unsigned stay_hi)
{
    static const unsigned kWave[] = {W_SINE, W_SINE, W_SINE, W_SINE, W_TRI, W_SQUARE};
    p.xmod(slot, g.pick(kWave), T_RATIO, ratio_detent(detent),
           stay_hi > 0x8000u ? g.range(0x8000, stay_hi) : 0x8000u);
    p.set(slot, XENV_DEST, 1);
    p.xenv(slot, 0, 0, d_lo, d_hi, i_lo, i_hi);
}

void am(Patch &p, Gen &g, unsigned slot, unsigned type, unsigned rate, unsigned wave,
        unsigned dep_lo, unsigned dep_hi, bool fade)
{
    if (fade) {
        p.xmod(slot, wave, type, rate, 0x8000);
        p.set(slot, XENV_DEST, 1);
        p.xenv(slot, 0, 0, RAW(xdec(30)), RAW(xdec(900)), dep_lo, dep_hi);
    } else {
        p.xmod(slot, wave, type, rate, g.range(dep_lo, dep_hi));
    }
}

void filter_fm(Patch &p, Gen &g, unsigned type, unsigned rate, unsigned d_lo, unsigned d_hi, bool fade)
{
    static const unsigned kWave[] = {W_SINE, W_SINE, W_TRI, W_SAW, W_SQUARE};
    if (fade) {
        p.xmod(CUTOFF, g.pick(kWave), type, rate, 0x8000);
        p.set(CUTOFF, XENV_DEST, 1);
        p.xenv(CUTOFF, 0, RAW(xatk(60)), RAW(xdec(40)), RAW(xdec(900)), d_lo, d_hi);
    } else {
        p.xmod(CUTOFF, g.pick(kWave), type, rate, g.range(d_lo, d_hi));
    }
}

void wave_fm(Patch &p, Gen &g, unsigned slot, unsigned detent, unsigned d_lo, unsigned d_hi)
{
    p.xmod(slot, g.chance(50) ? W_SINE : W_TRI, T_RATIO, ratio_detent(detent), g.range(d_lo, d_hi));
}

void humanize(Patch &p, Gen &g, unsigned slot, unsigned d_lo, unsigned d_hi)
{
    static const unsigned kDiv[] = {D_16, D_16, D_8, D_32};
    if (untouched(p, slot))
        p.xmod(slot, W_SH, T_SYNCED, division(g.pick(kDiv)), g.range(d_lo, d_hi));
}

/* a pinged filter: the oscillators all but off, a blip on VCO1's level
 * through a filter at the edge of oscillation -- the filter rings */
void ping(Patch &p, Gen &g, unsigned c_lo, unsigned c_hi)
{
    p.base(VCO1_WAVE, g.chance(50) ? RAW(kSquare) : RAW(kSaw));
    p.base(VCO1_VOL, 0);
    p.xenv(VCO1_VOL, KXENV(0, 0, RAW(xdec(2)), RAW(xdec(15)), RAW(norm(0.5)), RAW(norm(0.95))));
    p.filter(c_lo, c_hi, 0xE000, 0xFC00, 0x7000, 0x8000, kFenv0, kFenv0);
}

/* a click or a burst of noise at the start, on VCO2's level */
void transient(Patch &p, Gen &g, bool noisy)
{
    p.base(VCO2_WAVE, RAW(kSquare), RAW(kSaw));
    p.base(VCO2_TUNE, tune(g.irange(12, 40)));
    p.base(VCO2_VOL, 0);
    if (noisy)
        p.noise(VCO2_TUNE, RAW(semis(40)), RAW(semis(110)));
    p.xenv(VCO2_VOL, KXENV(0, 0, RAW(xdec(2)), RAW(xdec(25)), RAW(norm(0.3)), RAW(norm(0.95))));
}

void kick(Patch &p, Gen &g)
{
    enum { BOOM, PUNCH, HARD, SUB, TECHNO, FMK, PINGK };
    static const uint8_t kKind[] = {BOOM, BOOM, PUNCH, PUNCH, PUNCH, HARD, HARD, SUB, TECHNO, TECHNO,
                                    FMK, FMK, PINGK};
    const unsigned kind = g.pick(kKind);
    static const int8_t kLo[] = {-34, -30, -28, -36, -30, -32, -32}, kHi[] = {-26, -22, -20, -28, -22, -22, -22};
    const int kick = p.anchor(kLo[kind], kHi[kind]);
    if (p.cx->kit)
        p.cx->kit->kick_note = p.cx->played + kick;
    if (kind == FMK) {
        /* the body is VCO2, its pitch dropping; VCO1 at the body or an
         * octave up is the modulator, and an X-Env pings the TZFM: the
         * attack's punch is FM sidebands that die in a few ms */
        p.bases(KBASES(VCO1_VOL, 0, 0, VCO1_WAVE, RAW(kSine), RAW(kTri)));
        p.base(VCO1_TUNE, tune(kick + (g.chance(60) ? 0 : 12)));
        p.base(VCO2_WAVE, 0, 0x1000);
        p.base(VCO2_TUNE, tune(kick));
        p.bases(KBASES(VCO2_VOL, 0xA000, 0xE000, TZFM, 0, 0x1000));
        p.xenv(TZFM, KXENV(0, 0, RAW(xdec(3)), RAW(xdec(40)), RAW(norm(0.3)), RAW(norm(0.95))));
        p.xenv(VCO2_TUNE, KXENV(0, 0, RAW(xdec(10)), RAW(xdec(120)), RAW(semis(8)), RAW(semis(40))));
        p.filter(KFILTER(RAW(cutoff(90)), RAW(cutoff(149)), 0, 0x4000, kLp, kLp, kFenv0, RAW(cuts(15))));
        p.hit(RAW(env(120)), RAW(env(900)));
        p.base(VOLUME, 0xB000, 0xC800);
        return;
    }
    if (kind == PINGK) {
        /* the filter is the kick: rung by a blip, its pitch dropping */
        ping(p, g, 0, 0);
        p.base(VCO1_TUNE, tune(kick));             /* the blip at the kick's pitch */
        const int m = p.cx->played + kick;
        p.base(CUTOFF, unsigned(clamp16((m + 12 - (p.cx->played - 57)) * 256)));
        p.xenv(CUTOFF, KXENV(0, 0, RAW(xdec(10)), RAW(xdec(80)), RAW(cuts(20)), RAW(cuts(48))));
        p.hit(RAW(env(150)), RAW(env(900)));
        p.base(VOLUME, 0xB800, 0xD000);
        return;
    }
    p.base(VCO1_TUNE, tune(kick));
    p.base(VCO1_WAVE, 0, kind == HARD ? RAW(kTri) : 0x1800u);
    p.base(VCO1_VOL, kind == HARD ? 0xC000u : 0x7000u, kind == HARD ? 0xF800u : 0xB800u);
    /* the pitch drop that makes it a kick */
    switch (kind) {
    case BOOM:   p.xenv(VCO1_TUNE, KXENV(0, 0, RAW(xdec(30)), RAW(xdec(200)), RAW(semis(8)), RAW(semis(28)))); break;
    case SUB:    p.xenv(VCO1_TUNE, KXENV(0, 0, RAW(xdec(20)), RAW(xdec(120)), RAW(semis(5)), RAW(semis(18)))); break;
    case HARD:   p.xenv(VCO1_TUNE, KXENV(0, 0, RAW(xdec(8)), RAW(xdec(80)), RAW(semis(24)), RAW(semis(60)))); break;
    default:     p.xenv(VCO1_TUNE, KXENV(0, 0, RAW(xdec(10)), RAW(xdec(110)), RAW(semis(14)), RAW(semis(48)))); break;
    }
    if (kind == TECHNO || g.chance(35))
        transient(p, g, g.chance(30));
    else if (g.chance(15)) {                           /* a growl underneath */
        p.base(VCO2_WAVE, RAW(kSine), RAW(kTri));
        p.base(VCO2_TUNE, tune(kick + g.irange(-1, 1) * 12));
        p.bases(KBASES(VCO2_VOL, 0x2000, 0x6000, TZFM, 0x1000, 0x5000));
    }
    if (g.chance(15))       /* a resonant filter that closes with the hit */
        p.filter(KFILTER(RAW(cutoff(70)), RAW(cutoff(110)), 0x8000, 0xC000, kLp, kLp, RAW(cuts(10)), RAW(cuts(40))));
    else
        p.filter(KFILTER(RAW(cutoff(75)), RAW(cutoff(149)), 0, 0x5000, kLp, kLp, kFenv0, RAW(cuts(20))));
    switch (kind) {
    case BOOM:   p.hit(RAW(env(600)), RAW(env(2500))); break;
    case SUB:    p.hit(RAW(env(300)), RAW(env(1200))); break;
    case HARD:   p.hit(RAW(env(120)), RAW(env(500))); break;
    default:     p.hit(RAW(env(90)), RAW(env(450))); break;
    }
    p.base(VOLUME, 0xB000, 0xC800);
}

void snare(Patch &p, Gen &g)
{
    /* body: from a thick low snare to a piccolo */
    p.base(VCO1_WAVE, RAW(kSine), RAW(kTri) + 0x1000);
    p.base(VCO1_TUNE, tune(p.anchor(-17, 2)));
    p.xenv(VCO1_TUNE, KXENV(0, 0, RAW(xdec(8)), RAW(xdec(80)), RAW(semis(2)), RAW(semis(24))));
    if (g.chance(55)) {                                /* body shorter than noise   */
        p.base(VCO1_VOL, 0, 0x2000);
        p.xenv(VCO1_VOL, KXENV(0, 0, RAW(xdec(25)), RAW(xdec(200)), RAW(norm(0.4)), RAW(norm(0.9))));
    } else {
        p.base(VCO1_VOL, 0x4000, 0xA000);
    }
    /* noise: from a soft wash to a hard rattle */
    p.base(VCO2_WAVE, RAW(kTri), RAW(kSaw));
    p.base(VCO2_TUNE, tune(g.irange(6, 40)));
    p.base(VCO2_VOL, 0x7000, 0xD000);
    p.noise(VCO2_TUNE, RAW(semis(25)), RAW(semis(110)));
    if (g.chance(50))
        p.base(TZFM, 0x1000, 0xA000);
    switch (g.below(4)) {
    case 0:  /* band-pass into low-pass */
        p.filter(KFILTER(RAW(cutoff(95)), RAW(cutoff(135)), 0x0800, 0x6000, 0x1000, 0x7000, RAW(cuts(-10)), RAW(cuts(20))));
        break;
    case 1:  /* all-pass into high-pass: thin and crisp */
        p.filter(KFILTER(RAW(cutoff(90)), RAW(cutoff(125)), 0x0800, 0x5000, 0xC000, 0xF000, kFenv0, RAW(cuts(15))));
        break;
    case 2:  /* a ringing, resonant snare */
        p.filter(KFILTER(RAW(cutoff(95)), RAW(cutoff(120)), 0x7000, 0xB000, 0x2000, 0x8000, kFenv0, RAW(cuts(15))));
        break;
    default: /* lo-fi: dark and narrow */
        p.filter(KFILTER(RAW(cutoff(80)), RAW(cutoff(105)), 0x3000, 0x8000, 0x0000, 0x3000, kFenv0, kFenv0));
        break;
    }
    switch (g.below(8)) {
    case 0: case 1:     /* noise through a filter that jitters at audio rate */
        p.xmod(CUTOFF, W_SH, T_AUDIO, g.range(RAW(audio_st(24)), RAW(audio_st(48))),
               g.range(RAW(cuts(8)), RAW(cuts(35))));
        break;
    case 2:             /* a gritty, gated noise: S&H on its level */
        am(p, g, VCO2_VOL, T_AUDIO, g.range(RAW(audio_st(0)), RAW(audio_st(36))), W_SH,
           RAW(norm(0.2)), RAW(norm(0.6)), false);
        break;
    case 3:             /* every hit a little different */
        humanize(p, g, DECAY, RAW(norm(0.03)), RAW(norm(0.1)));
        break;
    default:
        break;
    }
    p.hit(RAW(env(60)), RAW(env(600)));
    p.base(VOLUME, 0xB000, 0xC800);
}

void clap(Patch &p, Gen &g)
{
    const int st1 = g.irange(0, 24);
    p.base(VCO1_WAVE, RAW(kSquare), RAW(kSaw));
    p.base(VCO1_TUNE, tune(st1));
    p.bases(KBASES(VCO1_VOL, 0x3000, 0x8000, VCO2_WAVE, RAW(kTri), RAW(kSaw)));
    p.base(VCO2_TUNE, tune(g.irange(10, 38)));
    p.bases(KBASES(VCO2_VOL, 0x9000, 0xD800, TZFM, 0x2000, 0xC000));
    p.noise(VCO2_TUNE, RAW(semis(40)), RAW(semis(110)));
    if (g.chance(80))
        p.filter(KFILTER(RAW(cutoff(88)), RAW(cutoff(122)), 0x1800, 0x9000, 0, 0x2800, kFenv0, kFenv0));
    else
        p.filter(KFILTER(RAW(cutoff(95)), RAW(cutoff(125)), 0x1000, 0x5000, 0xC000, 0xF000, kFenv0, kFenv0));
    /* the hand claps: a square on the volume at 80-130 Hz (faster, and the
     * output's smoothing fills the gaps in) whose depth the X-Env carries
     * away; how many claps is the X-Env's decay */
    const int st = g.irange(40, 48) - (p.cx->played + st1);
    p.xmod(VOLUME, W_SQUARE, T_AUDIO, audio_st(st), 0x8000);
    p.set(VOLUME, XENV_DEST, 1);
    p.xenv(VOLUME, KXENV(0, 0, RAW(xdec(20)), RAW(xdec(70)), RAW(norm(0.85)), RAW(norm(1.0))));
    if (g.chance(30))
        humanize(p, g, CUTOFF, RAW(cuts(3)), RAW(cuts(10)));
    p.hit(RAW(env(100)), RAW(env(900)));
    p.base(VOLUME, 0xA000, 0xB800);     /* a little lower: deeper gaps */
}

/* the hats' and the cymbal's metal: two squares at an inharmonic interval
 * through heavy TZFM -- in a kit, the kit's own */
void metal(Patch &p, Gen &g, int st_lo, int st_hi, int iv_lo, int iv_hi)
{
    const Kit *k = p.cx->kit;
    const int st1 = k ? k->metal_st : g.irange(st_lo, st_hi);
    p.base(VCO1_WAVE, RAW(kTri), RAW(kSaw));
    p.base(VCO1_TUNE, tune(st1));
    p.bases(KBASES(VCO1_VOL, 0x5000, 0x9800, VCO2_WAVE, RAW(kSquare) - 0x1000, RAW(kSaw), RAW(kSquare) - 0x1000, RAW(kSaw)));
    p.base(VCO2_TUNE, tune(st1 + (k ? k->metal_iv : g.irange(iv_lo, iv_hi))));
    if (k) {
        p.base(VCO2_FINE, k->metal_fine);
        p.base(TZFM, k->metal_tzfm);
    } else {
        p.bases(KBASES(VCO2_FINE, RAW(cents(-70)), RAW(cents(70)), TZFM, 0x6000, 0xFFFF));
    }
    p.base(VCO2_VOL, 0x8000, 0xC800);
}

/* four operators of metal: each VCO's pitch FM'd at an irrational ratio,
 * and VCO1 into VCO2 through the TZFM */
void four_op(Patch &p, Gen &g, unsigned i_lo, unsigned i_hi)
{
    p.xmod(VCO1_TUNE, W_SINE, T_RATIO, ratio_detent(metallic(g)), g.range(i_lo, i_hi));
    if (untouched(p, VCO2_TUNE))
        p.xmod(VCO2_TUNE, W_SINE, T_RATIO, ratio_detent(metallic(g)), g.range(i_lo, i_hi));
}

void hat(Patch &p, Gen &g, bool open)
{
    metal(p, g, 12, 40, 2, 13);
    if (g.chance(55))
        p.noise(VCO2_TUNE, RAW(semis(20)), RAW(semis(110)));
    if (g.chance(40))
        four_op(p, g, RAW(semis(6)), RAW(semis(30)));
    if (g.chance(40))
        humanize(p, g, DECAY, RAW(norm(0.02)), RAW(norm(0.1)));
    if (g.chance(20) && untouched(p, CUTOFF))
        p.xmod(CUTOFF, W_SH, T_AUDIO, g.range(RAW(audio_st(30)), RAW(audio_st(60))),
               g.range(RAW(cuts(5)), RAW(cuts(20))));
    if (g.chance(85))       /* high-passed */
        p.filter(KFILTER(RAW(cutoff(105)), RAW(cutoff(145)), 0, 0x7000, 0xC000, 0xFFFF, kFenv0, kFenv0));
    else                    /* a band-passed tick */
        p.filter(KFILTER(RAW(cutoff(115)), RAW(cutoff(140)), 0x2000, 0x8000, 0, 0x2000, kFenv0, kFenv0));
    if (open)
        p.hit(RAW(env(180)), RAW(env(1200)));
    else
        p.hit(RAW(env(8)), RAW(env(130)));
    p.base(VOLUME, 0xB000, 0xC800);
}

void tom(Patch &p, Gen &g)
{
    p.base(VCO1_WAVE, RAW(kSine), RAW(kTri));
    /* in a kit, a fifth, an octave or an octave and a fifth over its kick */
    int st = p.anchor(-28, 2);
    if (const Kit *k = p.cx->kit) {
        static const int kOver[] = {7, 12, 12, 19};
        const int want = k->kick_note + p.interval(k->kick_note - p.cx->played, kOver) - p.cx->played;
        if (k->kick_note && want >= -30 && want <= 4)
            st = want;
    }
    p.base(VCO1_TUNE, tune(st));
    p.base(VCO1_VOL, 0x7000, 0xC000);
    p.xenv(VCO1_TUNE, KXENV(0, 0, RAW(xdec(20)), RAW(xdec(300)), RAW(semis(2)), RAW(semis(30))));
    switch (g.below(5)) {
    case 0: case 1: {                                  /* a fifth or octave of body */
        static const int kBody[] = {7, 12};
        p.base(VCO2_WAVE, RAW(kSine), RAW(kTri));
        p.base(VCO2_TUNE, tune(st + p.interval(st, kBody)));
        p.base(VCO2_VOL, 0x2000, 0x5800);
        break;
    }
    case 2:                                            /* a noise skin */
        transient(p, g, true);
        break;
    case 3:                                            /* FM'd */
        p.base(VCO2_WAVE, RAW(kSine));
        p.base(VCO2_TUNE, tune(st));
        p.bases(KBASES(VCO2_VOL, 0x2000, 0x5000, TZFM, 0x0800, 0x3000));
        p.xenv(TZFM, KXENV(0, 0, RAW(xdec(30)), RAW(xdec(250)), RAW(norm(0.1)), RAW(norm(0.6))));
        break;
    default:
        break;
    }
    p.filter(KFILTER(RAW(cutoff(80)), RAW(cutoff(149)), 0, 0x6000, kLp, kLp, kFenv0, RAW(cuts(20))));
    if (g.chance(20)) {        /* or the filter rings the tom, its pitch dropping */
        ping(p, g, 0, 0);
        p.base(VCO2_VOL, 0);
        const int m = p.cx->played + st;
        p.base(CUTOFF, unsigned(clamp16((m + 12 - (p.cx->played - 57)) * 256)));
        p.xenv(CUTOFF, KXENV(0, 0, RAW(xdec(20)), RAW(xdec(200)), RAW(cuts(6)), RAW(cuts(24))));
    }
    p.hit(RAW(env(120)), RAW(env(1200)));
    p.base(VOLUME, 0xB000, 0xC800);
}

/* p54: the cowbell, the rim / clave / wood block and the conga / bongo went
 * to instruments of their own (cowbell, rim, conga), with more to them */
void perc(Patch &p, Gen &g)
{
    switch (g.below(7)) {
    case 0: {   /* a metal can: two VCOs, each FM'd at an irrational ratio */
        const int st = p.tone(0, 24);
        p.base(VCO1_WAVE, RAW(kSine), RAW(kTri));
        p.base(VCO1_TUNE, tune(st));
        p.bases(KBASES(VCO1_VOL, 0x6000, 0xA000, VCO2_WAVE, RAW(kSine), RAW(kTri)));
        p.base(VCO2_TUNE, tune(st + g.irange(3, 19)));
        p.bases(KBASES(VCO2_VOL, 0x6000, 0xA000, TZFM, 0x2000, 0x8000));
        four_op(p, g, RAW(semis(4)), RAW(semis(24)));
        p.filter(KFILTER(RAW(cutoff(95)), RAW(cutoff(140)), 0x1000, 0x6000, 0x6000, 0xC000, kFenv0, kFenv0));
        p.hit(RAW(env(40)), RAW(env(700)));
        break;
    }
    case 1: {   /* an AM ping: a sine whose level swings at an irrational ratio */
        const int st = p.tone(0, 24);
        p.bases(KBASES(VCO1_VOL, 0, 0, VCO2_WAVE, RAW(kSine), 0x1000));
        p.base(VCO2_TUNE, tune(st));
        p.base(VCO2_VOL, 0x8000, 0xC000);
        am(p, g, VCO2_VOL, T_RATIO, ratio_detent(metallic(g)), W_SINE, RAW(norm(0.5)), RAW(norm(0.95)), g.chance(50));
        p.filter(KFILTER(RAW(cutoff(115)), RAW(cutoff(149)), 0, 0x3000, kLp, kLp, kFenv0, kFenv0));
        p.hit(RAW(env(60)), RAW(env(700)));
        break;
    }
    case 2:     /* a zip: filter FM at audio rate, carried away by an X-Env */
        p.base(VCO1_WAVE, RAW(kSquare), RAW(kSaw));
        p.base(VCO1_TUNE, tune(p.tone(-12, 24)));
        p.bases(KBASES(VCO1_VOL, 0x8000, 0xB800, CUTOFF, RAW(cutoff(70)), RAW(cutoff(110)), RESO, 0x4000, 0xB000));
        filter_fm(p, g, T_AUDIO, g.range(RAW(audio_st(-12)), RAW(audio_st(24))), RAW(cuts(15)), RAW(cuts(60)), true);
        p.hit(RAW(env(30)), RAW(env(300)));
        break;
    case 3:     /* blip */
        p.base(VCO1_WAVE, RAW(kSine), RAW(kSquare));
        p.base(VCO1_TUNE, tune(p.tone(-6, 30)));
        p.base(VCO1_VOL, 0x7000, 0xB800);
        if (g.chance(50))
            p.xenv(VCO1_TUNE, KXENV(0, 0, RAW(xdec(5)), RAW(xdec(80)), RAW(semis(6)), RAW(semis(36))));
        else
            p.xenv(VCO1_TUNE, KXENV(0, 0, RAW(xdec(5)), RAW(xdec(80)), RAW(semis(-36)), RAW(semis(-6))));
        p.filter(KFILTER(RAW(cutoff(100)), RAW(cutoff(149)), 0, 0x5000, kLp, kLp, kFenv0, kFenv0));
        p.hit(RAW(env(20)), RAW(env(250)));
        break;
    case 4: {   /* a metal ping: a short FM bell */
        const int st = p.tone(0, 24);
        p.base(VCO2_WAVE, RAW(kSine), RAW(kTri));
        p.base(VCO2_TUNE, tune(st));
        p.bases(KBASES(VCO2_VOL, 0x8000, 0xB800, VCO1_WAVE, RAW(kSine), RAW(kTri)));
        p.base(VCO1_TUNE, tune(st + g.irange(5, 30)));
        p.bases(KBASES(VCO1_FINE, RAW(cents(-80)), RAW(cents(80)), VCO1_VOL, 0, 0x1800, TZFM, 0x2000, 0x7000));
        p.xenv(TZFM, KXENV(0, 0, RAW(xdec(20)), RAW(xdec(200)), RAW(norm(0.2)), RAW(norm(0.8))));
        p.filter(KFILTER(RAW(cutoff(110)), RAW(cutoff(149)), 0, 0x3000, kLp, kLp, kFenv0, kFenv0));
        p.hit(RAW(env(60)), RAW(env(500)));
        break;
    }
    case 5:     /* triangle (the instrument): a high ringing tone */
        p.base(VCO1_WAVE, RAW(kSine), RAW(kTri));
        p.base(VCO1_TUNE, tune(p.tone(24, 40)));
        p.bases(KBASES(VCO1_VOL, 0x6000, 0x9000, VCO2_WAVE, RAW(kSine), RAW(kSine)));
        p.base(VCO2_TUNE, tune(p.tone(24, 40) + g.irange(0, 1) * 7));
        p.bases(KBASES(VCO2_FINE, RAW(cents(-40)), RAW(cents(40)), VCO2_VOL, 0x3000, 0x6000));
        p.filter(KFILTER(RAW(cutoff(120)), RAW(cutoff(149)), 0x4000, 0x9000, 0x8000, 0xC000, kFenv0, kFenv0));
        p.hit(RAW(env(400)), RAW(env(1800)));
        break;
    default:    /* a tick: the shortest click there is, high-passed */
        transient(p, g, g.chance(50));
        p.base(VCO1_VOL, 0);
        p.filter(KFILTER(RAW(cutoff(100)), RAW(cutoff(140)), 0, 0x6000, 0xA000, 0xFFFF, kFenv0, kFenv0));
        p.hit(RAW(env(5)), RAW(env(40)));
        break;
    }
    p.base(VOLUME, 0xB000, 0xC800);
}

/* VCO waves: anything from triangle to saw for the rich voices, sine to
 * triangle-ish for the soft ones */
const unsigned kRich[] = {kSaw, kSquare, kSaw, 0xD000, kTri + 0x2000, kSquare + 0x1800};
const unsigned kSoft[] = {kSaw, kTri, 0xD000, kTri + 0x1800, kSine + 0x2000};
unsigned rich(Gen &g) { return g.chance(70) ? g.pick(kRich) : g.range(RAW(kTri), RAW(kSaw)); }

void bass(Patch &p, Gen &g)
{
    static const int kOct[] = {-36, -24, -24, -24, -24, -12, -12, -12};
    const int st = g.pick(kOct);
    p.base(VCO1_WAVE, g.chance(10) ? g.range(RAW(kSine), RAW(kTri)) : rich(g));
    p.base(VCO1_TUNE, tune(st));
    p.base(VCO1_VOL, 0x7000, 0xC800);
    switch (g.below(6)) {
    case 0: case 1:                                     /* VCO1 alone   */
        break;
    case 2:                                             /* sub octave   */
        p.base(VCO2_WAVE, RAW(kSine), RAW(kSquare));
        p.base(VCO2_TUNE, tune(st - 12));
        p.base(VCO2_VOL, 0x5000, 0xA800);
        break;
    case 3: {                                           /* fat unison   */
        p.base(VCO2_WAVE, rich(g));
        p.base(VCO2_TUNE, tune(st));
        const bool up = g.chance(50);
        p.base(VCO2_FINE, up ? RAW(cents(5)) : RAW(cents(-35)), up ? RAW(cents(35)) : RAW(cents(-5)));
        p.base(VCO2_VOL, 0x7000, 0xB800);
        break;
    }
    case 4: {                                           /* an octave up */
        p.base(VCO2_WAVE, rich(g));
        p.base(VCO2_TUNE, tune(st + 12));
        p.base(VCO2_VOL, 0x4000, 0x8000);
        break;
    }
    default:                                            /* a fifth up   */
        p.base(VCO2_WAVE, rich(g));
        p.base(VCO2_TUNE, tune(st + p.interval(st, kFifth)));
        p.base(VCO2_VOL, 0x4000, 0x8800);
        break;
    }
    if (g.chance(20)) {                                 /* FM pluck     */
        p.base(TZFM, 0x0800, 0x5000);
        p.xenv(TZFM, KXENV(0, 0, RAW(xdec(40)), RAW(xdec(400)), RAW(norm(0.1)), RAW(norm(0.7))));
    } else if (g.chance(15)) {                          /* sync sweep   */
        p.bases(KBASES(SYNC, 1, 1, VCO2_WAVE, RAW(kSaw), RAW(kSaw)));
        p.base(VCO2_TUNE, tune(st + g.irange(5, 30)));
        p.base(VCO2_VOL, 0x8000, 0xC000);
        p.xenv(VCO2_TUNE, KXENV(0, 0, RAW(xdec(50)), RAW(xdec(600)), RAW(semis(5)), RAW(semis(30))));
    }
    const unsigned colour = g.chance(85) ? g.range(0x7000, 0x8800) : g.range(0x2000, 0x6000);
    const bool down = g.chance(10);
    p.filter(RAW(cutoff(40)), RAW(cutoff(105)), 0, 0xB000, colour, colour,
             down ? RAW(cuts(-30)) : RAW(cuts(5)), down ? RAW(cuts(-5)) : RAW(cuts(90)));
    switch (g.below(8)) {
    case 0:     /* filter FM locked to the note: growl that stays in tune */
        filter_fm(p, g, T_RATIO, ratio_detent(harmonic(st, g.chance(60) ? 4 : 8)),
                  RAW(cuts(4)), RAW(cuts(20)), g.chance(50));
        break;
    case 1:     /* the waveform flipping at the note: harmonics without a filter */
        wave_fm(p, g, VCO1_WAVE, harmonic(st, g.chance(50) ? 4 : 8), RAW(norm(0.1)), RAW(norm(0.4)));
        break;
    case 2:     /* AM at half the note: a sub in the sidebands */
        am(p, g, VCO1_VOL, T_RATIO, ratio_detent(harmonic(st, 2)), W_SINE, RAW(norm(0.2)), RAW(norm(0.5)), false);
        break;
    case 3:     /* an FM bite on VCO1, gone in a moment */
        if (untouched(p, VCO1_TUNE))
            op_fm(p, g, VCO1_TUNE, harmonic(st, g.chance(50) ? 4 : 8), RAW(semis(3)), RAW(semis(14)),
                  RAW(xdec(30)), RAW(xdec(300)), 0);
        break;
    default:
        break;
    }
    p.adsr(KADSR(0, RAW(env(10)), RAW(env(60)), RAW(env(1500)), 0, RAW(level(1.0)), RAW(env(15)), RAW(env(400))));
    p.base(VOLUME, 0xB000, 0xC800);
}

void acid(Patch &p, Gen &g)
{
    p.base(VCO1_WAVE, g.chance(50) ? RAW(kSaw) : RAW(kSquare));
    const int st = g.chance(65) ? -24 : -12;
    p.base(VCO1_TUNE, tune(st));
    p.base(VCO1_VOL, 0x8000, 0xE000);                   /* into the filter's drive */
    if (g.chance(20)) {                                 /* a sub under it */
        p.base(VCO2_WAVE, RAW(kSquare));
        p.base(VCO2_TUNE, tune(st - 12));
        p.base(VCO2_VOL, 0x3000, 0x6000);
    }
    p.filter(KFILTER(RAW(cutoff(28)), RAW(cutoff(80)), 0x9000, 0xF400, kLp, kLp, RAW(cuts(30)), RAW(cuts(120))));
    if (g.chance(25))                                   /* an accent of its own */
        p.xenv(CUTOFF, KXENV(0, 0, RAW(xdec(30)), RAW(xdec(200)), RAW(cuts(10)), RAW(cuts(40))));
    if (g.chance(50))       /* a different accent on every step */
        humanize(p, g, FENV, RAW(cuts(10)), RAW(cuts(40)));
    if (g.chance(30))       /* and a different length */
        humanize(p, g, DECAY, RAW(norm(0.05)), RAW(norm(0.2)));
    p.adsr(KADSR(0, 0, RAW(env(60)), RAW(env(700)), 0, RAW(level(0.4)), RAW(env(15)), RAW(env(120))));
    p.base(VOLUME, 0xB000, 0xC800);
}

void lead(Patch &p, Gen &g)
{
    static const int kOct[] = {-12, -12, 0, 0, 0, 12};
    const int st = g.pick(kOct);
    p.base(VCO1_WAVE, rich(g));
    p.base(VCO1_TUNE, tune(st));
    p.base(VCO1_VOL, 0x6000, 0xB000);
    p.base(VCO2_WAVE, rich(g));
    static const int kInterval[] = {0, 0, 0, 12, 7, -12, 5, 19, 24, 3, 4};
    const int iv = p.interval(st, kInterval);
    p.base(VCO2_TUNE, tune(st + iv));
    if (iv == 0) {
        const bool up = g.chance(50);
        p.base(VCO2_FINE, up ? RAW(cents(4)) : RAW(cents(-25)), up ? RAW(cents(25)) : RAW(cents(-4)));
    }
    p.base(VCO2_VOL, 0x4000, 0x9800);
    p.filter(KFILTER(RAW(cutoff(70)), RAW(cutoff(145)), 0, 0x9000, 0x6800, 0x8800, RAW(cuts(-10)), RAW(cuts(60))));
    p.adsr(KADSR(0, RAW(env(150)), RAW(env(100)), RAW(env(1500)), RAW(level(0.3)), RAW(level(1.0)), RAW(env(30)), RAW(env(800))));
    if (g.chance(50)) {
        /* delayed vibrato: a sine whose depth the X-Env fades in */
        p.xmod(VCO1_TUNE, W_SINE, T_LFO, g.range(RAW(lfo_hz(4)), RAW(lfo_hz(7.5))), 0x8000);
        p.set(VCO1_TUNE, XENV_DEST, 1);
        p.xenv(VCO1_TUNE, KXENV(RAW(xatk(50)), RAW(xatk(800)), RAW(xdec(2500)), RAW(xdec(5000)), RAW(semis(0.1)), RAW(semis(0.6))));
    }
    static const unsigned kBright[] = {4, 8, 8, 12, 16};
    switch (g.below(5)) {
    case 0:     /* VCO2 an FM operator: a brighter, glassier attack */
        if (untouched(p, VCO2_TUNE))
            op_fm(p, g, VCO2_TUNE, harmonic(st + iv, g.pick(kBright)), RAW(semis(3)), RAW(semis(15)),
                  RAW(xdec(100)), RAW(xdec(1500)), RAW(semis(4)));
        break;
    case 1:     /* VCO1's waveform flipping at the note */
        wave_fm(p, g, VCO1_WAVE, harmonic(st, g.chance(50) ? 4 : 8), RAW(norm(0.05)), RAW(norm(0.3)));
        break;
    default:
        break;
    }
    p.base(VOLUME, 0xA800, 0xC000);
}

void pad(Patch &p, Gen &g)
{
    static const int kOct[] = {-24, -12, -12, 0, 0};
    const int st = g.pick(kOct);
    p.base(VCO1_WAVE, g.chance(70) ? g.pick(kSoft) : rich(g));
    p.base(VCO1_TUNE, tune(st));
    p.base(VCO1_VOL, 0x5000, 0x9800);
    p.base(VCO2_WAVE, g.chance(70) ? g.pick(kSoft) : rich(g));
    static const int kInterval[] = {0, 0, 7, 12, -12, 5, 3, 4, 19, 24, 10};
    const int iv = p.interval(st, kInterval);
    p.base(VCO2_TUNE, tune(st + iv));
    p.bases(KBASES(VCO2_FINE, RAW(cents(-30)), RAW(cents(30)), VCO2_VOL, 0x5000, 0x9800));
    /* bench: a base of 55 with a negative ENV AMOUNT left a pad at ~70 Hz */
    p.filter(KFILTER(RAW(cutoff(70)), RAW(cutoff(130)), 0, 0x8000, 0x6000, 0x8800, RAW(cuts(-10)), RAW(cuts(25))));
    p.adsr(KADSR(RAW(env(80)), RAW(env(3000)), RAW(env(1000)), RAW(env(5000)), RAW(level(0.5)), RAW(level(1.0)), RAW(env(400)), RAW(env(5000))));
    /* slow movement on the cutoff */
    p.xmod(CUTOFF, g.chance(50) ? W_SINE : W_TRI, g.chance(75) ? T_LFO : T_SYNCED,
           0, g.range(RAW(cuts(3)), RAW(cuts(20))));
    p.set(CUTOFF, XMOD_RATE, p.v[CUTOFF][XMOD_TYPE] == T_LFO
                                 ? g.range(RAW(lfo_hz(0.05)), RAW(lfo_hz(1)))
                                 : division(g.irange(D_1D, D_2)));
    if (g.chance(50))   /* the manual's evolving phase: a slow LFO on DETUNE */
        p.xmod(VCO1_FINE, W_SINE, T_LFO, g.range(RAW(lfo_hz(0.05)), RAW(lfo_hz(0.5))),
               g.range(RAW(norm(0.003)), RAW(norm(0.03))));
    if (g.chance(35))   /* shimmer on the waveform */
        p.xmod(VCO2_WAVE, W_TRI, T_LFO, g.range(RAW(lfo_hz(0.1)), RAW(lfo_hz(2))),
               g.range(RAW(norm(0.02)), RAW(norm(0.2))));
    if (g.chance(30))   /* the filter's colour drifts: low-pass towards band- or all-pass */
        p.xmod(COLOR, g.chance(50) ? W_SINE : W_TRI, T_LFO, g.range(RAW(lfo_hz(0.03)), RAW(lfo_hz(0.4))),
               g.range(RAW(norm(0.05)), RAW(norm(0.25))));
    if (g.chance(25) && untouched(p, VCO2_TUNE)) {   /* a little FM that glistens */
        static const unsigned kHigh[] = {8, 12, 16};
        op_fm(p, g, VCO2_TUNE, harmonic(st + iv, g.pick(kHigh)), RAW(semis(0.5)), RAW(semis(4)),
              RAW(xdec(1000)), RAW(xdec(5000)), RAW(semis(3)));
    }
    p.base(VOLUME, 0xA800, 0xC000);
}

void pluck(Patch &p, Gen &g)
{
    static const int kOct[] = {-24, -12, 0, 0, 12};
    const int st = g.pick(kOct);
    p.base(VCO1_WAVE, rich(g));
    p.base(VCO1_TUNE, tune(st));
    p.base(VCO1_VOL, 0x7000, 0xB000);
    switch (g.below(6)) {
    case 0: break;
    case 1:
        p.base(VCO2_WAVE, rich(g));
        p.base(VCO2_TUNE, tune(st + 12));
        p.base(VCO2_VOL, 0x4000, 0x8000);
        break;
    case 2:
        p.base(VCO2_WAVE, rich(g));
        p.base(VCO2_TUNE, tune(st + p.interval(st, kFifth)));
        p.base(VCO2_VOL, 0x4000, 0x8000);
        break;
    case 3:                                             /* an FM pluck */
        p.base(VCO2_WAVE, RAW(kSine), RAW(kTri));
        p.base(VCO2_TUNE, tune(st));
        p.bases(KBASES(VCO2_VOL, 0x7000, 0xA800, TZFM, 0x0800, 0x3000));
        p.xenv(TZFM, KXENV(0, 0, RAW(xdec(30)), RAW(xdec(300)), RAW(norm(0.2)), RAW(norm(0.8))));
        break;
    case 4:                                             /* a sync pluck */
        p.bases(KBASES(SYNC, 1, 1, VCO2_WAVE, RAW(kSaw), RAW(kSaw)));
        p.base(VCO2_TUNE, tune(st + g.irange(7, 24)));
        p.base(VCO2_VOL, 0x7000, 0xA800);
        p.xenv(VCO2_TUNE, KXENV(0, 0, RAW(xdec(40)), RAW(xdec(300)), RAW(semis(5)), RAW(semis(20))));
        break;
    default:
        p.base(VCO2_WAVE, rich(g));
        p.base(VCO2_TUNE, tune(st));
        p.bases(KBASES(VCO2_FINE, RAW(cents(4)), RAW(cents(20)), VCO2_VOL, 0x5000, 0x9000));
        break;
    }
    p.filter(KFILTER(RAW(cutoff(35)), RAW(cutoff(110)), 0, 0xB000, 0x6800, 0x8800, RAW(cuts(25)), RAW(cuts(110))));
    if (g.chance(25) && untouched(p, VCO1_TUNE)) {      /* an FM pluck on VCO1 */
        static const unsigned kR[] = {4, 8, 12, 16};
        op_fm(p, g, VCO1_TUNE, harmonic(st, g.pick(kR)), RAW(semis(4)), RAW(semis(20)),
              RAW(xdec(20)), RAW(xdec(200)), 0);
    }
    p.hit(RAW(env(60)), RAW(env(1200)));
    p.base(VOLUME, 0xB000, 0xC800);
}

void bell(Patch &p, Gen &g)
{
    static const int kOct[] = {-12, 0, 0, 12, 12, 24};
    const int st2 = g.pick(kOct);
    p.base(VCO2_WAVE, RAW(kSine), 0x1800);
    p.base(VCO2_TUNE, tune(st2));
    p.base(VCO2_VOL, 0x8800, 0xC000);
    const unsigned how = g.below(10);
    if (how >= 8) {
        /* an AM bell: VCO2's level swinging at a ratio of the note -- the
         * sidebands sit off the harmonics, gong-like */
        p.base(VCO1_VOL, 0);
        am(p, g, VCO2_VOL, T_RATIO, ratio_detent(g.chance(70) ? metallic(g) : harmonic(st2, 4u + g.below(28))),
           W_SINE, RAW(norm(0.4)), RAW(norm(0.95)), g.chance(60));
    } else if (how < 6) {
        /* VCO2 the sine carrier, VCO1 the modulator through TZFM, pinged by
         * an X-Env: the manual's "pinged FM sound" -- at any interval, off
         * the grid as much as the bell likes */
        p.base(VCO1_WAVE, RAW(kSine), RAW(kTri));
        p.base(VCO1_TUNE, tune(st2 + g.irange(-12, 40)));
        p.bases(KBASES(VCO1_FINE, RAW(cents(-90)), RAW(cents(90)), VCO1_VOL, 0, 0x2800, TZFM, 0, 0x5000));
        p.xenv(TZFM, KXENV(0, RAW(xatk(10)), RAW(xdec(150)), RAW(xdec(3000)), RAW(norm(0.1)), RAW(norm(0.9))));
    } else {
        /* a ratio from the irrational half of the table on VCO2's pitch */
        p.base(VCO1_VOL, 0);
        fm_index(p, g, 64u + g.below(40), RAW(xdec(100)), RAW(xdec(1500)), RAW(semis(2)), RAW(semis(14)));
    }
    p.filter(KFILTER(RAW(cutoff(115)), RAW(cutoff(149)), 0, 0x4000, kLp, kLp, kFenv0, kFenv0));
    p.hit(RAW(env(400)), RAW(env(6000)));
    p.base(VOLUME, 0xA800, 0xC000);
}

void zap(Patch &p, Gen &g)
{
    p.base(VCO1_WAVE, RAW(kSine), RAW(kSaw));
    p.base(VCO1_TUNE, tune(p.tone(-6, 36)));
    p.base(VCO1_VOL, 0x7000, 0xC000);
    if (g.chance(70))
        p.xenv(VCO1_TUNE, KXENV(0, 0, RAW(xdec(10)), RAW(xdec(400)), RAW(semis(12)), RAW(semis(72))));
    else    /* a dip that swoops back up */
        p.xenv(VCO1_TUNE, KXENV(0, 0, RAW(xdec(10)), RAW(xdec(400)), RAW(semis(-60)), RAW(semis(-12))));
    if (g.chance(40))
        p.filter(KFILTER(RAW(cutoff(50)), RAW(cutoff(100)), 0x8000, 0xE000, kLp, kLp, RAW(cuts(25)), RAW(cuts(90))));
    else
        p.filter(KFILTER(RAW(cutoff(100)), RAW(cutoff(149)), 0, 0x5000, kLp, kLp, kFenv0, kFenv0));
    p.hit(RAW(env(30)), RAW(env(600)));
    p.base(VOLUME, 0xB000, 0xC800);
}

void stab(Patch &p, Gen &g)
{
    static const int kInterval[] = {3, 4, 5, 7, 7, 8, 9, 10, 12, 14, 15, 16, 17};
    static const int kOct[] = {-24, -12, -12, 0, 0, 12};
    const int st = g.pick(kOct);
    p.base(VCO1_WAVE, rich(g));
    p.base(VCO1_TUNE, tune(st));
    p.base(VCO1_VOL, 0x6000, 0xA800);
    p.base(VCO2_WAVE, rich(g));
    p.base(VCO2_TUNE, tune(st + p.interval(st, kInterval)));
    p.base(VCO2_VOL, 0x6000, 0xA800);
    p.filter(KFILTER(RAW(cutoff(50)), RAW(cutoff(115)), 0, 0x9000, 0x6800, 0x8800, RAW(cuts(20)), RAW(cuts(100))));
    if (g.chance(35)) {     /* a 90s FM stab: both VCOs FM'd, the index falling */
        static const unsigned kR[] = {4, 8, 8, 12, 16};
        const int st2 = int(p.v[VCO2_TUNE][BASE] / 256) - 57;
        op_fm(p, g, VCO1_TUNE, harmonic(st, g.pick(kR)), RAW(semis(4)), RAW(semis(18)),
              RAW(xdec(50)), RAW(xdec(400)), 0);
        op_fm(p, g, VCO2_TUNE, harmonic(st2, g.pick(kR)), RAW(semis(4)), RAW(semis(18)),
              RAW(xdec(50)), RAW(xdec(400)), 0);
        p.base(CUTOFF, RAW(cutoff(90)), RAW(cutoff(130)));
    }
    p.adsr(KADSR(0, RAW(env(10)), RAW(env(60)), RAW(env(900)), 0, RAW(level(0.5)), RAW(env(40)), RAW(env(500))));
    p.base(VOLUME, 0xB000, 0xC800);
}

void glitch(Patch &p, Gen &g)
{
    /* the stock idea, bounded: a short sound with one to four randomly
     * modulated destinations, never silent */
    p.bases(KBASES(VCO1_WAVE, 0, 0xFFFF, VCO2_WAVE, 0, 0xFFFF));
    p.base(VCO1_TUNE, tune(g.irange(-30, 30)));
    p.base(VCO2_TUNE, tune(g.irange(-30, 30)));
    p.bases(KBASES(VCO1_VOL, 0x7000, 0xD000, VCO2_VOL, 0, 0xC000, TZFM, 0, 0xFFFF));
    if (g.chance(30))
        p.base(SYNC, 1);
    const unsigned color = g.range(0, 0xFFFF);
    /* a high-pass colour with the cutoff near the top would be silence */
    p.filter(RAW(cutoff(60)), color > 0xC000 ? RAW(cutoff(110)) : RAW(cutoff(145)), 0, 0xD000,
             color, color, RAW(cuts(-30)), RAW(cuts(60)));
    p.adsr(KADSR(0, RAW(env(10)), RAW(env(20)), RAW(env(800)), 0, RAW(level(0.7)), RAW(env(10)), RAW(env(400))));
    static const unsigned kDest[] = {VCO1_TUNE, VCO2_TUNE, CUTOFF, TZFM, VCO2_WAVE, VCO1_WAVE, VOLUME, RESO, COLOR};
    unsigned used = 0;
    for (unsigned n = 1 + g.below(4); n;) {
        const unsigned d = g.pick(kDest);
        if (used & (1u << d))
            continue;
        used |= 1u << d;
        --n;
        static const unsigned kWave[] = {W_SH, W_SH, W_SH, W_SQUARE, W_SAW, W_TRI, W_SINE};
        static const unsigned kType[] = {T_SYNCED, T_SYNCED, T_SYNCED, T_AUDIO, T_AUDIO, T_AUDIO,
                                         T_AUDIO, T_RATIO, T_RATIO, T_LFO};
        const unsigned type = g.pick(kType);
        unsigned amt;
        if (d == VCO1_TUNE || d == VCO2_TUNE)
            amt = g.range(RAW(semis(2)), RAW(semis(36)));
        else if (d == CUTOFF)
            amt = g.range(RAW(cuts(8)), RAW(cuts(60)));
        else
            amt = g.range(RAW(norm(0.05)), RAW(norm(0.6)));
        if (g.chance(50))
            amt = 0x10000u - amt;                       /* either polarity */
        const unsigned rate = type == T_LFO ? g.range(RAW(lfo_hz(2)), RAW(lfo_hz(10)))
                            : type == T_RATIO ? ratio_detent(g.below(128))
                            : type == T_SYNCED ? division(g.irange(D_4, D_32))
                            : g.range(0, 0xFFFF);
        p.xmod(d, g.pick(kWave), type, rate, amt);
        if (d != VOLUME && g.chance(40))
            p.xenv(d, KXENV(0, RAW(xatk(10)), RAW(xdec(5)), RAW(xdec(300)), 0x9000, 0xF000));
    }
    p.base(VOLUME, 0xA800, 0xC000);
}

/* ---- p39: more of the instrument ------------------------------------------ */

void cymbal(Patch &p, Gen &g)
{
    /* the hats' metal, longer: a ride keeps its ping, a crash is all wash */
    const bool ride = g.chance(45);
    metal(p, g, 18, 40, 2, 13);
    if (g.chance(50))
        four_op(p, g, RAW(semis(6)), RAW(semis(30)));
    if (ride) {
        p.noise(VCO2_TUNE, RAW(semis(5)), RAW(semis(50)));
        p.filter(KFILTER(RAW(cutoff(105)), RAW(cutoff(132)), 0x1000, 0x7000, 0xC000, 0xF000, kFenv0, kFenv0));
        p.hit(RAW(env(500)), RAW(env(2500)));
    } else {
        p.noise(VCO2_TUNE, RAW(semis(40)), RAW(semis(110)));
        p.filter(KFILTER(RAW(cutoff(98)), RAW(cutoff(130)), 0, 0x5000, 0xC800, 0xFFFF, kFenv0, RAW(cuts(15))));
        p.hit(RAW(env(1000)), RAW(env(5000)));
    }
    p.base(VOLUME, 0xA800, 0xC000);
}

void shaker(Patch &p, Gen &g)
{
    /* high-passed noise with a short swell instead of a hit */
    p.base(VCO1_WAVE, RAW(kSquare), RAW(kSaw));
    p.base(VCO1_TUNE, tune(g.irange(18, 40)));
    p.bases(KBASES(VCO1_VOL, 0x3000, 0x7800, VCO2_WAVE, RAW(kSquare), RAW(kSaw)));
    p.base(VCO2_TUNE, tune(g.irange(24, 44)));
    p.bases(KBASES(VCO2_VOL, 0x9000, 0xD800, TZFM, 0x4000, 0xD000));
    p.noise(VCO2_TUNE, RAW(semis(50)), RAW(semis(110)));
    p.filter(KFILTER(RAW(cutoff(112)), RAW(cutoff(142)), 0, 0x5000, 0xD000, 0xFFFF, kFenv0, kFenv0));
    p.adsr(KADSR(RAW(env(4)), RAW(env(25)), RAW(env(30)), RAW(env(150)), 0, 0, 0, 0));
    p.base(RELEASE, p.v[DECAY][BASE]);
    p.base(VOLUME, 0xA800, 0xC000);
}

void drop(Patch &p, Gen &g)
{
    /* the filter is the oscillator: at full resonance Mono's filter rings
     * as a sine, tuned like a VCO (FOLLOW on).  The X-Env sweeps it. */
    p.base(VCO1_WAVE, RAW(kSine), RAW(kTri));
    p.base(VCO1_TUNE, tune(g.irange(-12, 12)));
    p.base(VCO1_VOL, 0x0800, 0x3000);                  /* just enough to excite */
    p.filter(KFILTER(RAW(cutoff(85)), RAW(cutoff(125)), 0xF400, 0xFFFF, kLp, kLp, kFenv0, kFenv0));
    if (p.cx->scale) {
        /* the ringing filter's own pitch: coordinate c sounds MIDI c - 12,
         * and c = raw / 256 + (played - 57) with FOLLOW */
        const int m = p.cx->played + p.tone(73 - p.cx->played, 113 - p.cx->played);
        p.base(CUTOFF, unsigned(clamp16((m + 12 - (p.cx->played - 57)) * 256)));
    }
    if (g.chance(70))   /* a water drop: starts low, rises */
        p.xenv(CUTOFF, KXENV(0, 0, RAW(xdec(25)), RAW(xdec(250)), RAW(cuts(-36)), RAW(cuts(-8))));
    else                /* a ping that falls */
        p.xenv(CUTOFF, KXENV(0, 0, RAW(xdec(15)), RAW(xdec(150)), RAW(cuts(8)), RAW(cuts(36))));
    p.hit(RAW(env(40)), RAW(env(400)));
    p.base(VOLUME, 0xB000, 0xC800);
}

/* The FM voices put the carrier on VCO2 -- TZFM modulates VCO2 by VCO1, so
 * that is Mono's FM architecture -- and silence VCO1 (its default level is
 * up).  The X-Mod at a ratio of the note on VCO2's pitch, its depth carried
 * by an X-Env routed to the X-Mod amount (FUNC+TYPE+WAVE): a DX operator
 * with an envelope on its index. */
void fm_index(Patch &p, Gen &g, unsigned detent, unsigned d_lo, unsigned d_hi,
              unsigned a_lo, unsigned a_hi)
{
    p.xmod(VCO2_TUNE, g.chance(80) ? W_SINE : W_TRI, T_RATIO, ratio_detent(detent), 0x8000);
    p.set(VCO2_TUNE, XENV_DEST, 1);
    p.xenv(VCO2_TUNE, 0, 0, d_lo, d_hi, a_lo, a_hi);
}

/* p52 (D#+F# = FM basses only, and a deeper FM bass): a third DX, a third analog TZFM, a third both; the DX index always
 * stays under its envelope (p51 left it at 0 half the time, so the FM was
 * the attack only), now and then swells in; more ratios; the TZFM growl
 * deeper, four operators half the time, and it can move with the tempo. */
void fm_bass(Patch &p, Gen &g)
{
    static const int kOct[] = {-36, -24, -24, -24, -12, -12};
    const int st = g.pick(kOct);
    p.bases(KBASES(VCO1_VOL, 0, 0, VCO2_WAVE, RAW(kSine), 0x4000));
    p.base(VCO2_TUNE, tune(st));
    p.base(VCO2_VOL, 0x9000, 0xC800);
    const unsigned how = g.below(3);
    if (how < 2u) {
        /* a ratio X-Mod on the carrier's pitch: modulator 0.5..7 times the
         * carrier (the ratio is taken against the played note, which sits
         * `-st` semitones above it), a quarter of the time an irrational one */
        unsigned detent;
        if (g.chance(25)) {
            detent = 64u + g.below(24);
        } else {
            static const uint8_t kMul2[] = {1, 2, 2, 3, 4, 4, 6, 8, 10, 14};  /* x2: 0.5 .. 7 */
            const unsigned octaves = unsigned(-st) / 12u;           /* 1, 2 or 3 */
            /* modulator = note * (d + 1) / 4 = carrier * 2^octaves * (d + 1) / 4 */
            const unsigned want4 = (g.pick(kMul2) * 4u) >> (octaves + 1u);   /* (d + 1) */
            detent = want4 ? want4 - 1u : 0u;
        }
        /* bench: an index under ~10 st left the fundamental on top --
         * a sine bass with a little fizz; this one growls */
        fm_index(p, g, detent, RAW(xdec(20)), RAW(xdec(1500)), RAW(semis(10)), RAW(semis(40)));
        p.set(VCO2_TUNE, XMOD_AMT, g.range(RAW(semis(3)), RAW(semis(16))));
        if (g.chance(30))   /* the index swells in */
            p.set(VCO2_TUNE, XENV_ATK, g.range(RAW(xatk(40)), RAW(xatk(300))));
    }
    if (how >= 1u) {
        /* analog thru-zero FM: VCO1 the modulator a musical interval away */
        static const int kMod[] = {0, 0, 12, 12, 19, 24, 7, -12, 28};
        const int mst = st + g.pick(kMod);
        p.base(VCO1_WAVE, RAW(kSine), RAW(kTri));
        p.base(VCO1_TUNE, tune(mst));
        p.bases(KBASES(VCO1_FINE, RAW(cents(-15)), RAW(cents(15)), VCO1_VOL, 0, 0x2000, TZFM, 0x1000, 0x7000));
        p.xenv(TZFM, KXENV(0, 0, RAW(xdec(20)), RAW(xdec(1000)), RAW(norm(0.35)), RAW(norm(0.95))));
        if (g.chance(50))   /* and the modulator FM'd itself: four operators */
            op_fm(p, g, VCO1_TUNE, harmonic(mst, g.chance(50) ? 4 : 8), RAW(semis(6)), RAW(semis(30)),
                  RAW(xdec(20)), RAW(xdec(600)), RAW(semis(10)));
        if (g.chance(40)) { /* the growl moves with the tempo: every step its own, or a wobble */
            static const uint8_t kMove[] = {W_SH, W_SH, W_SINE, W_TRI};
            p.xmod(TZFM, g.pick(kMove), T_SYNCED, division(g.irange(D_4, D_16)),
                   g.range(RAW(norm(0.08)), RAW(norm(0.35))));
        }
    }
    /* FM makes the timbre: the filter stays open enough to let it through */
    p.filter(KFILTER(RAW(cutoff(100)), RAW(cutoff(149)), 0, 0x7000, kLp, kLp, kFenv0, RAW(cuts(50))));
    p.adsr(KADSR(0, RAW(env(5)), RAW(env(100)), RAW(env(1200)), 0, RAW(level(0.9)), RAW(env(20)), RAW(env(300))));
    p.base(VOLUME, 0xB000, 0xC800);
}

void fm_ep(Patch &p, Gen &g)
{
    /* an electric piano: 1:1 TZFM for the bark, a 14:1 (or 7:1) tine */
    const bool low = g.chance(30);
    p.base(VCO2_WAVE, RAW(kSine), 0x1000);
    p.base(VCO2_TUNE, tune(low ? -12 : 0));
    p.bases(KBASES(VCO2_VOL, 0x9000, 0xB800, VCO1_WAVE, RAW(kSine), RAW(kTri)));
    p.base(VCO1_TUNE, tune((low ? -12 : 0) + (g.chance(80) ? 0 : 12)));
    p.bases(KBASES(VCO1_VOL, 0, 0x1800, TZFM, 0x0400, 0x2800));
    p.xenv(TZFM, KXENV(0, 0, RAW(xdec(200)), RAW(xdec(1500)), RAW(norm(0.08)), RAW(norm(0.5))));
    fm_index(p, g, low ? (g.chance(50) ? 27u : 13u) : (g.chance(50) ? 55u : 27u),
             RAW(xdec(15)), RAW(xdec(120)), RAW(semis(1.5)), RAW(semis(9)));
    p.filter(KFILTER(RAW(cutoff(100)), RAW(cutoff(145)), 0, 0x3000, kLp, kLp, kFenv0, RAW(cuts(15))));
    p.adsr(KADSR(0, 0, RAW(env(700)), RAW(env(3500)), 0, RAW(level(0.35)), RAW(env(200)), RAW(env(1200))));
    if (g.chance(40))       /* the suitcase's tremolo */
        p.xmod(VOLUME, W_SINE, T_LFO, g.range(RAW(lfo_hz(2.5)), RAW(lfo_hz(7))),
               g.range(RAW(norm(0.05)), RAW(norm(0.2))));
    p.base(VOLUME, 0xA800, 0xC000);
}

void mallet(Patch &p, Gen &g)
{
    static const int kOct[] = {-12, 0, 0, 12, 12, 24};
    const int st = g.pick(kOct);
    p.bases(KBASES(VCO1_VOL, 0, 0, VCO2_WAVE, RAW(kSine), 0x1000));
    p.base(VCO2_TUNE, tune(st));
    p.base(VCO2_VOL, 0x9000, 0xC000);
    switch (g.below(4)) {
    case 0:     /* marimba: a 4:1 knock, gone almost at once */
        fm_index(p, g, g.chance(70) ? 15u : 11u, RAW(xdec(6)), RAW(xdec(50)), RAW(semis(3)), RAW(semis(14)));
        p.hit(RAW(env(200)), RAW(env(900)));
        break;
    case 1: {   /* kalimba: an inharmonic overtone, a little longer */
        fm_index(p, g, 76u + g.below(16), RAW(xdec(10)), RAW(xdec(80)), RAW(semis(2)), RAW(semis(10)));
        p.hit(RAW(env(300)), RAW(env(1500)));
        break;
    }
    case 2:     /* vibraphone / glockenspiel: 3.5:1 or root 2, with tremolo */
        fm_index(p, g, g.chance(50) ? 13u : 67u, RAW(xdec(10)), RAW(xdec(80)),
                 RAW(semis(1.5)), RAW(semis(8)));
        p.hit(RAW(env(500)), RAW(env(2500)));
        if (g.chance(60))
            p.xmod(VOLUME, W_SINE, T_LFO, g.range(RAW(lfo_hz(3)), RAW(lfo_hz(7))),
                   g.range(RAW(norm(0.05)), RAW(norm(0.2))));
        break;
    default:    /* steel drum: a 2:1 with a slow index and a soft attack */
        fm_index(p, g, g.chance(50) ? 7u : 11u, RAW(xdec(60)), RAW(xdec(300)), RAW(semis(2)), RAW(semis(8)));
        p.adsr(KADSR(RAW(env(3)), RAW(env(15)), RAW(env(300)), RAW(env(1200)), 0, 0, 0, 0));
        p.base(RELEASE, p.v[DECAY][BASE]);
        break;
    }
    p.filter(KFILTER(RAW(cutoff(110)), RAW(cutoff(149)), 0, 0x3000, kLp, kLp, kFenv0, kFenv0));
    p.base(VOLUME, 0xA800, 0xC000);
}

void reese(Patch &p, Gen &g)
{
    /* two saws a few cents apart, the beat itself drifting: a slow LFO on
     * VCO2's DETUNE; both loud enough to drive the filter */
    const int st = g.chance(70) ? -24 : -12;
    p.base(VCO1_WAVE, g.chance(70) ? RAW(kSaw) : g.range(RAW(kSquare), RAW(kSaw)));
    p.base(VCO1_TUNE, tune(st));
    p.base(VCO1_VOL, 0x9000, 0xD000);
    p.base(VCO2_WAVE, g.chance(70) ? RAW(kSaw) : g.range(RAW(kSquare), RAW(kSaw)));
    p.base(VCO2_TUNE, tune(st));
    const bool up = g.chance(50);
    p.base(VCO2_FINE, up ? RAW(cents(5)) : RAW(cents(-45)), up ? RAW(cents(45)) : RAW(cents(-5)));
    p.base(VCO2_VOL, 0x9000, 0xD000);
    p.xmod(VCO2_FINE, W_SINE, T_LFO, g.range(RAW(lfo_hz(0.05)), RAW(lfo_hz(1.5))),
           g.range(RAW(norm(0.01)), RAW(norm(0.12))));
    p.filter(KFILTER(RAW(cutoff(55)), RAW(cutoff(120)), 0, 0x6000, 0x6800, 0x8800, kFenv0, RAW(cuts(30))));
    if (g.chance(30))
        p.xmod(CUTOFF, W_SINE, T_LFO, g.range(RAW(lfo_hz(0.05)), RAW(lfo_hz(0.5))),
               g.range(RAW(cuts(5)), RAW(cuts(20))));
    p.adsr(KADSR(0, RAW(env(20)), RAW(env(300)), RAW(env(2000)), RAW(level(0.5)), RAW(level(1.0)), RAW(env(30)), RAW(env(400))));
    p.base(VOLUME, 0xB000, 0xC800);
}

void wobble(Patch &p, Gen &g)
{
    /* a synced LFO on the cutoff: the wobble locks to the tempo */
    p.base(VCO1_WAVE, rich(g));
    p.base(VCO1_TUNE, tune(g.chance(80) ? -24 : -12));
    p.base(VCO1_VOL, 0x8000, 0xD000);
    switch (g.below(3)) {
    case 0: {
        p.base(VCO2_WAVE, RAW(kSaw));
        p.base(VCO2_TUNE, tune(-24));
        const bool up = g.chance(50);
        p.base(VCO2_FINE, up ? RAW(cents(5)) : RAW(cents(-30)), up ? RAW(cents(30)) : RAW(cents(-5)));
        p.base(VCO2_VOL, 0x7000, 0xB800);
        break;
    }
    case 1:
        p.base(VCO2_WAVE, RAW(kSquare), RAW(kSaw));
        p.base(VCO2_TUNE, tune(-36));
        p.base(VCO2_VOL, 0x4000, 0x8800);
        break;
    default:
        p.base(VCO2_VOL, 0);
        break;
    }
    /* low enough to close, high enough that the wobble opens it (bench: a
     * base of 30 left the filter shut most of the cycle) */
    p.filter(KFILTER(RAW(cutoff(55)), RAW(cutoff(85)), 0x3000, 0xD000, kLp, kLp, kFenv0, kFenv0));
    static const unsigned kDiv[] = {D_2, D_4, D_4, D_4T, D_8, D_8, D_8T, D_16, D_16, D_32};
    static const unsigned kWave[] = {W_SINE, W_SINE, W_SINE, W_TRI, W_TRI, W_SAW, W_SQUARE, W_SH};
    const unsigned div = division(g.pick(kDiv));
    switch (g.below(6)) {
    case 0:     /* the colour wobbles, low-pass to band-pass */
        p.base(COLOR, 0x5000, 0x8000);
        p.xmod(COLOR, g.pick(kWave), T_SYNCED, div, g.range(RAW(norm(0.15)), RAW(norm(0.35))));
        p.base(CUTOFF, RAW(cutoff(60)), RAW(cutoff(95)));
        break;
    case 1:     /* the waveform wobbles */
        p.xmod(VCO1_WAVE, g.pick(kWave), T_SYNCED, div, g.range(RAW(norm(0.2)), RAW(norm(0.5))));
        p.xmod(CUTOFF, g.pick(kWave), T_SYNCED, div, g.range(RAW(cuts(10)), RAW(cuts(40))));
        break;
    default:
        p.xmod(CUTOFF, g.pick(kWave), T_SYNCED, div, g.range(RAW(cuts(25)), RAW(cuts(70))));
        break;
    }
    if (g.chance(30) && untouched(p, RESO))     /* the resonance with it */
        p.xmod(RESO, g.pick(kWave), T_SYNCED, div, g.range(RAW(norm(0.1)), RAW(norm(0.3))));
    p.adsr(KADSR(0, 0, RAW(env(400)), RAW(env(2500)), RAW(level(0.75)), RAW(level(1.0)), RAW(env(20)), RAW(env(200))));
    p.base(VOLUME, 0xB000, 0xC800);
}

void growl(Patch &p, Gen &g)
{
    /* filter FM (the manual's recipe): an audio-rate X-Mod on the cutoff,
     * its depth swelling and falling with an X-Env -- "yoi" */
    const int st = g.chance(70) ? -24 : -12;
    p.base(VCO1_WAVE, rich(g));
    p.base(VCO1_TUNE, tune(st));
    p.base(VCO1_VOL, 0x8000, 0xD000);
    if (g.chance(40)) {
        p.base(VCO2_WAVE, RAW(kSquare), RAW(kSaw));
        p.base(VCO2_TUNE, tune(st - 12));
        p.base(VCO2_VOL, 0x4000, 0x8800);
    }
    p.filter(KFILTER(RAW(cutoff(45)), RAW(cutoff(100)), 0x2000, 0xC000, kLp, kLp, kFenv0, RAW(cuts(20))));
    static const unsigned kWave[] = {W_SINE, W_TRI, W_SAW, W_SQUARE};
    if (g.chance(50))       /* a ratio: the growl's timbre follows the note */
        p.xmod(CUTOFF, g.pick(kWave), T_RATIO, ratio_detent(harmonic(st, g.chance(50) ? 4 : 8)), 0x8000);
    else
        p.xmod(CUTOFF, g.pick(kWave), T_AUDIO, audio_st(g.irange(-24, 24)), 0x8000);
    if (g.chance(40))
        p.set(CUTOFF, XMOD_AMT, g.range(RAW(cuts(3)), RAW(cuts(20))));
    p.set(CUTOFF, XENV_DEST, 1);
    p.xenv(CUTOFF, KXENV(RAW(xatk(5)), RAW(xatk(400)), RAW(xdec(100)), RAW(xdec(1500)), RAW(cuts(10)), RAW(cuts(70))));
    p.adsr(KADSR(0, RAW(env(10)), RAW(env(200)), RAW(env(1200)), RAW(level(0.3)), RAW(level(0.9)), RAW(env(30)), RAW(env(300))));
    p.base(VOLUME, 0xB000, 0xC800);
}

void sync_lead(Patch &p, Gen &g)
{
    /* VCO2 hard-synced to VCO1 and swept: the sync timbre is VCO2's, so
     * VCO1 (the master) stays quiet */
    static const int kOct[] = {-24, -12, -12, 0, 0, 12};
    const int st = g.pick(kOct);
    p.bases(KBASES(SYNC, 1, 1, VCO1_WAVE, RAW(kTri), RAW(kSaw)));
    p.base(VCO1_TUNE, tune(st));
    p.bases(KBASES(VCO1_VOL, 0, 0x4000, VCO2_WAVE, RAW(kSquare), RAW(kSaw)));
    p.base(VCO2_TUNE, tune(st + g.irange(3, 36)));
    p.base(VCO2_VOL, 0x8800, 0xC000);
    if (g.chance(60)) {
        const bool up = g.chance(80);
        p.xenv(VCO2_TUNE, 0, RAW(xatk(30)), RAW(xdec(80)), RAW(xdec(1500)),
               up ? RAW(semis(5)) : RAW(semis(-24)), up ? RAW(semis(36)) : RAW(semis(-5)));
    }
    else
        p.xmod(VCO2_TUNE, g.chance(50) ? W_TRI : W_SINE, g.chance(70) ? T_LFO : T_SYNCED,
               g.range(RAW(lfo_hz(0.1)), RAW(lfo_hz(6))), g.range(RAW(semis(2)), RAW(semis(14))));
    p.filter(KFILTER(RAW(cutoff(85)), RAW(cutoff(145)), 0, 0x6000, 0x6800, 0x8800, kFenv0, RAW(cuts(30))));
    p.adsr(KADSR(0, RAW(env(30)), RAW(env(150)), RAW(env(1500)), RAW(level(0.4)), RAW(level(1.0)), RAW(env(30)), RAW(env(500))));
    p.base(VOLUME, 0xA800, 0xC000);
}

void hoover(Patch &p, Gen &g)
{
    /* the rave hoover: detuned saws scooping up into the note, and the
     * wave moving between square and saw (a pulse-width sweep, near
     * enough) */
    static const int kInterval[] = {0, 0, -12, 12};
    const int st = g.chance(50) ? -12 : 0;
    const int iv = g.pick(kInterval);
    p.base(VCO1_WAVE, 0xB800, 0xF000);
    p.base(VCO1_TUNE, tune(st));
    p.bases(KBASES(VCO1_VOL, 0x7800, 0xB000, VCO2_WAVE, 0xC000, 0xFFFF));
    p.base(VCO2_TUNE, tune(st + iv));
    if (iv == 0) {
        const bool up = g.chance(50);
        p.base(VCO2_FINE, up ? RAW(cents(10)) : RAW(cents(-45)), up ? RAW(cents(45)) : RAW(cents(-10)));
    }
    p.base(VCO2_VOL, 0x7800, 0xB000);
    p.xenv(VCO1_TUNE, KXENV(0, 0, RAW(xdec(60)), RAW(xdec(600)), RAW(semis(-12)), RAW(semis(-2))));
    p.xenv_like(VCO2_TUNE, VCO1_TUNE);
    p.xmod(VCO1_WAVE, g.chance(50) ? W_SINE : W_TRI, T_LFO, g.range(RAW(lfo_hz(1)), RAW(lfo_hz(8))),
           g.range(RAW(norm(0.04)), RAW(norm(0.2))));
    p.filter(KFILTER(RAW(cutoff(95)), RAW(cutoff(140)), 0, 0x5000, kLp, kLp, kFenv0, RAW(cuts(15))));
    p.adsr(KADSR(0, RAW(env(30)), RAW(env(300)), RAW(env(1800)), RAW(level(0.6)), RAW(level(1.0)), RAW(env(100)), RAW(env(800))));
    p.base(VOLUME, 0xA800, 0xC000);
}

void phaser_pad(Patch &p, Gen &g)
{
    /* the colour half way between low- and high-pass is a resonant
     * all-pass; a slow LFO on its cutoff sweeps the notches: a phaser */
    static const int kOct[] = {-24, -12, -12, 0, 0};
    const int st = g.pick(kOct);
    p.base(VCO1_WAVE, g.chance(70) ? g.pick(kSoft) : rich(g));
    p.base(VCO1_TUNE, tune(st));
    p.base(VCO1_VOL, 0x5000, 0x9800);
    p.base(VCO2_WAVE, g.chance(70) ? g.pick(kSoft) : rich(g));
    static const int kInterval[] = {0, 0, 7, 12, -12, 5, 19};
    p.base(VCO2_TUNE, tune(st + p.interval(st, kInterval)));
    p.bases(KBASES(VCO2_FINE, RAW(cents(-30)), RAW(cents(30)), VCO2_VOL, 0x5000, 0x9800));
    p.filter(KFILTER(RAW(cutoff(70)), RAW(cutoff(120)), 0x4000, 0xC000, 0xB000, 0xD000, kFenv0, kFenv0));
    p.xmod(CUTOFF, g.chance(50) ? W_SINE : W_TRI, T_LFO, g.range(RAW(lfo_hz(0.03)), RAW(lfo_hz(1))),
           g.range(RAW(cuts(8)), RAW(cuts(40))));
    p.adsr(KADSR(RAW(env(80)), RAW(env(2500)), RAW(env(1000)), RAW(env(5000)), RAW(level(0.6)), RAW(level(1.0)), RAW(env(500)), RAW(env(4500))));
    p.base(VOLUME, 0xA800, 0xC000);
}

void gate(Patch &p, Gen &g)
{
    /* a sustained chord chopped by a synced square (or ramp, or random
     * levels) on the volume */
    static const int kInterval[] = {3, 4, 5, 7, 7, 12, 10, 9};
    static const int kOct[] = {-12, -12, 0, 0, 12};
    const int st = g.pick(kOct);
    p.base(VCO1_WAVE, rich(g));
    p.base(VCO1_TUNE, tune(st));
    p.base(VCO1_VOL, 0x6000, 0xA000);
    p.base(VCO2_WAVE, rich(g));
    p.base(VCO2_TUNE, tune(st + p.interval(st, kInterval)));
    p.base(VCO2_VOL, 0x6000, 0xA000);
    p.filter(KFILTER(RAW(cutoff(70)), RAW(cutoff(130)), 0, 0x7000, 0x6800, 0x8800, kFenv0, RAW(cuts(25))));
    static const unsigned kDiv[] = {D_4, D_8, D_8, D_8T, D_16, D_16, D_16, D_32};
    static const unsigned kWave[] = {W_SQUARE, W_SQUARE, W_SQUARE, W_SQUARE, W_SAW, W_SH};
    p.xmod(VOLUME, g.pick(kWave), T_SYNCED, division(g.pick(kDiv)), g.range(RAW(norm(0.7)), RAW(norm(1.0))));
    if (g.chance(30))
        humanize(p, g, CUTOFF, RAW(cuts(5)), RAW(cuts(20)));
    p.adsr(KADSR(0, RAW(env(40)), RAW(env(1000)), RAW(env(4000)), RAW(level(0.8)), RAW(level(1.0)), RAW(env(60)), RAW(env(600))));
    p.base(VOLUME, 0xA000, 0xB800);
}

void bleeps(Patch &p, Gen &g)
{
    /* computer bleeps: a synced sample & hold on the pitch (and the
     * resonant cutoff), a new random note on every division */
    static const unsigned kDiv[] = {D_16, D_16, D_8, D_8T, D_32};
    const unsigned div = division(g.pick(kDiv));
    p.base(VCO1_WAVE, g.chance(50) ? RAW(kSine) : g.range(RAW(kTri), RAW(kSaw)));
    p.base(VCO1_TUNE, tune(g.chance(50) ? 0 : g.chance(50) ? 12 : 24));
    p.base(VCO1_VOL, 0x7000, 0xB000);
    p.xmod(VCO1_TUNE, W_SH, T_SYNCED, div, g.range(RAW(semis(2)), RAW(semis(24))));
    if (g.chance(50)) {
        p.filter(KFILTER(RAW(cutoff(70)), RAW(cutoff(115)), 0x5000, 0xC800, kLp, kLp, kFenv0, kFenv0));
        p.xmod(CUTOFF, W_SH, T_SYNCED, div, g.range(RAW(cuts(10)), RAW(cuts(45))));
    } else {
        p.filter(KFILTER(RAW(cutoff(100)), RAW(cutoff(145)), 0, 0x4000, kLp, kLp, kFenv0, kFenv0));
    }
    if (g.chance(30))       /* metallic: AM at an irrational ratio */
        am(p, g, VCO1_VOL, T_RATIO, ratio_detent(metallic(g)), W_SINE, RAW(norm(0.3)), RAW(norm(0.7)), false);
    p.adsr(KADSR(0, 0, RAW(env(500)), RAW(env(2500)), RAW(level(0.5)), RAW(level(1.0)), RAW(env(30)), RAW(env(300))));
    p.base(VOLUME, 0xA800, 0xC000);
}

void riser(Patch &p, Gen &g)
{
    /* noise and a saw swept by X-Envs that take seconds (an X-Env always
     * completes its attack, whatever the gate does) */
    p.base(VCO1_WAVE, RAW(kTri), RAW(kSaw));
    p.base(VCO1_TUNE, tune(g.chance(50) ? 0 : 12));
    p.bases(KBASES(VCO1_VOL, 0x3000, 0x8000, VCO2_WAVE, RAW(kSaw), RAW(kSaw)));
    p.base(VCO2_TUNE, tune(g.irange(6, 30)));
    p.base(VCO2_VOL, 0x8000, 0xC800);
    p.noise(VCO2_TUNE, RAW(semis(30)), RAW(semis(110)));
    p.bases(KBASES(CUTOFF, RAW(cutoff(40)), RAW(cutoff(80)), RESO, 0x2000, 0xA000, COLOR, 0x2000, 0x8800, FENV, kFenv0, kFenv0));
    if (g.chance(60)) {                 /* up */
        p.xenv(VCO1_TUNE, KXENV(RAW(xatk(500)), RAW(xatk(6000)), RAW(xdec(200)), RAW(xdec(1500)), RAW(semis(5)), RAW(semis(36))));
        p.xenv(CUTOFF, p.v[VCO1_TUNE][XENV_ATK], p.v[VCO1_TUNE][XENV_ATK],
               RAW(xdec(200)), RAW(xdec(1500)), RAW(cuts(30)), RAW(cuts(100)));
        p.adsr(KADSR(RAW(env(500)), RAW(env(4000)), RAW(env(2000)), RAW(env(6000)), RAW(level(1.0)), RAW(level(1.0)), RAW(env(200)), RAW(env(1500))));
    } else {                            /* down: starts at the top, falls */
        p.xenv(VCO1_TUNE, KXENV(0, 0, RAW(xdec(600)), RAW(xdec(4500)), RAW(semis(5)), RAW(semis(36))));
        p.xenv(CUTOFF, 0, 0, p.v[VCO1_TUNE][XENV_DEC], p.v[VCO1_TUNE][XENV_DEC],
               RAW(cuts(30)), RAW(cuts(100)));
        p.hit(RAW(env(1000)), RAW(env(5000)));
    }
    p.base(VOLUME, 0xA800, 0xC000);
}

void siren(Patch &p, Gen &g)
{
    p.base(VCO1_WAVE, RAW(kSine), RAW(kSaw));
    p.base(VCO1_TUNE, tune(g.irange(-6, 30)));
    p.base(VCO1_VOL, 0x7000, 0xB800);
    if (g.chance(60)) {     /* siren: a slow sweep of a few semitones */
        p.xmod(VCO1_TUNE, g.chance(60) ? W_TRI : W_SINE, T_LFO,
               g.range(RAW(lfo_hz(0.4)), RAW(lfo_hz(4))), g.range(RAW(semis(3)), RAW(semis(18))));
    } else {                /* UFO: a fast warble and some TZFM shimmer */
        p.xmod(VCO1_TUNE, W_SINE, T_LFO, g.range(RAW(lfo_hz(3)), RAW(lfo_hz(10))),
               g.range(RAW(semis(0.5)), RAW(semis(6))));
        p.base(VCO2_WAVE, RAW(kSine), RAW(kTri));
        p.base(VCO2_TUNE, tune(g.irange(0, 30)));
        p.bases(KBASES(VCO2_VOL, 0x4000, 0x8800, TZFM, 0x1000, 0x8000));
        p.xmod(TZFM, W_SINE, T_LFO, g.range(RAW(lfo_hz(0.1)), RAW(lfo_hz(2))),
               g.range(RAW(norm(0.05)), RAW(norm(0.3))));
    }
    p.filter(KFILTER(RAW(cutoff(100)), RAW(cutoff(149)), 0, 0x6000, kLp, kLp, kFenv0, kFenv0));
    p.adsr(KADSR(0, RAW(env(60)), RAW(env(800)), RAW(env(4000)), RAW(level(0.7)), RAW(level(1.0)), RAW(env(100)), RAW(env(1500))));
    p.base(VOLUME, 0xA800, 0xC000);
}

void drone(Patch &p, Gen &g)
{
    /* low, sustained, and never quite the same: slow LFOs on the TZFM,
     * VCO2's waveform and the cutoff, at unrelated rates */
    static const int kOct[] = {-36, -24, -24, -12, -12, 0};
    const int st = g.pick(kOct);
    static const int kInterval[] = {0, 7, 12, 19, 5, 24};
    p.base(VCO1_WAVE, g.range(RAW(kSine), RAW(kSaw)));
    p.base(VCO1_TUNE, tune(st));
    p.base(VCO1_VOL, 0x5000, 0x9800);
    p.base(VCO2_WAVE, g.range(RAW(kTri), RAW(kSaw)));
    p.base(VCO2_TUNE, tune(st + p.interval(st, kInterval)));
    p.bases(KBASES(VCO2_FINE, RAW(cents(-25)), RAW(cents(25)), VCO2_VOL, 0x5000, 0x9800, TZFM, 0, 0x5000));
    p.xmod(TZFM, W_SINE, T_LFO, g.range(RAW(lfo_hz(0.02)), RAW(lfo_hz(0.5))),
           g.range(RAW(norm(0.03)), RAW(norm(0.3))));
    p.xmod(VCO2_WAVE, W_TRI, T_LFO, g.range(RAW(lfo_hz(0.03)), RAW(lfo_hz(0.6))),
           g.range(RAW(norm(0.03)), RAW(norm(0.3))));
    p.filter(KFILTER(RAW(cutoff(55)), RAW(cutoff(120)), 0, 0x9000, 0x4000, 0x8800, kFenv0, kFenv0));
    p.xmod(CUTOFF, W_SINE, T_LFO, g.range(RAW(lfo_hz(0.01)), RAW(lfo_hz(0.3))),
           g.range(RAW(cuts(5)), RAW(cuts(35))));
    if (g.chance(30) && untouched(p, COLOR))
        p.xmod(COLOR, W_SINE, T_LFO, g.range(RAW(lfo_hz(0.02)), RAW(lfo_hz(0.3))),
               g.range(RAW(norm(0.05)), RAW(norm(0.25))));
    if (g.chance(30) && untouched(p, VCO1_TUNE)) {
        static const unsigned kR[] = {2, 4, 8, 12};
        op_fm(p, g, VCO1_TUNE, harmonic(st, g.pick(kR)), RAW(semis(1)), RAW(semis(6)),
              RAW(xdec(1000)), RAW(xdec(5000)), RAW(semis(4)));
    }
    p.adsr(KADSR(RAW(env(300)), RAW(env(4000)), RAW(env(3000)), RAW(env(6000)), RAW(level(0.8)), RAW(level(1.0)), RAW(env(1000)), RAW(env(6000))));
    p.base(VOLUME, 0xA800, 0xC000);
}

/* ---- p43: more of the instrument ------------------------------------------ */

void metal_perc(Patch &p, Gen &g)
{
    /* struck metal -- cans, sheet, gongs: two VCOs each FM'd at an
     * irrational ratio, VCO1 into VCO2 through the TZFM, sometimes with the
     * index falling */
    const int st = p.tone(-12, 24);
    p.base(VCO1_WAVE, RAW(kSine), RAW(kSquare));
    p.base(VCO1_TUNE, tune(st));
    p.bases(KBASES(VCO1_VOL, 0x4000, 0xA000, VCO2_WAVE, RAW(kSine), RAW(kSquare)));
    p.base(VCO2_TUNE, tune(st + g.irange(-7, 24)));
    p.bases(KBASES(VCO2_FINE, RAW(cents(-60)), RAW(cents(60)), VCO2_VOL, 0x6000, 0xB000, TZFM, 0x2000, 0xC000));
    if (g.chance(50)) {
        four_op(p, g, RAW(semis(4)), RAW(semis(28)));
    } else {
        op_fm(p, g, VCO1_TUNE, metallic(g), RAW(semis(8)), RAW(semis(36)), RAW(xdec(30)), RAW(xdec(800)), RAW(semis(8)));
        op_fm(p, g, VCO2_TUNE, metallic(g), RAW(semis(8)), RAW(semis(36)), RAW(xdec(30)), RAW(xdec(800)), RAW(semis(8)));
    }
    const unsigned colour = g.chance(60) ? g.range(0, 0x3000) : g.range(0x6000, 0xC000);
    p.filter(RAW(cutoff(90)), RAW(cutoff(140)), 0x1000, 0x8000, colour, colour, kFenv0, RAW(cuts(15)));
    p.hit(RAW(env(40)), RAW(env(2500)));
    p.base(VOLUME, 0xB000, 0xC800);
}

void am_bell(Patch &p, Gen &g)
{
    /* ring-modulated bells: a sine whose level swings at a ratio of the
     * note -- harmonic or not -- the swing fading with the note */
    static const int kOct[] = {-12, 0, 12, 12, 24};
    const int st = g.pick(kOct);
    p.bases(KBASES(VCO1_VOL, 0, 0, VCO2_WAVE, RAW(kSine), 0x1800));
    p.base(VCO2_TUNE, tune(st));
    p.base(VCO2_VOL, 0x9000, 0xC800);
    const unsigned detent = g.chance(60) ? metallic(g) : harmonic(st, 4u + g.below(44));
    am(p, g, VCO2_VOL, T_RATIO, ratio_detent(detent), g.chance(80) ? W_SINE : W_TRI,
       RAW(norm(0.5)), RAW(norm(1.0)), g.chance(50));
    if (g.chance(40)) {     /* and a TZFM ping on top */
        p.base(VCO1_WAVE, RAW(kSine));
        p.base(VCO1_TUNE, tune(st + g.irange(5, 31)));
        p.base(TZFM, 0, 0x1800);
        p.xenv(TZFM, KXENV(0, 0, RAW(xdec(50)), RAW(xdec(800)), RAW(norm(0.1)), RAW(norm(0.6))));
    }
    p.filter(KFILTER(RAW(cutoff(110)), RAW(cutoff(149)), 0, 0x3000, kLp, kLp, kFenv0, kFenv0));
    p.hit(RAW(env(300)), RAW(env(4500)));
    p.base(VOLUME, 0xA800, 0xC000);
}

void talk(Patch &p, Gen &g)
{
    /* a talking, vowel-ish voice: saws into a resonant band-pass whose
     * cutoff and colour move -- a wah, a "yoi", a mouth */
    static const int kOct[] = {-24, -12, -12, 0};
    const int st = g.pick(kOct);
    p.base(VCO1_WAVE, RAW(kSquare), RAW(kSaw));
    p.base(VCO1_TUNE, tune(st));
    p.bases(KBASES(VCO1_VOL, 0x8000, 0xC000, VCO2_WAVE, RAW(kSaw), RAW(kSaw)));
    p.base(VCO2_TUNE, tune(st + (g.chance(50) ? 0 : 12)));
    p.bases(KBASES(VCO2_FINE, RAW(cents(-12)), RAW(cents(12)), VCO2_VOL, 0x6000, 0xA000));
    p.filter(KFILTER(RAW(cutoff(80)), RAW(cutoff(112)), 0x9000, 0xD800, 0x0800, 0x3800, kFenv0, kFenv0));
    switch (g.below(3)) {
    case 0:     /* a wah: an LFO, free or synced, on the cutoff */
        p.xmod(CUTOFF, g.chance(50) ? W_SINE : W_TRI, g.chance(50) ? T_LFO : T_SYNCED, 0,
               g.range(RAW(cuts(8)), RAW(cuts(25))));
        p.set(CUTOFF, XMOD_RATE, p.v[CUTOFF][XMOD_TYPE] == T_LFO ? g.range(RAW(lfo_hz(0.5)), RAW(lfo_hz(6)))
                                                                 : division(g.irange(D_4, D_16)));
        break;
    case 1:     /* a "yoi": the cutoff swells and falls with the note */
        p.xenv(CUTOFF, KXENV(RAW(xatk(30)), RAW(xatk(300)), RAW(xdec(100)), RAW(xdec(900)), RAW(cuts(10)), RAW(cuts(30))));
        break;
    default:    /* a mouth: the colour moves between band- and low-pass */
        p.xmod(COLOR, W_SINE, g.chance(50) ? T_LFO : T_SYNCED, 0, g.range(RAW(norm(0.1)), RAW(norm(0.3))));
        p.set(COLOR, XMOD_RATE, p.v[COLOR][XMOD_TYPE] == T_LFO ? g.range(RAW(lfo_hz(0.3)), RAW(lfo_hz(4)))
                                                               : division(g.irange(D_4, D_16)));
        break;
    }
    p.adsr(KADSR(0, RAW(env(40)), RAW(env(300)), RAW(env(2000)), RAW(level(0.6)), RAW(level(1.0)), RAW(env(50)), RAW(env(500))));
    p.base(VOLUME, 0xB000, 0xC800);
}

void wind(Patch &p, Gen &g)
{
    /* noise through a resonant band-pass that drifts: wind, surf, breath */
    p.bases(KBASES(VCO1_VOL, 0, 0, VCO2_WAVE, RAW(kTri), RAW(kSaw)));
    p.base(VCO2_TUNE, tune(g.irange(12, 36)));
    p.base(VCO2_VOL, 0xA000, 0xD800);
    p.noise(VCO2_TUNE, RAW(semis(60)), RAW(semis(110)));
    p.filter(KFILTER(RAW(cutoff(70)), RAW(cutoff(120)), 0x8000, 0xE000, 0, 0x3000, kFenv0, kFenv0));
    p.xmod(CUTOFF, g.chance(60) ? W_SINE : W_TRI, T_LFO, g.range(RAW(lfo_hz(0.05)), RAW(lfo_hz(0.6))),
           g.range(RAW(cuts(10)), RAW(cuts(35))));
    if (g.chance(40))
        p.xmod(RESO, W_SINE, T_LFO, g.range(RAW(lfo_hz(0.03)), RAW(lfo_hz(0.3))), g.range(RAW(norm(0.05)), RAW(norm(0.2))));
    p.adsr(KADSR(RAW(env(300)), RAW(env(2500)), RAW(env(2000)), RAW(env(5000)), RAW(level(0.7)), RAW(level(1.0)), RAW(env(500)), RAW(env(4000))));
    p.base(VOLUME, 0xA800, 0xC000);
}

void crackle(Patch &p, Gen &g)
{
    /* grains: noise whose level is sampled and held at a low audio rate --
     * crackle, vinyl, Geiger -- and a cutoff that jumps with the tempo */
    p.bases(KBASES(VCO1_VOL, 0, 0, VCO2_WAVE, RAW(kSquare), RAW(kSaw)));
    p.base(VCO2_TUNE, tune(g.irange(12, 40)));
    p.base(VCO2_VOL, 0x9000, 0xD000);
    p.noise(VCO2_TUNE, RAW(semis(60)), RAW(semis(110)));
    am(p, g, VOLUME, T_AUDIO, g.range(RAW(audio_st(-48)), RAW(audio_st(-12))), W_SH,
       RAW(norm(0.6)), RAW(norm(1.0)), false);
    const unsigned colour = g.chance(50) ? g.range(0, 0x3000) : g.range(0xC000, 0xFFFF);
    p.filter(RAW(cutoff(85)), RAW(cutoff(130)), 0x2000, 0x9000, colour, colour, kFenv0, kFenv0);
    humanize(p, g, CUTOFF, RAW(cuts(10)), RAW(cuts(30)));
    if (g.chance(50))
        p.hit(RAW(env(60)), RAW(env(600)));
    else
        p.adsr(KADSR(RAW(env(20)), RAW(env(400)), RAW(env(1000)), RAW(env(3000)), RAW(level(0.6)), RAW(level(1.0)), RAW(env(200)), RAW(env(2000))));
    p.base(VOLUME, 0xA800, 0xC000);
}

void bass808(Patch &p, Gen &g)
{
    /* the long, tuned kick a trap bass is: a sine with a small drop, driven,
     * sustaining as long as the step holds */
    const int st = g.chance(60) ? -24 : -12;
    p.base(VCO1_WAVE, 0, 0x1800);
    p.base(VCO1_TUNE, tune(st));
    p.base(VCO1_VOL, 0xA000, 0xF800);
    p.xenv(VCO1_TUNE, KXENV(0, 0, RAW(xdec(15)), RAW(xdec(150)), RAW(semis(2)), RAW(semis(14))));
    if (g.chance(30))
        transient(p, g, false);
    else if (g.chance(30)) {   /* a growl of TZFM under it */
        p.base(VCO2_WAVE, RAW(kSine), RAW(kTri));
        p.base(VCO2_TUNE, tune(st + (g.chance(50) ? 0 : 12)));
        p.bases(KBASES(VCO2_VOL, 0x2000, 0x6000, TZFM, 0x0800, 0x4000));
    }
    p.filter(KFILTER(RAW(cutoff(85)), RAW(cutoff(149)), 0, 0x4000, kLp, kLp, kFenv0, RAW(cuts(15))));
    p.adsr(KADSR(0, 0, RAW(env(500)), RAW(env(3000)), RAW(level(0.3)), RAW(level(0.9)), RAW(env(100)), RAW(env(1200))));
    p.base(VOLUME, 0xB000, 0xC800);
}

/* ---- p54: instruments of their own, out of perc ----------------------------- */
/* the cowbell: two squares a ~1.48 ratio apart, band-passed -- the 808's
 * knock and tail (an X-Env on the volume over a lower base), a TZFM clank,
 * or choked short */
void cowbell(Patch &p, Gen &g)
{
    const int st = p.tone(4, 17);
    p.base(VCO1_WAVE, RAW(kSquare) - 0x1000, RAW(kSquare) + 0x1000);
    p.base(VCO1_TUNE, tune(st));
    p.bases(KBASES(VCO1_VOL, 0x7000, 0xA800, VCO2_WAVE, RAW(kSquare), RAW(kSquare)));
    p.base(VCO2_TUNE, tune(st + 7));
    p.bases(KBASES(VCO2_FINE, RAW(cents(-30)), RAW(cents(-10)), VCO2_VOL, 0x7000, 0xA800));
    p.filter(KFILTER(RAW(cutoff(92)), RAW(cutoff(118)), 0x2000, 0x8000, 0, 0x2000, kFenv0, kFenv0));
    p.base(VOLUME, 0xB000, 0xC800);
    switch (g.below(3)) {
    case 0:
        p.hit(RAW(env(150)), RAW(env(500)));
        p.base(VOLUME, 0x5800, 0x7800);
        p.xenv(VOLUME, KXENV(0, 0, RAW(xdec(10)), RAW(xdec(40)), RAW(norm(0.35)), RAW(norm(0.6))));
        break;
    case 1:
        p.base(TZFM, 0x1000, 0x4000);
        p.xenv(TZFM, KXENV(0, 0, RAW(xdec(10)), RAW(xdec(80)), RAW(norm(0.2)), RAW(norm(0.6))));
        p.hit(RAW(env(40)), RAW(env(300)));
        break;
    default:
        p.hit(RAW(env(20)), RAW(env(90)));
        break;
    }
}

/* rimshot (a short high ping and a click of noise), clave (a high sine and
 * the filter ringing with it) or wood block */
void rim(Patch &p, Gen &g)
{
    switch (g.below(3)) {
    case 0:
        p.base(VCO1_WAVE, RAW(kTri), RAW(kSquare));
        p.base(VCO1_TUNE, tune(p.tone(14, 28)));
        p.base(VCO1_VOL, 0x7000, 0xB000);
        transient(p, g, true);
        p.filter(KFILTER(RAW(cutoff(100)), RAW(cutoff(125)), 0x6000, 0xC000, 0x1000, 0x4000, kFenv0, kFenv0));
        p.hit(RAW(env(10)), RAW(env(50)));
        break;
    case 1:
        p.base(VCO1_WAVE, RAW(kSine), RAW(kTri));
        p.base(VCO1_TUNE, tune(p.tone(20, 34)));
        p.base(VCO1_VOL, 0x8000, 0xC000);
        p.xenv(VCO1_TUNE, KXENV(0, 0, RAW(xdec(3)), RAW(xdec(15)), RAW(semis(1)), RAW(semis(6))));
        p.filter(KFILTER(RAW(cutoff(105)), RAW(cutoff(135)), 0x8000, 0xD800, kLp, 0xA000, kFenv0, kFenv0));
        p.hit(RAW(env(25)), RAW(env(90)));
        break;
    default:
        p.base(VCO1_WAVE, RAW(kTri), RAW(kSquare));
        p.base(VCO1_TUNE, tune(p.tone(5, 22)));
        p.base(VCO1_VOL, 0x6000, 0xA000);
        p.xenv(VCO1_TUNE, KXENV(0, 0, RAW(xdec(3)), RAW(xdec(20)), RAW(semis(1)), RAW(semis(8))));
        p.filter(KFILTER(RAW(cutoff(90)), RAW(cutoff(115)), 0x9000, 0xD800, 0x2000, 0x6000, kFenv0, kFenv0));
        p.hit(RAW(env(20)), RAW(env(120)));
        break;
    }
    p.base(VOLUME, 0xB000, 0xC800);
}

/* conga, bongo or timbale: a sine skin whose pitch bends up into the note
 * (a palm on it) or falls, now and then slapped (a burst of noise); the
 * timbale's shell rings */
void conga(Patch &p, Gen &g)
{
    static const int8_t kLo[] = {-12, 0, 5}, kHi[] = {2, 14, 19};
    const unsigned kind = g.below(3);
    p.base(VCO1_WAVE, RAW(kSine), RAW(kTri));
    p.base(VCO1_TUNE, tune(p.tone(kLo[kind], kHi[kind])));
    p.base(VCO1_VOL, 0x8000, 0xB800);
    if (g.chance(60))
        p.xenv(VCO1_TUNE, KXENV(0, 0, RAW(xdec(10)), RAW(xdec(60)), RAW(semis(-6)), RAW(semis(-1))));
    else
        p.xenv(VCO1_TUNE, KXENV(0, 0, RAW(xdec(5)), RAW(xdec(40)), RAW(semis(1)), RAW(semis(8))));
    if (g.chance(35))
        transient(p, g, true);
    p.filter(KFILTER(RAW(cutoff(80)), RAW(cutoff(125)), 0x4000, 0xC000, kLp, kLp, kFenv0, RAW(cuts(14))));
    if (kind == 2u)
        p.base(RESO, 0xB000, 0xD800);
    if (kind)
        p.hit(RAW(env(40)), RAW(env(250)));
    else
        p.hit(RAW(env(80)), RAW(env(450)));
    p.base(VOLUME, 0xB000, 0xC800);
}

/* the dub chord: two oscillators a chord's interval apart -- minor third,
 * fifth, minor seventh, ninth; kept in the scale -- dark and resonant, the
 * filter plucked, and now and then its cutoff steps every hit (stab already
 * covers the bright two-note stabs) */
void chord(Patch &p, Gen &g)
{
    static const int kInterval[] = {3, 7, 7, 10, 10, 14, 15, 17};
    static const int kOct[] = {-12, -12, 0, 0, 0, 12};
    const int st = g.pick(kOct);
    p.base(VCO1_WAVE, rich(g));
    p.base(VCO1_TUNE, tune(st));
    p.base(VCO1_VOL, 0x6000, 0xA000);
    p.base(VCO2_WAVE, rich(g));
    p.base(VCO2_TUNE, tune(st + p.interval(st, kInterval)));
    p.bases(KBASES(VCO2_FINE, RAW(cents(-8)), RAW(cents(8)), VCO2_VOL, 0x6000, 0xA000));
    p.filter(KFILTER(RAW(cutoff(65)), RAW(cutoff(100)), 0x7000, 0xC800, 0x7000, 0x8800, RAW(cuts(15)), RAW(cuts(60))));
    if (g.chance(40))
        humanize(p, g, CUTOFF, RAW(cuts(4)), RAW(cuts(18)));
    p.adsr(KADSR(0, RAW(env(5)), RAW(env(120)), RAW(env(700)), 0, RAW(level(0.35)), RAW(env(80)), RAW(env(600))));
    p.base(VOLUME, 0xB000, 0xC800);
}

/* ---- p42: spice ---------------------------------------------------------------
 * After a bass or tonal recipe, none to three of the knobs a player would
 * reach for next: an LFO or an X-Env on the cutoff, a TZFM bite, a drifting
 * detune, a moving waveform, a pitch chirp, vibrato, resonance movement,
 * drive or tremolo -- each only where the recipe left the slot alone.  p54:
 * colour or resonance FM at the note, and slow evolution. */
void spice(Patch &p, Gen &g)
{
    static const uint8_t kCount[] = {0, 0, 0, 1, 1, 1, 1, 2, 2, 3};
    const bool vco2 = p.v[VCO2_VOL][BASE] >= 0x3000u;
    const bool held = p.v[SUSTAIN][BASE] >= 0x4000u;
    unsigned n = g.pick(kCount);
    for (unsigned tries = 0; n && tries < 16; ++tries) {
        bool done = false;
        const int st1 = int(p.v[VCO1_TUNE][BASE] / 256) - 57;
        switch (g.below(16)) {
        case 13:    /* p54: the filter's colour flips at the note -- band-pass,
                     * low-pass and high-pass in turn, every cycle: nasal, vocal */
        case 14: {  /* p54: or its resonance swings at the note: a metallic edge */
            const unsigned slot = g.chance(60) ? COLOR : RESO;
            if (untouched(p, slot)) {
                p.xmod(slot, g.chance(60) ? W_SINE : W_TRI, T_RATIO, ratio_detent(harmonic(st1, g.chance(50) ? 4 : 8)),
                       g.range(RAW(norm(0.06)), RAW(norm(0.3))));
                done = true;
            }
            break;
        }
        case 15:    /* p54: slow evolution -- the waveform or the cutoff drifts over
                     * tens of seconds, so every pass of the pattern is a little different */
            if (untouched(p, VCO1_WAVE) && untouched(p, CUTOFF)) {
                const bool wave = g.chance(50);
                p.xmod(wave ? VCO1_WAVE : CUTOFF, g.chance(50) ? W_TRI : W_SINE, T_LFO,
                       g.range(RAW(lfo_hz(0.02)), RAW(lfo_hz(0.12))),
                       wave ? g.range(RAW(norm(0.1)), RAW(norm(0.35))) : g.range(RAW(cuts(4)), RAW(cuts(20))));
                done = true;
            }
            break;
        case 10:    /* filter FM at the note, a touch */
            if (untouched(p, CUTOFF)) {
                filter_fm(p, g, T_RATIO, ratio_detent(harmonic(st1, g.chance(60) ? 4 : 8)),
                          RAW(cuts(3)), RAW(cuts(12)), g.chance(50));
                done = true;
            }
            break;
        case 11:    /* the waveform flipping at the note, a touch */
            if (untouched(p, VCO1_WAVE)) {
                wave_fm(p, g, VCO1_WAVE, harmonic(st1, g.chance(50) ? 4 : 8), RAW(norm(0.05)), RAW(norm(0.25)));
                done = true;
            }
            break;
        case 12: {  /* every step a little different */
            static const unsigned kSlot[] = {DECAY, FENV, CUTOFF};
            unsigned slot = g.pick(kSlot);
            if (slot == DECAY && p.v[DECAY][BASE] > RAW(env(600)))
                slot = FENV;        /* a bell or a pad should ring as set */
            if (untouched(p, slot)) {
                humanize(p, g, slot, slot == DECAY ? RAW(norm(0.03)) : RAW(cuts(4)),
                         slot == DECAY ? RAW(norm(0.12)) : RAW(cuts(15)));
                done = true;
            }
            break;
        }
        case 0:     /* the cutoff wanders, freely or with the tempo */
            if (untouched(p, CUTOFF)) {
                static const unsigned kWave[] = {W_SINE, W_TRI, W_TRI, W_SAW, W_SQUARE, W_SH};
                const bool sync = g.chance(40);
                p.xmod(CUTOFF, g.pick(kWave), sync ? T_SYNCED : T_LFO,
                       sync ? division(g.irange(D_1, D_16)) : g.range(RAW(lfo_hz(0.05)), RAW(lfo_hz(4))),
                       g.range(RAW(cuts(3)), RAW(cuts(25))));
                done = true;
            }
            break;
        case 1:     /* the filter plucks on its own */
            if (untouched(p, CUTOFF)) {
                const bool up = g.chance(80);
                p.xenv(CUTOFF, 0, RAW(xatk(30)), RAW(xdec(30)), RAW(xdec(800)),
                       up ? RAW(cuts(8)) : RAW(cuts(-45)), up ? RAW(cuts(45)) : RAW(cuts(-8)));
                done = true;
            }
            break;
        case 2:     /* a TZFM bite at the start */
            if (vco2 && untouched(p, TZFM) && p.v[TZFM][BASE] < 0x1000u) {
                p.base(TZFM, 0, 0x2000);
                p.xenv(TZFM, KXENV(0, 0, RAW(xdec(20)), RAW(xdec(400)), RAW(norm(0.1)), RAW(norm(0.6))));
                done = true;
            }
            break;
        case 3:     /* VCO2's detune drifts */
            if (vco2 && untouched(p, VCO2_FINE)) {
                p.xmod(VCO2_FINE, W_SINE, T_LFO, g.range(RAW(lfo_hz(0.1)), RAW(lfo_hz(2))),
                       g.range(RAW(norm(0.005)), RAW(norm(0.04))));
                done = true;
            }
            break;
        case 4: {   /* a waveform moves */
            const unsigned slot = vco2 && g.chance(50) ? VCO2_WAVE : VCO1_WAVE;
            if (untouched(p, slot)) {
                p.xmod(slot, g.chance(50) ? W_TRI : W_SINE, T_LFO, g.range(RAW(lfo_hz(0.1)), RAW(lfo_hz(6))),
                       g.range(RAW(norm(0.03)), RAW(norm(0.25))));
                done = true;
            }
            break;
        }
        case 5:     /* a pitch chirp into the note, both oscillators */
            if (untouched(p, VCO1_TUNE) && untouched(p, VCO2_TUNE) && !p.v[SYNC][BASE]) {
                const bool up = g.chance(50);
                p.xenv(VCO1_TUNE, 0, 0, RAW(xdec(5)), RAW(xdec(60)),
                       up ? RAW(semis(1)) : RAW(semis(-12)), up ? RAW(semis(12)) : RAW(semis(-1)));
                p.xenv_like(VCO2_TUNE, VCO1_TUNE);
                done = true;
            }
            break;
        case 6:     /* vibrato on a held sound */
            if (held && untouched(p, VCO1_TUNE) && untouched(p, VCO2_TUNE)) {
                const unsigned rate = g.range(RAW(lfo_hz(4)), RAW(lfo_hz(7.5)));
                const unsigned amt = g.range(RAW(semis(0.08)), RAW(semis(0.4)));
                p.xmod(VCO1_TUNE, W_SINE, T_LFO, rate, amt);
                p.xmod(VCO2_TUNE, W_SINE, T_LFO, rate, amt);
                done = true;
            }
            break;
        case 7:     /* the resonance breathes */
            if (untouched(p, RESO)) {
                p.xmod(RESO, W_SINE, T_LFO, g.range(RAW(lfo_hz(0.05)), RAW(lfo_hz(2))),
                       g.range(RAW(norm(0.05)), RAW(norm(0.3))));
                done = true;
            }
            break;
        case 8: {   /* drive */
            static const unsigned kVol[] = {VCO1_VOL, VCO2_VOL};
            for (unsigned slot : kVol)
                if (p.v[slot][BASE] >= 0x3000u)
                    p.base(slot, clamp16(int(p.v[slot][BASE]) + int(g.range(0x1000, 0x3800))));
            done = true;
            break;
        }
        default:    /* tremolo on a held sound */
            if (held && untouched(p, VOLUME)) {
                const bool sync = g.chance(40);
                p.xmod(VOLUME, g.chance(70) ? W_SINE : W_TRI, sync ? T_SYNCED : T_LFO,
                       sync ? division(g.irange(D_4, D_16)) : g.range(RAW(lfo_hz(2)), RAW(lfo_hz(8))),
                       g.range(RAW(norm(0.05)), RAW(norm(0.3))));
                done = true;
            }
            break;
        }
        if (done)
            --n;
    }
}

/* ---- the catalogue --------------------------------------------------------- */
void hat_closed(Patch &p, Gen &g) { hat(p, g, false); }
void hat_open(Patch &p, Gen &g) { hat(p, g, true); }

/* The groups the black keys pick (R2; p49 layout): C# the
 * kick, C#+D# hats, clap and snare, D# toms and the other drums, F# the
 * deep basses (FM among them), F#+G# the other basses, G# the tonal ones,
 * A# zaps, blips and short glitches, F#+A# everything else; p51, G#+A#
 * (left over): the plucked and struck tonal ones -- pluck, bell, stab,
 * EP, mallet, AM bell -- which G# keeps too; p52, D#+F#: FM basses only. */
enum Group : uint16_t {
    G_KICK = 1, G_HATS = 2, G_DRUMS = 4, G_DEEP = 8, G_BASS = 16, G_TONAL = 32, G_ZAP = 64, G_OTHER = 128,
    G_PLUCK = 256, G_FM = 512
};

/* The order is the instrument's number (D_MNRAND_LAST, D_MNRAND_FORCE and
 * tests/test_modnote_random.py); new ones go at the end.  The weight is how
 * often each comes up, out of the sum: drums about 40 %, basses 20 %. */
struct Entry {
    void (*make)(Patch &, Gen &);
    uint8_t weight;
    uint8_t level;      /* p50/p51: the output level, x/64 (see leveled) */
    uint16_t group;
};
const Entry kCatalogue[] = {
    {kick, 12, 85, G_KICK}, {snare, 9, 85, G_HATS}, {clap, 6, 100, G_HATS},
    {hat_closed, 9, 100, G_HATS}, {hat_open, 5, 31, G_HATS}, {tom, 5, 47, G_DRUMS},
    {perc, 4, 78, G_DRUMS}, {bass, 9, 37, G_DEEP}, {acid, 5, 52, G_BASS}, {lead, 4, 28, G_TONAL},
    {pad, 4, 42, G_TONAL}, {pluck, 5, 46, G_TONAL | G_PLUCK}, {bell, 4, 21, G_TONAL | G_PLUCK}, {zap, 4, 100, G_ZAP},
    {stab, 4, 31, G_TONAL | G_PLUCK}, {glitch, 5, 69, G_ZAP},
    /* p39 */
    {cymbal, 4, 23, G_DRUMS}, {shaker, 4, 100, G_DRUMS}, {drop, 3, 100, G_DRUMS},
    {fm_bass, 6, 27, G_DEEP | G_FM}, {reese, 4, 18, G_DEEP}, {wobble, 5, 21, G_BASS},
    {growl, 4, 22, G_BASS}, {sync_lead, 4, 43, G_TONAL}, {hoover, 3, 16, G_TONAL},
    {phaser_pad, 3, 35, G_TONAL}, {gate, 4, 17, G_TONAL}, {fm_ep, 4, 19, G_TONAL | G_PLUCK},
    {mallet, 4, 23, G_TONAL | G_PLUCK}, {bleeps, 4, 21, G_ZAP}, {riser, 3, 33, G_OTHER}, {siren, 3, 17, G_OTHER},
    {drone, 3, 55, G_TONAL},
    /* p43 */
    {metal_perc, 4, 58, G_DRUMS}, {am_bell, 3, 22, G_TONAL | G_PLUCK}, {talk, 3, 50, G_TONAL},
    {wind, 2, 100, G_OTHER}, {crackle, 2, 49, G_OTHER}, {bass808, 5, 19, G_DEEP},
    /* p54 */
    {cowbell, 3, 100, G_DRUMS}, {rim, 4, 100, G_DRUMS}, {conga, 4, 49, G_DRUMS}, {chord, 4, 42, G_TONAL | G_PLUCK},
};
constexpr unsigned kInstruments = sizeof kCatalogue / sizeof kCatalogue[0];

/* p55: styles -- one palette for a whole generation, so a full set of 40
 * instruments sounds like one project.  p56: TEMPO+FUNC+G# takes the style of the mod-note page it is
 * pressed on: 1 techno, 2 dub, 3 acid, 4 electro, 5 ambient (A# stays any
 * instruments); the drum and bass kits built after it take the same
 * (D_MNRAND_MUTED's top byte, the style + 1; A# or a boot clears it).  A style is the instruments'
 * weights (0..15, two to a byte, low nibble first, catalogue order) and
 * the ranges of a Kit drawn once for the generation: room (x 0x100 raw on
 * every decay and release), brightness (cutoff semitones) and drive (x
 * 0x100 raw on the sounding VCO levels) -- and with it the kit's coherence
 * across the whole set: one metal for every hat and cymbal, toms tuned
 * over the kick. */
struct Style {
    int8_t room[2], bright[2], drive[2];
    uint8_t w[(kInstruments + 1) / 2];
};
const Style kStyle[] = {
    /* techno  */ {{-12, 0}, {-2, 6}, {0, 24},
                  {0x3C, 0x96, 0x46, 0x65, 0x12, 0x21, 0x31, 0x35, 0x34, 0x51, 0x14, 0x12, 0x13, 0x03, 0x20, 0x02, 0x41, 0x01, 0x10, 0x21, 0x15, 0x04}},
    /* dub     */ {{4, 20}, {-10, -2}, {-16, 0},
                  {0x38, 0x54, 0x25, 0x93, 0x10, 0x23, 0x11, 0x12, 0x42, 0x22, 0x23, 0x00, 0x30, 0x22, 0x12, 0x42, 0x13, 0x02, 0x33, 0x02, 0x26, 0x0C}},
    /* acid    */ {{-8, 4}, {0, 8}, {8, 28},
                  {0x3C, 0x96, 0x35, 0x43, 0x2E, 0x21, 0x40, 0x23, 0x24, 0x21, 0x21, 0x24, 0x02, 0x02, 0x20, 0x12, 0x20, 0x10, 0x00, 0x21, 0x13, 0x01}},
    /* electro */ {{-12, -2}, {2, 8}, {-4, 16},
                  {0x8A, 0x96, 0x54, 0x44, 0x32, 0x21, 0x61, 0x32, 0x22, 0x52, 0x31, 0x41, 0x01, 0x11, 0x51, 0x11, 0x30, 0x51, 0x00, 0x68, 0x44, 0x01}},
    /* ambient */ {{8, 24}, {-6, 2}, {-16, -4},
                  {0x02, 0x21, 0x12, 0x33, 0x30, 0x5A, 0x06, 0x21, 0x32, 0x11, 0x03, 0x10, 0x60, 0x52, 0x26, 0x03, 0x28, 0x15, 0x45, 0x00, 0x12, 0x04}},
};
constexpr unsigned kStyles = sizeof kStyle / sizeof kStyle[0];

unsigned weight(const Style *st, unsigned i)
{
    return st ? st->w[i >> 1] >> ((i & 1u) * 4u) & 15u : kCatalogue[i].weight;
}

/* the style the last A# chose (none: null) */
const Style *stored_style()
{
    const unsigned s = diag(D_MNRAND_MUTED) >> 24;
    return s && s <= kStyles ? &kStyle[s - 1u] : nullptr;
}

/* p50: the instruments level with one another.  The bench (tools/bench, the
 * firmware in sysemu, a digital voice after it) renders 300 ms notes; their
 * K-weighted momentary loudness (BS.1770, 400 ms, the maximum).  p51: nine draws of each instead of three,
 * and the median alone hid the loud draws -- a wide-spread instrument's
 * typical patch sat at the target while a third of them stood 8 dB over it.
 * Now the level puts the middle of the median and the energy mean at -29
 * LU, with the energy mean at most -26.5 (the kick's -27.4, which stays at
 * full volume: the reference), and no boost past 100 (VOLUME's headroom;
 * the quiet drums and FX are at full volume already).  Applied to the
 * output volume, which the VCA follows linearly -- all of it: p50 scaled
 * the base alone, and the gate (a synced X-Mod on VOLUME nearly full
 * depth over a low base) came out 3 dB louder for a 9 dB cut.  The depths
 * are cubic, so they take the cube root of the factor. */
void leveled(Patch &p, unsigned what)
{
    uint16_t *v = p.v[VOLUME];
    const float k = kCatalogue[what].level * (1.f / 64);
    float c = 1.f;
    for (unsigned i = 0; i < 5u; ++i)
        c = (2.f * c + k / (c * c)) * (1.f / 3);
    v[BASE] = uint16_t(clamp16(int(float(v[BASE]) * k)));
    for (unsigned l = XMOD_AMT; l <= XENV_AMT; l += XENV_AMT - XMOD_AMT)
        v[l] = uint16_t(clamp16(0x8000 + int(float(int(v[l]) - 0x8000) * c)));
}

/* by weight among the instruments in `groups` (0: all of them); p55: the
 * style's weights when there is one */
unsigned choose(Gen &g, unsigned groups, const Style *st)
{
    const auto in = [groups](unsigned i) { return !groups || (kCatalogue[i].group & groups); };
    unsigned total = 0;
    for (unsigned i = 0; i < kInstruments; ++i)
        if (in(i))
            total += weight(st, i);
    if (!total)
        return choose(g, 0, nullptr);
    unsigned r = g.below(total);
    for (unsigned i = 0; i < kInstruments; ++i) {
        if (!in(i))
            continue;
        const unsigned w = weight(st, i);
        if (r < w)
            return i;
        r -= w;
    }
    return kInstruments - 1u;
}

/* ---- events ------------------------------------------------------------------ */
void send(void *mgr, Byte *e)
{
    void *p = e;
    reinterpret_cast<Dispatch>(0x081576D5u)(mgr, &p);
    if (p)
        destroy(p);
}

Byte *make(unsigned size, uintptr_t vt, unsigned tag, unsigned note)
{
    Byte *e = static_cast<Byte *>(reinterpret_cast<Alloc>(0x081591D5u)(size));
    if (!e)
        return nullptr;
    for (unsigned i = 0; i < size; ++i)
        e[i] = 0;
    *reinterpret_cast<uint32_t *>(e) = vt;
    e[4] = Byte(tag);
    e[5] = Byte(tag >> 8);
    e[6] = Byte(note);
    return e;
}

/* p42: the seed.  A state that lives across presses (D_MNRAND_SEED),
 * advanced by the golden ratio and stirred with the cycle counter at this
 * press and the stock generator, then avalanched (murmur3's finaliser), so
 * two presses never start from related states even if a source stalls. */
uint32_t avalanche(uint32_t h)
{
    h ^= h >> 16;
    h *= 0x85EBCA6Bu;
    h ^= h >> 13;
    h *= 0xC2B2AE35u;
    h ^= h >> 16;
    return h;
}

Gen seeded()
{
    volatile uint32_t &state = diag(D_MNRAND_SEED);
    uint32_t s = state + 0x9E3779B9u;
    s ^= *reinterpret_cast<volatile uint32_t *>(kDwtCyccnt);
    s = avalanche(s ^ avalanche(reinterpret_cast<Rng>(0x08147985u)()));
    state = s;
    Gen g;
    g.s = s ? s : 0x6D2B79F5u;
    g.next();
    g.next();
    return g;
}

constexpr uintptr_t kUi = 0x2000DE54u;          /* the panel UI object (static) */

/* the pitch a mod note plays at (0x0814FE5C): UI+0x98[n] semitone +
 * (UI+0xC0[n] octave + 5) * 12; C4 for anything else */
int mn_played(unsigned note)
{
    const auto *ui = reinterpret_cast<const Byte *>(kUi);
    if (note < 40u) {
        const int played = ui[0x98 + note] + (int8_t(ui[0xC0 + note]) + 5) * 12;
        if (played >= 0 && played <= 127)
            return played;
    }
    return 60;
}

/* the playing pattern's scale (bank 0 at manager+0x18D: [4] scale, [5]
 * root; degree lists at manager+0x6C, 14 bytes each: count, semitones --
 * the layout np_random.cpp reads) and the note the sound is built for */
Context context(const void *mgr, int played, Kit *kit)
{
    Context c{};
    c.kit = kit;
    const auto *m = static_cast<const Byte *>(mgr);
    const unsigned scale = m[0x18D + 4], root = m[0x18D + 5];
    if (scale && scale < 20u && root < 12u) {
        const Byte *deg = m + 0x6C + scale * 14u;
        unsigned mask = 0, tones = 0;
        for (unsigned i = 1; i <= deg[0] && i <= 12u; ++i)
            if (deg[i] < 12u && !(mask >> deg[i] & 1u)) {
                mask |= 1u << deg[i];
                ++tones;
            }
        if (tones >= 2u && (mask & 1u)) {     /* an empty custom scale: chromatic */
            c.scale = uint16_t(mask);
            c.root = uint8_t(root);
        }
    }
    c.played = played;
    return c;
}

/* A new sound where the last note came from is a new instrument: make the
 * next note-on see a switch, so the CH fade takes the old sound out and
 * the new one starts from silence.  `id` is how np_choke_note_on knows the
 * last one (mod note | 0x100, a patch note 0x1FF); 0x1FE is a marker no
 * note-on produces. */
void switched(unsigned id)
{
    static const unsigned kLast[] = {D_CHOKE_LAST0, D_CHOKE_LAST1};
    for (unsigned off : kLast)
        if (diag(off) == id)
            diag(off) = 0x1FEu;
}

/* clear the mod note, then one ModNoteParamData per parameter that
 * differs from the reset, all with the transaction tag, synchronously */
void emit(void *mgr, unsigned tag, unsigned note, const Patch &p, unsigned what, unsigned groups)
{
    unsigned sent = 0;
    if (Byte *e = make(8, kClearVt, tag, note))
        send(mgr, e);
    for (unsigned s = 0; s < kSlots; ++s)
        for (unsigned l = 0; l < kLocals; ++l) {
            const unsigned raw = p.v[s][l];
            if (raw == reset_value(s, l))
                continue;
            Byte *e = make(12, kParamVt, tag, note);
            if (!e)
                continue;
            e[7] = Byte(s);
            e[8] = Byte(l);
            e[10] = Byte(raw);
            e[11] = Byte(raw >> 8);
            send(mgr, e);
            ++sent;
        }
    switched(note | 0x100u);
    if (note < 40u)
        reinterpret_cast<volatile uint8_t *>(kDiag + D_MNRAND_KIND)[note] = uint8_t(what + 1u);
    diag(D_MNRAND_N) += 1u;
    diag(D_MNRAND_LAST) = what | note << 8 | (sent & 0xFFu) << 16 | groups << 24;
}

/* P1: into the pattern's patch, what a knob turn writes: PatchParamData
 * (16 bytes: +4 tag, +8 pattern, +9 type 0, +10 slot, +11 local, +12
 * value) for every parameter off the reset, and the reset for every one
 * the old patch held and the new sound leaves there.  Not the stock
 * ClearPatchData: its handler (0x081279C0) resets every record whose byte
 * +4 is the layer, whatever its type -- mod note 0's parameters go with
 * layer 0's patch (DELETE+RECORD does the same).  The patch's records are
 * type 0 in the manager's list (+0x1BC: [4] layer, [5] type, [6] slot,
 * [7] local); 0x0811FF0C gives a layer's pattern. */
unsigned emit_patch(void *mgr, unsigned tag, unsigned pattern, const Patch &p)
{
    constexpr unsigned kParams = kSlots * kLocals;
    uint32_t held[(kParams + 31u) / 32u];
    for (unsigned w = 0; w < (kParams + 31u) / 32u; ++w)
        held[w] = 0u;
    using LayerPattern = unsigned (*)(void *, unsigned);
    for (unsigned layer = 0; layer < 2u; ++layer) {
        if (reinterpret_cast<LayerPattern>(0x0811FF0Du)(mgr, layer) != pattern)
            continue;
        for (const Byte *r = *reinterpret_cast<Byte *const *>(static_cast<Byte *>(mgr) + 0x1BC); r;
             r = *reinterpret_cast<Byte *const *>(r))
            if (r[4] == layer && r[5] == 0u && r[6] < kSlots && r[7] < kLocals) {
                const unsigned i = r[6] * kLocals + r[7];
                held[i >> 5] |= 1u << (i & 31u);
            }
        break;
    }
    unsigned sent = 0;
    for (unsigned s = 0; s < kSlots; ++s)
        for (unsigned l = 0; l < kLocals; ++l) {
            const unsigned raw = p.v[s][l], i = s * kLocals + l;
            if (raw == reset_value(s, l) && !(held[i >> 5] >> (i & 31u) & 1u))
                continue;
            Byte *e = make(16, kPatchParamVt, tag, 0);
            if (!e)
                continue;
            e[8] = Byte(pattern);
            e[10] = Byte(s);
            e[11] = Byte(l);
            e[12] = Byte(raw);
            e[13] = Byte(raw >> 8);
            send(mgr, e);
            ++sent;
        }
    return sent;
}

/* p49: FM 40 % deeper: every modulator at audio rate or
 * at a ratio of the note (not the S&H noise) gets its X-Mod amount, and an
 * X-Env routed to that amount (dest 1) its depth, 1.4x as far from centre */
void deeper_fm(Patch &p)
{
    for (auto &v : p.v) {
        if (v[XMOD_TYPE] < T_AUDIO || v[XMOD_WAVE] == W_SH)
            continue;
        for (unsigned l = XMOD_AMT; l <= XENV_AMT; l += XENV_AMT - XMOD_AMT)
            if (l == XMOD_AMT || v[XENV_DEST] == 1u)
                v[l] = uint16_t(clamp16(0x8000 + (int(v[l]) - 0x8000) * 7 / 5));
    }
}

/* p54: the main envelope follows the tempo.  The recipes' times are set at
 * 120 BPM; at another tempo every attack, decay and release scales with
 * the beat (140 BPM x0.86, 90 x1.33, within x0.5..x2), and an attack never
 * takes more than a beat -- two seconds of attack never sound on a
 * sequencer step.  The time curve is kEnv's, at run time: ms at raw
 * i * 0x2000.  Tempo: the transport's tick rate (+4, float), 2048 at 120
 * BPM -- what the synced LFOs run from (np_lfo.cpp). */
const float kEnvMs[9] = {0.2f, 6.8f, 32.0f, 105.0f, 287.0f, 695.0f, 1546.0f, 3300.0f, 6500.0f};

float env_ms(unsigned raw)
{
    const unsigned i = raw >> 13 < 7u ? raw >> 13 : 7u;
    return kEnvMs[i] + float(raw - i * 0x2000u) * (1.f / 8192) * (kEnvMs[i + 1] - kEnvMs[i]);
}

uint16_t env_raw(float ms)
{
    unsigned i = 0;
    while (i < 7u && ms > kEnvMs[i + 1])
        ++i;
    const float f = (ms - kEnvMs[i]) / (kEnvMs[i + 1] - kEnvMs[i]);
    return uint16_t(clamp16(int(i * 0x2000u) + int((f < 0.f ? 0.f : f > 1.f ? 1.f : f) * 8192.f + 0.5f)));
}

void tempo_fit(Patch &p)
{
    const auto *t = reinterpret_cast<const uint8_t *>(reinterpret_cast<void *(*)()>(0x08118381u)());
    const float rate = *reinterpret_cast<const float *>(t + 4);
    float k = rate > 1024.f && rate < 8192.f ? 2048.f / rate : 1.f;
    for (unsigned s = ATTACK; s <= RELEASE; ++s) {
        float ms = env_ms(p.v[s][BASE]) * k;
        if (s == ATTACK && ms > 385.f * k)           /* a beat: 500 ms at 120, the attack's 1.3x */
            ms = 385.f * k;
        p.v[s][BASE] = env_raw(ms);
    }
}

/* p54: a draw that would barely sound is drawn again -- no oscillator up
 * (a level, a level X-Env or a level X-Mod) and the filter not
 * self-oscillating, or a low- or band-pass shut more than half an octave
 * below the lower sounding oscillator even at its brightest (ENV AMOUNT,
 * the cutoff's X-Env and X-Mod all up).  A noise source (S&H at audio
 * rate on a VCO's pitch) is broadband and always passes.  Bench, 234
 * renders: of the 33 under -45 LU, 17 were attacks longer than a 300 ms
 * note (tempo_fit's cap), the shut filters these. */
float up_st(unsigned raw)
{
    const float x = (int(raw) - 0x8000) * (1.f / 32768);
    return x > 0.f ? 149.f * x * x * x : 0.f;
}

bool mute(const Patch &p)
{
    const auto &v = p.v;
    const bool s1 = v[VCO1_VOL][BASE] >= 0x1800u || v[VCO1_VOL][XENV_AMT] > 0x8000u || v[VCO1_VOL][XMOD_AMT] != 0x8000u;
    const bool s2 = v[VCO2_VOL][BASE] >= 0x1800u || v[VCO2_VOL][XENV_AMT] > 0x8000u || v[VCO2_VOL][XMOD_AMT] != 0x8000u;
    if (!s1 && !s2)
        return v[RESO][BASE] < 0xF000u;
    if (v[COLOR][BASE] > 0x9000u)
        return false;
    for (unsigned slot = VCO1_TUNE; slot <= VCO2_TUNE; slot += VCO2_TUNE - VCO1_TUNE)
        if (v[slot][XMOD_WAVE] == W_SH && v[slot][XMOD_TYPE] == T_AUDIO && v[slot][XMOD_AMT] != 0x8000u)
            return false;
    int pitch = 0xFFFF;
    if (s1)
        pitch = v[VCO1_TUNE][BASE];
    if (s2 && v[VCO2_TUNE][BASE] < pitch)
        pitch = v[VCO2_TUNE][BASE];
    const unsigned xm = v[CUTOFF][XMOD_AMT];          /* either sign opens it half the cycle */
    const float open = up_st(v[FENV][BASE]) + up_st(v[CUTOFF][XENV_AMT]) + up_st(xm > 0x8000u ? xm : 0x10000u - xm);
    return float(v[CUTOFF][BASE]) + open * 256.f < float(pitch - 6 * 256);
}

void season(Patch &p, const Kit &k);

/* instrument `what`, built for cx: its recipe, and a bass or tonal one
 * gets the spice; p54: drawn again (up to five times) while mute(), then
 * fitted to the tempo; p55: seasoned by a style's kit when there is one */
void instrument(Patch &p, Gen &g, Context &cx, unsigned what)
{
    p.g = &g;
    p.cx = &cx;
    for (unsigned tries = 0;; ++tries) {
        p.reset();
        kCatalogue[what].make(p, g);
        if (kCatalogue[what].group & (G_DEEP | G_BASS | G_TONAL))
            spice(p, g);
        if (cx.kit)
            season(p, *cx.kit);
        deeper_fm(p);
        if (tries == 5u || !mute(p))
            break;
        diag(D_MNRAND_MUTED) += 1u;
    }
    tempo_fit(p);
    leveled(p, what);
}

/* by weight from `groups` (0: any); not the same instrument twice running
 * in one place (`last`: its number + 1): once, draw again (a group of one
 * -- C#, the kick -- keeps it).  D_MNRAND_FORCE (tests) decides instead. */
unsigned pick(Gen &g, unsigned groups, unsigned last, const Style *st = nullptr)
{
    const uint32_t force = diag(D_MNRAND_FORCE);
    if (force && force <= kInstruments)
        return force - 1u;
    unsigned what = choose(g, groups, st);
    if (last == what + 1u)
        what = choose(g, groups, st);
    return what;
}

/* One instrument from `groups` for mod note `note`; p55: by a style's
 * weights and seasoned by its kit, for A# */
void generate(void *mgr, unsigned tag, unsigned note, unsigned groups, const Style *st = nullptr,
              Kit *kit = nullptr)
{
    Gen g = seeded();
    const unsigned what =
        pick(g, groups, note < 40u ? reinterpret_cast<volatile uint8_t *>(kDiag + D_MNRAND_KIND)[note] : 0u, st);
    Context cx = context(mgr, mn_played(note), kit);
    Patch p;
    instrument(p, g, cx, what);
    emit(mgr, tag, note, p, what, groups);
}

/* K1: a drum kit over the eight mod notes of the page, one transaction.
 * C kick, D snare, E clap, F closed hat, G open hat, A tom, B perc, C'
 * cymbal or shaker.  What the kit shares is drawn once (Kit), applied by
 * the recipes (the hats' and cymbal's metal, the tom over the kick) and
 * afterwards to every member (room, brightness, drive). */
void season(Patch &p, const Kit &k)
{
    auto add = [&p](unsigned slot, int delta, int hi) {
        const int v = int(p.v[slot][BASE]) + delta;
        p.v[slot][BASE] = uint16_t(v < 0 ? 0 : v > hi ? hi : v);
    };
    add(DECAY, k.room, 0xFFFF);
    add(RELEASE, k.room, 0xFFFF);
    add(CUTOFF, k.bright * 256, (149 - 3) * 256);
    static const unsigned kVol[] = {VCO1_VOL, VCO2_VOL};
    for (unsigned slot : kVol)
        if (p.v[slot][BASE] >= 0x3000u)       /* only an oscillator that sounds */
            add(slot, k.drive, 0xFFFF);
}

/* what a kit shares, drawn once; p55: room, brightness and drive in a
 * style's ranges when there is one */
void draw_kit(Kit &kit, Gen &g, const Style *st)
{
    kit.kick_note = 0;                          /* every field set: no memset */
    if (st) {
        kit.room = g.irange(st->room[0] * 256, st->room[1] * 256);
        kit.bright = g.irange(st->bright[0], st->bright[1]);
        kit.drive = g.irange(st->drive[0] * 256, st->drive[1] * 256);
    } else {
        kit.room = g.irange(-0x0C00, 0x0C00);   /* about x0.6 .. x1.6 */
        kit.bright = g.irange(-6, 6);
        kit.drive = g.irange(-0x1000, 0x1800);
    }
    kit.metal_st = g.irange(19, 34);
    kit.metal_iv = g.irange(3, 9);
    kit.metal_fine = g.exact(RAW(cents(-45)), RAW(cents(45)));
    kit.metal_tzfm = g.range(0x9000, 0xF000);
}

void generate_kit(void *mgr, unsigned tag, unsigned page)
{
    enum : uint8_t { CYMBAL_OR_SHAKER = 0xFF };
    static const uint8_t kLayout[8] = {0, 1, 2, 3, 4, 5, 6, CYMBAL_OR_SHAKER};
    Gen g = seeded();
    Kit kit;
    draw_kit(kit, g, stored_style());
    for (unsigned col = 0; col < 8u; ++col) {
        const unsigned note = page * 8u + col;
        if (note >= 40u)
            break;
        unsigned what = kLayout[col];
        if (what == CYMBAL_OR_SHAKER)
            what = g.chance(60) ? 16u : 17u;
        if (what == 6u) {                       /* p54: B perc, cowbell, rim or conga */
            static const uint8_t kB[] = {6, 39, 40, 41};
            what = g.pick(kB);
        }
        Context cx = context(mgr, mn_played(note), &kit);
        Patch p;
        p.g = &g;
        p.cx = &cx;
        p.reset();
        kCatalogue[what].make(p, g);
        season(p, kit);
        deeper_fm(p);
        tempo_fit(p);
        leveled(p, what);
        emit(mgr, tag, note, p, what, G_KICK);
    }
    diag(D_MNRAND_KITS) += 1u;
}

/* K2: a bass kit over the page, in the pattern's scale.  A different bass
 * on every key (p46; p45 gave all eight one recipe): each key draws its own
 * by weight, none twice until all seven basses are on the page.  Each key
 * is a degree of the scale up from the root -- C the root, D the second,
 * ..., C' the octave, a scale of fewer notes carrying on into the next
 * octave; no scale: C major, each key its own note -- and its bass is
 * built for that note, so what the recipe puts in the scale (a fifth, an
 * interval) is in it from there.  The keys' live pitches (UI+0x98
 * semitone, UI+0xC0 octave: UI state, what UP+DOWN+key sets) become those
 * degrees, around C4, so playing or recording the page plays the scale. */
void generate_bass_kit(void *mgr, unsigned tag, unsigned page)
{
    Gen g = seeded();
    const Style *st = stored_style();
    Kit kit;
    draw_kit(kit, g, st);
    Context cx = context(mgr, 60, st ? &kit : nullptr);
    unsigned mask = cx.scale, root = cx.root;
    if (!mask) {
        mask = 0xAB5u;                          /* C major */
        root = 0u;
    }
    uint8_t deg[12];
    unsigned n = 0;
    for (unsigned i = 0; i < 12u; ++i)
        if (mask >> i & 1u)
            deg[n++] = uint8_t(i);
    const int base = 60 + int(root) - (root > 5u ? 12 : 0);
    auto *ui = reinterpret_cast<Byte *>(kUi);
    uint64_t used = 0;
    for (unsigned col = 0; col < 8u; ++col) {
        const unsigned note = page * 8u + col;
        if (note >= 40u)
            break;
        const int pitch = base + deg[col % n] + 12 * int(col / n);
        ui[0x98 + note] = Byte(pitch % 12);
        ui[0xC0 + note] = Byte(pitch / 12 - 5);
        unsigned what = pick(g, G_DEEP | G_BASS, 0u, st);
        for (unsigned tries = 0; (used >> what & 1u) && tries < 24u; ++tries)
            what = pick(g, G_DEEP | G_BASS, 0u, st);
        used |= uint64_t(1) << what;
        cx.played = pitch;
        Patch p;
        instrument(p, g, cx, what);
        emit(mgr, tag, note, p, what, G_DEEP | G_BASS);
    }
    diag(D_MNRAND_KITS) += 0x100u;
}

/* K3: every mod note of every page, an instrument each, one transaction.
 * p56: A# (style 0) any instruments, G# the style of the mod-note page it
 * is pressed on (style = page + 1, see kStyle); either way the kits built
 * after it follow it -- A# clears the style, so it never outlives the next
 * full generation (p55 kept it until a reboot) */
void generate_all(void *mgr, unsigned tag, unsigned style)
{
    if (style > kStyles)
        style = kStyles;
    volatile uint32_t &word = diag(D_MNRAND_MUTED);
    word = (word & 0x00FFFFFFu) | style << 24;
    const Style *st = style ? &kStyle[style - 1u] : nullptr;
    Gen g = seeded();
    Kit kit;
    draw_kit(kit, g, st);
    for (unsigned note = 0; note < 40u; ++note)
        generate(mgr, tag, note, 0u, st, st ? &kit : nullptr);
    diag(D_MNRAND_KITS) += style ? 0x1000000u : 0x10000u;
}

/* A transaction tag the way 0x08157578 assigns one to a queued event:
 * the manager's counter (+0x4C) advanced, its low 14 bits. */
unsigned new_tag(void *mgr)
{
    auto *m = static_cast<Byte *>(mgr);
    *reinterpret_cast<volatile uint32_t *>(m + 0x58) = 0u;
    volatile uint32_t &count = *reinterpret_cast<volatile uint32_t *>(m + 0x4C);
    count = count + 1u;
    return count & 0x3FFFu;
}

/* mod notes generated by a black + white press, until that key's release:
 * neither the press nor the release records a step */
volatile uint32_t &mark_word(unsigned note)
{
    return diag(note < 32u ? D_MNRAND_MARK0 : D_MNRAND_MARK1);
}

/* The black keys (C# D# F# G# A#) as bits 0-4, and what each held set
 * picks; a set not in the table, the union of its keys' own groups. */
const uint8_t kBlackKey[5] = {35, 37, 40, 42, 44};
const uint16_t kCombo[][2] = {
    {1, G_KICK}, {3, G_HATS}, {2, G_DRUMS}, {4, G_DEEP}, {12, G_BASS}, {8, G_TONAL}, {16, G_ZAP}, {20, G_OTHER},
    {24, G_PLUCK}, {6, G_FM},
};

/* the black keys down (this one counts, whatever the UI has seen of it),
 * as the groups they pick; none: 0, any instrument */
unsigned black_groups(Byte *ui, unsigned key)
{
    unsigned held = 0;
    for (unsigned i = 0; i < 5u; ++i)
        if (kBlackKey[i] == key || np_ui_pressed(ui, kBlackKey[i]))
            held |= 1u << i;
    unsigned singles = 0;
    for (const auto &c : kCombo) {
        if (c[0] == held)
            return c[1];
        if (!(c[0] & (c[0] - 1u)) && (held & c[0]))      /* one of the set's keys */
            singles |= c[1];
    }
    return singles;
}

/* P1: one instrument from `groups` into `pattern`'s patch, built for the
 * note `played` (the key that asked for it plays it next), one transaction */
void generate_patch(void *mgr, unsigned pattern, int played, unsigned groups)
{
    Gen g = seeded();
    volatile uint32_t &last = diag(D_MNRAND_PKIND);
    const unsigned what = pick(g, groups, last);
    Context cx = context(mgr, played, nullptr);
    Patch p;
    instrument(p, g, cx, what);
    const unsigned sent = emit_patch(mgr, new_tag(mgr), pattern, p);
    switched(0x1FFu);
    last = what + 1u;
    diag(D_MNRAND_PATCHES) += 1u;
    diag(D_MNRAND_LAST) = what | 0xFFu << 8 | (sent & 0xFFu) << 16 | groups << 24;
}

/* the stock press or release; for a chord's key with the RECORD flag
 * (UI+0xED) held at 0 -- the only thing either handler reads it for is
 * recording the note, and nothing they call reads it */
void nm_stock(Byte *ui, unsigned key, bool quiet, uintptr_t fn)
{
    const Byte rec = ui[0xED];
    if (quiet)
        ui[0xED] = 0;
    reinterpret_cast<void (*)(Byte *, unsigned)>(fn)(ui, key);
    if (quiet)
        ui[0xED] = rec;
}

uint32_t key_bit(unsigned key) { return key - 34u < 13u ? 1u << (key - 34u) : 0u; }
}  // namespace

extern "C" unsigned np_ui_pressed(void *ui, unsigned key);

/* Replaces 0x0814476C: (handler, &owned event).  The thunk at 0x0811EC6C
 * passes the event already detached from the queue; like the stock body
 * this takes it, does the work and deletes it.  TEMPO+FUNC+note: any
 * instrument. */
extern "C" void np_modnote_random(void *, void **slot)
{
    Byte *ev = static_cast<Byte *>(*slot);
    if (!ev)
        return;
    *slot = nullptr;
    if (*reinterpret_cast<const uint32_t *>(ev) == kRandPatchVt)
        generate(reinterpret_cast<Manager>(0x0811F855u)(), ev[4] | unsigned(ev[5]) << 8, ev[6], 0u);
    destroy(ev);
}

/* R2: in mod-note mode a white key pressed while black keys are held
 * builds an instrument from their groups in that key's mod note, then
 * plays it.  Replaces `b.w 0x0814FE5C` at 0x081521AA, the press handler's
 * tail after its key check (0x0814A3E4 has put the key's mod note in
 * UI+0xE8; a black key never gets here).  The black keys' state is the
 * event consumer's (np_ui.cpp), so it is the state as of this press. */
extern "C" void np_mn_key_press(Byte *ui, unsigned key)
{
    const unsigned note = ui[0xE8];
    const unsigned groups = black_groups(ui, key);
    if (note < 64u) {
        const uint32_t bit = 1u << (note & 31u);
        if (groups)
            mark_word(note) |= bit;
        else
            mark_word(note) &= ~bit;
    }
    if (groups) {
        void *mgr = reinterpret_cast<Manager>(0x0811F855u)();
        generate(mgr, new_tag(mgr), note, groups);
    }
    /* the stock press: held-key list, then the note-on -- synchronously,
     * so what plays is the instrument just built */
    reinterpret_cast<void (*)(Byte *, unsigned)>(0x0814FE5Du)(ui, key);
}

/* K1-K3: TEMPO+FUNC + a black key in mod-note mode.  C# a drum kit over
 * the page, D# a bass kit over the page in the scale, A# every mod note of
 * every page; p56: G# every mod note in the page's style (ST).  Replaces the key check `bl 0x0814A3E4` at 0x081524D4 in the
 * TEMPO+FUNC randomize handler 0x081524CC: for those it builds and answers
 * 0, so the stock creates no event; any other key gets the stock check (a
 * black key still fails it and does nothing).  p53, F#: every mod note's
 * pitch back to C4 (UI+0x98 semitone 0, UI+0xC0 octave 0 -- the state the
 * bass kit and UP+DOWN+key set), the sounds left as they are; the stock
 * page randomizer writes its steps at C4, so they then play as the keys. */
extern "C" unsigned np_mn_chord_check(Byte *ui, unsigned key)
{
    if (key == 40u) {
        for (unsigned n = 0; n < 40u; ++n)
            ui[0x98 + n] = ui[0xC0 + n] = 0;
        return 0u;
    }
    if (key == 35u || key == 37u || key == 42u || key == 44u) {
        void *mgr = reinterpret_cast<Manager>(0x0811F855u)();
        const unsigned tag = new_tag(mgr);
        if (key == 35u)
            generate_kit(mgr, tag, ui[0xE9]);
        else if (key == 37u)
            generate_bass_kit(mgr, tag, ui[0xE9]);
        else
            generate_all(mgr, tag, key == 42u ? ui[0xE9] + 1u : 0u);
        return 0u;
    }
    return reinterpret_cast<unsigned (*)(Byte *, unsigned)>(0x0814A3E5u)(ui, key);
}

/* The RECORD flag (UI+0xED) as the press (0x0814FEB8) and the release
 * (0x081521FE) read it: 0 for a mod note a black + white press built, so
 * that press records no step; the release clears the mark. */
extern "C" unsigned np_mn_rec_flag(const Byte *ui, unsigned note, unsigned release)
{
    unsigned rec = ui[0xED];
    if (note < 64u) {
        volatile uint32_t &w = mark_word(note);
        const uint32_t bit = 1u << (note & 31u);
        if (w & bit) {
            rec = 0;
            if (release)
                w &= ~bit;
        }
    }
    return rec;
}

/* P1: normal mode (UI+0x84 = 1).  TEMPO+FUNC + a white key builds an
 * instrument of any kind into the pattern's patch, + a black key one from
 * its group, a second black key held with it the pair's (as R2, kCombo);
 * then the stock press plays it at that key's note.  Stock, the
 * chord only plays the note.  The thunks 0x08147B94 / 0x08147B9C end in
 * `b.w` to the press (0x08151DA4) and the release (0x08151FFC), (ui, key);
 * those branches come here.  The chord records no step, press or release. */
extern "C" void np_nm_key_press(Byte *ui, unsigned key)
{
    const uint32_t bit = key_bit(key);
    volatile uint32_t &mark = diag(D_MNRAND_PMARK);
    if (bit && ui[0x84] == 1u && np_ui_pressed(ui, 25u) && np_ui_pressed(ui, 26u)) {
        /* the note this press plays (0x0814A3C8): key - 34 + UI+0xEA octave * 12 */
        generate_patch(reinterpret_cast<Manager>(0x0811F855u)(), ui[0xEF],
                       int(key - 34u) + ui[0xEA] * 12, black_groups(ui, key));
        mark |= bit;
    } else {
        mark &= ~bit;
    }
    nm_stock(ui, key, mark & bit, 0x08151DA5u);
}

extern "C" void np_nm_key_release(Byte *ui, unsigned key)
{
    const uint32_t bit = key_bit(key);
    volatile uint32_t &mark = diag(D_MNRAND_PMARK);
    const bool quiet = mark & bit;
    mark &= ~bit;
    nm_stock(ui, key, quiet, 0x08151FFDu);
}

/* SL (p57): the scale library.  In the scale setting (UI mode 3, setting
 * 1), hold a PAGE key and press SEQ 1..16: that page's custom scale
 * becomes library scale page * 16 + seq -- A the East (maqams, ragas,
 * Balkan), B pentatonics and hexatonics, C modes and jazz, D symmetric,
 * Messiaen and chord tones (docs/scale-library.json; none is one of the
 * fifteen built-in scales).  Written the way the stock edit (hold PAGE + a
 * NOTE key) writes it: one SetCustomScaleNoteData (vtable 0x0816425C, +6
 * the custom scale, +7 the note; handler 0x08127B74 toggles it) per note
 * that differs, so it is saved with the project, and under one transaction
 * tag: one undo step.  Bit n is n semitones over the root; the root is
 * always in.  np_ui_switch (np_ui.cpp) catches the press. */
namespace {
const uint16_t kScaleLib[64] = {
    0x5B3, 0x973, 0x673, 0x6D9, 0x9AB, 0xAAB, 0x6D3, 0x575,
    0x5B5, 0x9CB, 0xAD3, 0x9D3, 0x6B3, 0x895, 0x529, 0x4A5,
    0x4A9, 0x295, 0x18D, 0x4A3, 0x463, 0x28D, 0x18B, 0x2A5,
    0x8D1, 0x495, 0x2A9, 0x29D, 0x555, 0x655, 0x999, 0x4D3,
    0xAD5, 0x56B, 0x6D5, 0x55B, 0x6AB, 0xB55, 0x56D, 0x9B5,
    0x66B, 0xB35, 0xAD9, 0x35B, 0x5D5, 0xEB5, 0xBB5, 0x6BD,
    0x6DB, 0xB6D, 0xD53, 0x653, 0xDDD, 0x9E7, 0x8E3, 0xD75,
    0xBEF, 0x091, 0x089, 0x489, 0x491, 0x081, 0x421, 0x007,
};
}

extern "C" void np_scale_library(unsigned page, unsigned seq)
{
    if (page > 3u || seq > 15u)
        return;
    void *mgr = reinterpret_cast<Manager>(0x0811F855u)();
    const Byte *deg = static_cast<const Byte *>(mgr) + 0x6C + (16u + page) * 14u;
    unsigned have = 1u;
    for (unsigned i = 1; i <= deg[0] && i <= 12u; ++i)
        if (deg[i] < 12u)
            have |= 1u << deg[i];
    const unsigned diff = (have ^ kScaleLib[page * 16u + seq]) & 0xFFEu;
    const unsigned tag = new_tag(mgr);
    for (unsigned n = 1; n < 12u; ++n)
        if (diff >> n & 1u)
            if (Byte *e = make(8, 0x0816425Cu, tag, page)) {
                e[7] = Byte(n);
                send(mgr, e);
            }
}
