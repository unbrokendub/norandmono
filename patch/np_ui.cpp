/* UI switch state belongs to the event consumer, not the DMA scanner.
 * Stock helpers read/write SwitchManager's +0x10..0x12 bytes. A newer ISR
 * press resets +0x11 during an older release; the all-up check also sees
 * future releases and erases the modifier mask too early.
 * Keep 48 bytes in reserved pool metadata, outside allocator bitmaps and
 * blocks. np_runtime_init clears the whole page before constructors.
 * Only UI consumption/callbacks write it. No allocation, IRQ masking,
 * producer changes or changes to the queued event layout.
 */
#include <stdint.h>
#include "np_diag.h"

extern "C" void np_ui_stock_switch(void *ui, void **event);
extern "C" void np_diag_dispatch(void *ui, void *event);
extern "C" void np_delete(void *p);

namespace {
using Byte = volatile uint8_t;
constexpr uintptr_t kSwitchEventVtable = 0x0815E238u;
constexpr uintptr_t kStateBase = 0x3003C040u;
constexpr unsigned kCount = 48;
constexpr uint8_t kPressed = 1, kLongHold = 2, kConsumed = 4;
constexpr unsigned kMode = 0x8B, kModeDirty = 0x8D;
Byte *state() { return reinterpret_cast<Byte *>(kStateBase); }

volatile uint32_t &diag(unsigned offset) {
    return *reinterpret_cast<volatile uint32_t *>(0x2000E400u + offset);
}
uint32_t cycles() { return *reinterpret_cast<volatile uint32_t *>(0xE0001004u); }
}

/* Main's UI service used to drain until empty (0x081574F8), before main
 * ever visited ProjectEventManager. A pot burst therefore built a second
 * queue as large as the first, while postponing application of its values.
 * Keep every event, in FIFO order, but yield after eight events / 250 us.
 * A single callback is not preempted by this budget. Backpressure stops
 * feeding the project queue at 32 pending items; np_project_first ensures
 * that a nonzero manager budget always makes progress on the next stage.
 * Only main's call is replaced; other users of the stock drain are intact.
 */
extern "C" void np_ui_service(void *ui)
{
    auto *q = static_cast<volatile uint32_t *>(ui);
    const uint32_t start = cycles();
    for (unsigned n = 0; n < 8; ++n) {
        if (*reinterpret_cast<volatile uint32_t *>(0x2000C6B0u) >= 32u)
            break;
        void *event;
        uint32_t retired = 0, mask;
        // DMA may relocate the deque's map. Publish the complete pop before
        // restoring interrupts, including on a 128-pointer block boundary.
        // No allocator or callback runs with interrupts disabled here.
        __asm volatile("mrs %0, primask\n\tcpsid i" : "=r"(mask) :: "memory");
        const uint32_t cur = q[0x24 / 4];
        if (cur == q[0x34 / 4]) {
            __asm volatile("msr primask, %0" :: "r"(mask) : "memory");
            break;
        }
        auto *cell = reinterpret_cast<volatile uint32_t *>(cur);
        event = reinterpret_cast<void *>(*cell);
        *cell = 0;
        uint32_t next = cur + 4;
        if (next == q[0x2C / 4]) {
            retired = q[0x28 / 4];
            const uint32_t node = q[0x30 / 4] + 4;
            next = *reinterpret_cast<const volatile uint32_t *>(node);
            q[0x30 / 4] = node;
            q[0x28 / 4] = next;
            q[0x2C / 4] = next + 512;
        }
        q[0x24 / 4] = next;
        __asm volatile("msr primask, %0" :: "r"(mask) : "memory");
        if (retired) np_delete(reinterpret_cast<void *>(retired));
        np_diag_dispatch(ui, &event);
        if (event) {
            auto *vtable = *static_cast<uint32_t **>(event);
            reinterpret_cast<void (*)(void *)>(vtable[1])(event);
        }
        if (cycles() - start >= 100000u) break;
    }
    const uint32_t elapsed = cycles() - start;
    if (elapsed > diag(D_UI_SERVICE_MAX)) diag(D_UI_SERVICE_MAX) = elapsed;
    if (q[0x24 / 4] != q[0x34 / 4]) ++diag(D_UI_SERVICE_YIELDS);
}

extern "C" unsigned np_ui_consumed(void *, unsigned key)
{
    return key < kCount && (state()[key] & kConsumed) != 0;
}

extern "C" void np_ui_consume(void *, unsigned key)
{
    if (key < kCount) state()[key] |= kConsumed;
}

extern "C" void np_ui_allow(void *, unsigned key)
{
    if (key < kCount) state()[key] &= ~kConsumed;
}

extern "C" void np_ui_consume_modifier(void *ui, unsigned bit)
{
    // Stock 0x0814A734 writes the driver's flags directly rather than
    // calling its UI setter. Mirror its TBB cases exactly, including the
    // intentional exceptions for FUNC and note keys, and whole key groups.
    if (bit == 0 || bit == 14) return;
    if (bit == 12 || bit == 13) {
        const unsigned first = bit == 12 ? 4 : 20;
        const unsigned end = bit == 12 ? 20 : 24;
        for (unsigned key = first; key < end; ++key) np_ui_consume(ui, key);
    } else if (bit <= 16) {
        const auto *inverse = reinterpret_cast<const uint8_t *>(0x08172B14u);
        np_ui_consume(ui, inverse[bit]);
    }
}

