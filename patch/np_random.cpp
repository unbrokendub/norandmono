/* TEMPO+FUNC parameter randomizer, stock V2.2.0r only.
 * Quantize OSC1/2 coarse and filter cutoff automation at creation,
 * never in the audio ISR.
 * The stock handler retains RNG calls, event ownership and project/undo
 * dispatch. See docs/patches.md, section SR.
 */
#include <stdint.h>

namespace {
using Byte = uint8_t;
using Submit = void (*)(void *, void **);
constexpr uintptr_t kEngine = 0x200047F0u, kSequencer = 0x2000CFF4u;
unsigned u16(const Byte *p) { return p[0] | (unsigned(p[1]) << 8); }
uintptr_t u32(const Byte *p) { return *reinterpret_cast<const uint32_t *>(p); }
float engine(unsigned off) {
    return *reinterpret_cast<const volatile float *>(kEngine + off);
}
bool ram(uintptr_t p, unsigned size) {
    return !(p & 3) && ((p >= 0x20000000u && p <= 0x20020000u - size) ||
                       (p >= 0x30000000u && p <= 0x30040000u - size));
}

// Non-allocating read of the 12-byte per-step lists used by 0x081203D0.
// Nodes own secondary-base pointers (+4) to SequencerEvent<NoteData>.
// Pitch at primary+12 is MIDI*255 (NOT MIDI*256). On a rest, use the last
// preceding normal note, wrapping the pattern. This is a nominal snapshot:
// probability/slide/live notes can change which note actually sounds.
// Return -1 for malformed lists, 0 for no note, 1 for a found note.
int step_note(unsigned bank, unsigned step, unsigned length, float &note) {
    unsigned budget = 2048;
    for (unsigned back = 0; back < length; ++back) {
        const uintptr_t head = kSequencer + 0x8C + bank * 0x63C + step * 12;
        uintptr_t node = *reinterpret_cast<const uint32_t *>(head);
        bool found = false;
        while (node != head) {
            if (!budget-- || !ram(node, 16)) return -1;
            const auto *n = reinterpret_cast<const Byte *>(node);
            uintptr_t event = u32(n + 12);
            if (!ram(event, 16)) return -1;
            const auto *e = reinterpret_cast<const Byte *>(event);
            if (u32(e) == 0x081642C0u && e[4] == bank && e[7] == 0 && e[14] == 255) {
                note = float(u16(e + 8)) * (1.0f / 255.0f);
                found = true;
            }
            node = u32(n);
        }
        if (found) return 1;
        step = step ? step - 1 : length - 1;
    }
    return 0;
}

// Coarse FOLLOW decoding at 0x08115C52/0x08115CBC: 11088 -> 256 raw
// units per semitone (default). This is not a raw/65536 linear amount.
float follow(float raw, unsigned half_range) {
    int value = int(raw);
    unsigned level = unsigned(value < 0 ? -value : value) / half_range / 43;
    const float amount = level ? float(32u << (level - 1)) : 0.0f;
    return value < 0 ? -amount : amount;
}

bool quantize(const Byte *manager, Byte *out, const Byte *request) {
    const unsigned slot = request[10];
    const bool cutoff = slot == 9;
    if ((slot != 0 && slot != 4 && !cutoff) || request[11] != 0) return true;
    const unsigned range = cutoff ? 150 : 127;  // stock randomizer descriptor
    const unsigned id = request[6];
    unsigned bank;
    if (manager[0x183] == id) bank = 0;
    else if (manager[0x184] == id) bank = 1;
    else return true;
    const Byte *pattern = manager + 0x18D + bank * 0x13;
    const unsigned scale = pattern[4], root = pattern[5], length = pattern[0];
    // Factory chromatic retains the existing free-random behavior / opt-out.
    if (scale == 0) return true;
    if (scale >= 20 || root >= 12 || !length || length > 128) return false;
    const Byte *degrees = manager + 0x6C + scale * 14;
    if (!degrees[0] || degrees[0] > 12) return false;
    unsigned mask = 0;
    for (unsigned i = 1; i <= degrees[0]; ++i) {
        if (degrees[i] >= 12) return false;
        mask |= 1u << ((root + degrees[i]) % 12);
    }
    const unsigned step = u16(request + 8);
    if (step >= length || request[12] > request[13]) return false;
    float note = engine(slot == 4 ? 0x5FD0 : 0x5FCC);
    // With an empty pattern retain the current keyboard pitch reference.
    if (step_note(bank, step, length, note) < 0) return false;
    const float base = engine((bank ? 0x47C0 : 0x2FB4) + slot * 36);
    const float raw_tracking = engine((bank ? 0x551C : 0x3D10) + slot * 36);
    const float transpose = engine(bank ? 0x6020 : 0x6004);
    // Stock note-on multiplies by float(1/255), so MIDI 127 can be
    // 127.0000076f. Do not reject that valid endpoint due to FP rounding.
    if (!(note >= 0 && note <= 127.001f && base >= 0 && base <= 65535 &&
          raw_tracking >= -32768 && raw_tracking <= 32767 &&
          transpose >= -128 && transpose <= 128)) return false;
    // Only OSC1/2 force master FOLLOW to 1 (0x08115716/0x0811596C).
    // Cutoff uses engine+0x6044, in addition to its own coarse FOLLOW.
    const float master = cutoff ? engine(0x6044) : 1.0f;
    if (!(master >= 0 && master <= 1)) return false;
    const float tracking = follow(raw_tracking, range >> 1);
    const float note_term = (note + transpose - 57.0f) * tracking;
    const float center = base + note_term * master;
    const int lo = (int(request[12]) - 128) * int(range);
    const int hi = (int(request[13]) - 128) * int(range);
    const int original = int(int16_t(u16(out + 16)));
    int best = 0, distance = 0x7FFFFFFF;
    bool found = false;
    // Filter: raw/256 /149 *149 -> calibration coordinate 0..149.
    // Its calibration is MIDI+12 (440 Hz -> 81), an octave offset which
    // leaves pitch class unchanged. Do not confuse descriptor max=150
    // with the DSP divisor=149. OSC behavior/range remains unchanged.
    // At most 150 candidates; no allocation or IRQ masking.
    const unsigned pitches = cutoff ? 150 : 128;
    for (unsigned pitch = 0; pitch < pitches; ++pitch) {
        if (!(mask & (1u << (pitch % 12)))) continue;
        const float offset = float(pitch * 256) - center;
        if (!(offset >= -32768.0f && offset <= 32767.0f)) continue;
        const int value = int(offset + (offset < 0 ? -0.5f : 0.5f));
        if (value < lo || value > hi) continue;
        int delta = value - original;
        if (delta < 0) delta = -delta;
        if (delta < distance) {
            best = value;
            distance = delta;
            found = true;
        }
    }
    // No scale note inside the user's range: leave that step unchanged.
    if (!found) return false;
    out[16] = Byte(best);
    out[17] = Byte(unsigned(best) >> 8);
    return true;
}
}

extern "C" void np_random_submit(void *manager, void **output, const Byte *request)
{
    if (quantize(static_cast<const Byte *>(manager), static_cast<Byte *>(*output), request))
        reinterpret_cast<Submit>(0x081576D5u)(manager, output);
    // Sent or skipped: stock caller owns and destroys the output.
}