extern "C" unsigned np_ui_long_hold(void *, unsigned key)
{
    return key < kCount && (state()[key] & kLongHold) != 0;
}

extern "C" unsigned np_ui_pressed(void *, unsigned key)
{
    return key < kCount && (state()[key] & kPressed) != 0;
}

extern "C" void np_ui_reconcile(void *ui)
{
    // Preserve all-up recovery, but at the current event's time. Do not
    // rebuild the mask: callbacks consume modifiers; some keys share bits.
    for (unsigned key = 0; key < kCount; ++key)
        if (state()[key] & kPressed) return;
    *reinterpret_cast<volatile uint32_t *>(static_cast<Byte *>(ui) + 0x7C) = 0;
}

extern "C" void np_ui_begin_unlink(void *ui, unsigned)
{
    // Replace the entry callback's toggle only. Re-entering an already
    // active/stale unlink state must not invert the press/release pairing.
    static_cast<Byte *>(ui)[kMode] = 5;
}

extern "C" void np_scale_library(unsigned page, unsigned seq);
extern "C" const uint32_t np_led_white;

/* WL (p62): TEMPO+FOLLOW steps the white LEDs (np_led_scale) through 25,
 * 50 and 100 % (16, 32, 64 of 64).  The writer only sends an LED whose
 * request changed, so the white ones are marked dirty to show the new
 * scale at once (driver 0x2000190C, entry +0x94 + 20 i, dirty at +3). */
namespace {
constexpr unsigned kFollow = 1, kTempo = 25, kFunc = 26;
void led_white_step()
{
    volatile uint32_t &level = diag(D_LED_WHITE);
    const uint32_t now = level ? level : np_led_white;
    level = now < 32u ? 32u : now < 64u ? 64u : 16u;
    auto *drv = reinterpret_cast<volatile uint8_t *>(0x2000190Cu + 0x94u + 3u);
    for (unsigned i = 24; i <= 46; ++i)
        if (i <= 40 || i >= 45)
            drv[20u * i] = 1u;
}
}

extern "C" void np_ui_switch(void *ui, void **event)
{
    auto *panel = static_cast<Byte *>(ui);
    unsigned library = 0;   // SL: page + 1, and the SEQ key from bit 8
    bool led_step = false;  // WL: this press is TEMPO+FOLLOW
    // The dispatcher selected UiEvent<SwitchEventData>. Leave malformed
    // or other event types to the original RTTI/error/ownership path.
    if (event != nullptr && *event != nullptr &&
        *static_cast<const uint32_t *>(*event) == kSwitchEventVtable) {
        const auto *data = static_cast<const uint8_t *>(*event);
        const unsigned key = data[6], kind = data[5];
        if (key < kCount) {
            if (kind == 0) state()[key] = kPressed;
            else if (kind == 1) state()[key] |= kLongHold;
            else if (kind == 2) state()[key] &= ~kPressed;
            // SL (p57): in the scale setting (mode 3, setting 1) a SEQ key
            // pressed with a PAGE key held loads that page's library scale
            // into its custom slot (np_mnrand.cpp).  The stock would take
            // the SEQ key as a built-in scale -- queued, so after anything
            // sent here -- so it sees this one press in mode 0, which has
            // no actions; its bookkeeping and the event's ownership stay.
            if (kind == 0 && key >= 4 && key < 20 && panel[0x84] == 3 && panel[0xEC] == 1)
                for (unsigned page = 0; page < 4; ++page)
                    if (state()[20 + page] & kPressed) {
                        library = (page + 1) | (key - 4) << 8;
                        break;
                    }
            // WL: FOLLOW pressed with TEMPO held (not FUNC: TEMPO+FUNC+
            // FOLLOW is the stock pitchbend randomizer)
            if (kind == 0 && key == kFollow && (state()[kTempo] & kPressed) &&
                !(state()[kFunc] & kPressed))
                led_step = true;
            if (data[4] == 0 && (key == 2 || key == 3) &&
                kind == 2 && panel[kMode] == 5) {
                // Unlike the stock exit's toggle, this is idempotent.
                panel[kMode] = 1;
                panel[kModeDirty] = 1;
                np_ui_consume(ui, 2);
                np_ui_consume(ui, 3);
            }
        }
    }
    // Keep RTTI, maps, modifier updates and the original event destructor.
    // The stock sees an SL or WL press in mode 0, which has no actions.
    const uint8_t mode = panel[0x84];
    if (library || led_step) panel[0x84] = 0;
    np_ui_stock_switch(ui, event);
    if (led_step) {
        panel[0x84] = mode;
        led_white_step();
        np_ui_consume(ui, kFollow);     // and its release toggles no FOLLOW
    }
    if (library) {
        panel[0x84] = 3;
        np_scale_library((library & 0xFF) - 1, library >> 8);
        // The mini keyboard shows the scale only when the view is drawn
        // (0x0814A08C on a mode or view change, the settings renderer
        // 0x0814F3B0 on a value key); draw it now with its own renderer.
        reinterpret_cast<void (*)(void *)>(0x0814ECC9u)(ui);
        // and the key is used, as the stock value handlers mark theirs
        // (0x0814A358): unconsumed, its release would enter a note on
        // that step when the setting is left (p59)
        np_ui_consume(ui, (library >> 8) + 4);
    }
}
