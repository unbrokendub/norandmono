/*
 * np_runtime.cpp -- bounded ISR allocation and low-overhead telemetry.
 *
 * There is no source tree or linker script for the original application, so
 * writable state is deliberately kept in two ranges that the patcher owns:
 *
 *   0x2000E400..0x2000F7FF  diagnostic block in free DTCM (part retained)
 *   0x3003C000..0x3003FFFF  fixed pools carved from the top of the D2 heap
 *
 * tools/patch.py reduces the stock heap limit by exactly 16 KiB.  This is
 * preferable to assuming that an otherwise unused SRAM bank has been clocked
 * and initialised by code for which we have no sources.
 */

#include <stdint.h>
#include "np_diag.h"

extern "C" const uint32_t np_build_id;

#ifndef NP_DSP_OPT
#define NP_DSP_OPT 3
#endif
#ifndef NP_EV_LOG
#define NP_EV_LOG 1
#endif
/* p54: the MIDI CC telemetry (telemetry_service and the words it sends) is
 * a diagnostic build only, tools/patch.py --telemetry: ~4.5 KB of the 512 KB
 * cap.  Without it the sender's helpers go unreferenced (--gc-sections). */
#ifndef NP_TELEMETRY
#define NP_TELEMETRY 1
#endif
#if !NP_TELEMETRY
#pragma GCC diagnostic ignored "-Wunused-function"
#endif

namespace {

constexpr uintptr_t kDiag = 0x2000E400u;
constexpr uintptr_t kDwtCtrl = 0xE0001000u;
constexpr uintptr_t kDwtCyccnt = 0xE0001004u;
constexpr uintptr_t kDwtLar = 0xE0001FB0u;
constexpr uintptr_t kDemcr = 0xE000EDFCu;
constexpr uint32_t kTrcena = 1u << 24;
constexpr uint32_t kCyccntena = 1u;
constexpr uint32_t kDwtUnlock = 0xC5ACCE55u;
constexpr uintptr_t kSpi4Cr1 = 0x40013400u;      /* LED driver transport */
constexpr uintptr_t kSpi4Sr = 0x40013414u;

constexpr uintptr_t kHeapBase = 0x30000000u;
constexpr uintptr_t kHeapLimit = 0x3003C000u;
constexpr uintptr_t kBrkPtr = 0x200000F4u;
constexpr uintptr_t kFreeListHead = 0x2000E19Cu;

/* 16 KiB pool layout. Metadata +0..0x23 contains allocator bitmaps;
 * +0x40..0x6F is the UI consumer's switch shadow (np_ui.cpp).
 * np_runtime_init clears all 256 metadata bytes before constructors. */
constexpr uintptr_t kPoolMeta = 0x3003C000u;
constexpr uintptr_t kSmallBase = 0x3003C100u;
constexpr unsigned kSmallSize = 32u;
constexpr unsigned kSmallCount = 256u;
constexpr unsigned kSmallWords = kSmallCount / 32u;
constexpr uintptr_t kBigBase = 0x3003E200u;
constexpr unsigned kBigSize = 512u;
constexpr unsigned kBigCount = 15u;

/* Size does not imply lifetime: any thread object of these sizes, not
 * just a transient event, can reach this allocator. A busy thread used to pin
 * every block and starve the next ISR allocation (once fatal), despite
 * ample ordinary heap space. Keep a hard address-based reserve that a
 * preempted thread cannot consume. IRQs may use the full pools; threads
 * only the first 192 small / 7 big blocks, then their existing heap
 * fallback. No check-then-allocate race against an occupancy counter. */
constexpr unsigned kSmallThreadCount = 192u;
constexpr unsigned kBigThreadCount = 7u;
static_assert(kSmallThreadCount % 32u == 0u && kSmallThreadCount < kSmallCount,
              "small IRQ reserve must occupy complete bitmap words");
static_assert(kBigThreadCount < kBigCount, "big IRQ reserve must be nonempty");

/* Existing fields through 0x3c are kept ABI-compatible with older tools. */
enum : unsigned {
    D_FORMAT = 0x40, D_CORE_HZ = 0x44, D_DWT_READY = 0x48,
    D_SBRK_FAIL = 0x4c, D_IRQOFF_MAX = 0x50, D_IRQOFF_START = 0x54,
    D_MIDI_Q_HIGH = 0x58, D_MIDI_Q_FULL = 0x5c,
    D_MIDI_TX_BYTES = 0x60,
    D_POOL_SMALL_USED = 0x64, D_POOL_SMALL_HIGH = 0x68,
    D_POOL_BIG_USED = 0x6c, D_POOL_BIG_HIGH = 0x70,
    D_POOL_FAIL = 0x74, D_POOL_BAD_FREE = 0x78,
    D_MALLOC_FAIL = 0x7c, D_HEAP_LIMIT = 0x80,
    D_HEAP_REMAINING = 0x84, D_HEAP_LARGEST = 0x88,
    D_USB_PTR = 0x8c, D_USB_PENDING = 0x90,
    D_USB_DEFERRED = 0x94, D_USB_COALESCED = 0x98,
    D_AUDIO_DEADLINE = 0x9c, D_MAIN_LAST = 0xa0,
    D_HEAP_SAMPLE_LAST = 0xa4, D_DSP_OPT = 0xa8,
    D_POOL_READY = 0xac, D_POOL_THREAD_FALLBACK = 0xb0,
    D_STATS = 0xb8,
    /* telemetry sender state (per boot) */
    D_TL_PHASE = 0x130, D_TL_POS = 0x134, D_TL_NEXT_MS = 0x138, D_TL_SEQ = 0x13c,
    /* (0x140..0x163 are the envelope constants, 0x180.. the RC state) */
    D_TL_WORD = 0x164,            /* the word being sent, latched whole */
    D_TL_BASE = 0x168,            /* PC ring index frozen for this record */
    D_TL_FAULT_SENT = 0x16c,      /* boot records announced this boot */
    D_TL_LAST_MS = 0x170,         /* uwTick when the sender last ran */
    /* retained across warm resets, see np_patch.S */
    D_RSR = 0x420, D_PCIDX = 0x424, D_PCHIST = 0x428,
    D_LASTSTATS = 0x440,          /* copy of 0x60..0x130 from the previous boot */
    D_PCRING = 0x540,             /* 256 words, PC[23:0] | IPSR << 24 */
    D_LASTRING = 0x940,           /* the ring frozen by np_early at this boot */
    D_LASTIDX = 0xd40,
    D_LEDWAIT = 0xd44,            /* LED frames the frame guard held back */
    D_LEDTS = 0xd48,              /* CYCCNT when the last LED frame started */
    D_LEDSENT = 0xd4c,            /* LED frames started (monotonic) */
};

constexpr unsigned kStatWords = 6u;
constexpr unsigned kStatCount = 5u;
enum : unsigned { S_COUNT, S_TOTAL_LO, S_TOTAL_HI, S_MAX, S_OVERRUN, S_LAST };
enum : unsigned { P_AUDIO, P_SYSTICK, P_TIM5, P_DMA1_STREAM0, P_MAIN };   /* P_TIM5: IRQ 50, the sequencer tick (was mislabelled SPI3) */

inline volatile uint32_t &word(uintptr_t address)
{
    return *reinterpret_cast<volatile uint32_t *>(address);
}

inline volatile uint32_t &diag(unsigned offset)
{
    return word(kDiag + offset);
}

inline uint32_t ipsr()
{
    uint32_t v;
    __asm volatile("mrs %0, ipsr" : "=r"(v));
    return v;
}

inline void dmb()
{
    __asm volatile("dmb" ::: "memory");
}

inline uint32_t load_exclusive(volatile uint32_t *p)
{
    uint32_t v;
    __asm volatile("ldrex %0, [%1]" : "=&r"(v) : "r"(p) : "memory");
    return v;
}

inline bool store_exclusive(volatile uint32_t *p, uint32_t v)
{
    uint32_t failed;
    __asm volatile("strex %0, %2, [%1]"
                   : "=&r"(failed) : "r"(p), "r"(v) : "memory");
    return failed == 0u;
}

uint32_t atomic_add(volatile uint32_t *p, uint32_t add)
{
    uint32_t old;
    do {
        old = load_exclusive(p);
    } while (!store_exclusive(p, old + add));
    return old + add;
}

uint32_t atomic_exchange(volatile uint32_t *p, uint32_t value)
{
    uint32_t old;
    do {
        old = load_exclusive(p);
    } while (!store_exclusive(p, value));
    return old;
}

void update_high(unsigned used_offset, unsigned high_offset, int delta)
{
    volatile uint32_t *used = &diag(used_offset);
    uint32_t now = delta > 0 ? atomic_add(used, 1u) : atomic_add(used, ~0u);
    if (delta <= 0)
        return;
    volatile uint32_t *high = &diag(high_offset);
    uint32_t seen = *high;
    while (now > seen) {
        uint32_t actual = load_exclusive(high);
        if (actual >= now) {
            __asm volatile("clrex" ::: "memory");
            break;
        }
        if (store_exclusive(high, now))
            break;
        seen = actual;
    }
}

void *pool_take(uintptr_t bitmap_address, unsigned words,
                uintptr_t block_base, unsigned block_size,
                unsigned count, unsigned used_offset, unsigned high_offset)
{
    auto *bitmap = reinterpret_cast<volatile uint32_t *>(bitmap_address);
    for (unsigned wi = 0; wi < words; ++wi) {
        volatile uint32_t *entry = bitmap + wi;
        for (;;) {
            uint32_t bits = load_exclusive(entry);
            uint32_t free_bits = ~bits;
            if (wi + 1u == words && (count & 31u) != 0u)
                free_bits &= (1u << (count & 31u)) - 1u;
            if (free_bits == 0u) {
                __asm volatile("clrex" ::: "memory");
                break;
            }
            unsigned bit = static_cast<unsigned>(__builtin_ctz(free_bits));
            if (!store_exclusive(entry, bits | (1u << bit)))
                continue;
            unsigned index = wi * 32u + bit;
            update_high(used_offset, high_offset, 1);
            return reinterpret_cast<void *>(block_base + index * block_size);
        }
    }
    return nullptr;
}

bool pool_release(void *p, uintptr_t base, unsigned block_size,
                  unsigned count, uintptr_t bitmap_address,
                  unsigned used_offset, unsigned high_offset)
{
    (void)high_offset;
    uintptr_t address = reinterpret_cast<uintptr_t>(p);
    uintptr_t end = base + block_size * count;
    if (address < base || address >= end)
        return false;
    uintptr_t delta = address - base;
    if ((delta % block_size) != 0u) {
        atomic_add(&diag(D_POOL_BAD_FREE), 1u);
        return true;
    }
    unsigned index = static_cast<unsigned>(delta / block_size);
    volatile uint32_t *entry = reinterpret_cast<volatile uint32_t *>(bitmap_address)
                             + index / 32u;
    uint32_t mask = 1u << (index & 31u);
    for (;;) {
        uint32_t bits = load_exclusive(entry);
        if ((bits & mask) == 0u) {
            __asm volatile("clrex" ::: "memory");
            atomic_add(&diag(D_POOL_BAD_FREE), 1u);
            return true;
        }
        if (store_exclusive(entry, bits & ~mask)) {
            update_high(used_offset, used_offset, -1);
            return true;
        }
    }
}

using MallocFn = void *(*)(uint32_t);
using FreeFn = void (*)(void *);
using NewHandler = void (*)();
using GetNewHandlerFn = NewHandler (*)();
using UsbServiceFn = void (*)(void *);

constexpr uintptr_t kStockMalloc = 0x0815A0C1u;
constexpr uintptr_t kStockFree = 0x0815A0D1u;
constexpr uintptr_t kGetNewHandler = 0x08159B4Du;
constexpr uintptr_t kNewAbort = 0x0815A225u;
constexpr uintptr_t kUsbService = 0x08110245u;

void heap_snapshot()
{
    uintptr_t brk = word(kBrkPtr);
    if (brk < kHeapBase || brk > kHeapLimit)
        return;
    uint32_t tail = static_cast<uint32_t>(kHeapLimit - brk);
    diag(D_HEAP_REMAINING) = tail;
    uint32_t largest = tail > 8u ? tail - 8u : 0u;

    /* The allocator's free list is address-sorted.  No pointer is followed
     * until it has been range-checked, so an interrupt racing this diagnostic
     * sample can make the sample incomplete but cannot send it outside RAM. */
    uintptr_t node = word(kFreeListHead);
    for (unsigned guard = 0; node != 0u && guard < 4096u; ++guard) {
        if (node < kHeapBase || node + 8u > brk || (node & 3u) != 0u)
            return;
        uint32_t size = word(node);
        uintptr_t next = word(node + 4u);
        if (size < 12u || size > brk - node)
            return;
        uint32_t available = size;
        if (node + size == brk)
            available += tail; /* allocator extends the last free block */
        uint32_t payload = available > 8u ? available - 8u : 0u;
        if (payload > largest)
            largest = payload;
        if (next != 0u && (next <= node || next >= brk))
            return;
        node = next;
    }
    diag(D_HEAP_LARGEST) = largest;
}

} /* namespace */

extern "C" void np_runtime_init()
{
    /* This tail is per-boot, unlike the retained PC ring immediately before
     * it. P5 accidentally carried its LED timestamp across warm resets. */
    for (unsigned off = D_LEDWAIT; off < NP_DIAG_SIZE; off += 4u)
        diag(off) = 0u;
    for (unsigned off = 0; off < 72u; off += 4u) {
        diag(D_LED_SUBMITTED + off) = 0xFFFFFFFFu;
        diag(D_LED_COMPLETED + off) = 0xFFFFFFFFu;
    }
    /* Per-boot pool state.  Allocated blocks do not need clearing. */
    auto *meta = reinterpret_cast<volatile uint32_t *>(kPoolMeta);
    for (unsigned i = 0; i < 0x100u / sizeof(uint32_t); ++i)
        meta[i] = 0u;

    diag(D_FORMAT) = 2u;
    diag(D_CORE_HZ) = 400000000u;
    diag(D_DSP_OPT) = NP_DSP_OPT;
    diag(D_HEAP_LIMIT) = kHeapLimit;
    /* np_audio_irq measures the WHOLE stock EXTI2 handler, and that handler
     * renders eight samples per call: the deadline is the buffer interval,
     * 400 MHz / (32 kHz / 8) = 250 us, not the 31.25 us of one sample.  With
     * the single-sample figure here every frame counted as an overrun. */
    diag(D_AUDIO_DEADLINE) = 100000u;

    word(kDemcr) |= kTrcena;
    dmb();
    /* Cortex-M7 implementations may expose the CoreSight lock.  STM32H7
     * accepts this key; an implementation without the lock ignores it. */
    word(kDwtLar) = kDwtUnlock;
    word(kDwtCyccnt) = 0u;
    word(kDwtCtrl) |= kCyccntena;
    dmb();
    diag(D_DWT_READY) = (word(kDwtCtrl) & kCyccntena) != 0u;
    heap_snapshot();
}

extern "C" void *np_new(uint32_t size)
{
    if (size == 0u)
        size = 1u;

    bool in_exception = ipsr() != 0u;
    if (in_exception) {
        void *p = nullptr;
        if (size <= kSmallSize) {
            p = pool_take(kPoolMeta, kSmallWords, kSmallBase, kSmallSize,
                          kSmallCount, D_POOL_SMALL_USED, D_POOL_SMALL_HIGH);
        }
        if (p == nullptr && size <= kBigSize) {
            p = pool_take(kPoolMeta + 0x20u, 1u, kBigBase, kBigSize,
                          kBigCount, D_POOL_BIG_USED, D_POOL_BIG_HIGH);
        }
        if (p != nullptr)
            return p;
        /* Both pools exhausted from an interrupt.  This used to be fatal
         * (udf, fault cause 5, reset), and a mod-note key press -- one
         * burst of a few hundred event objects from the key-scan ISR --
         * hit it on the unit.  The stock firmware serves every ISR
         * allocation from the ordinary heap under the PRIMASK malloc lock,
         * which saves and restores the previous mask, so that path is as
         * safe here as it always was.  Count the episode and use it. */
        atomic_add(&diag(D_POOL_FAIL), 1u);
        for (;;) {
            void *h = reinterpret_cast<MallocFn>(kStockMalloc)(size);
            if (h != nullptr)
                return h;
            atomic_add(&diag(D_MALLOC_FAIL), 1u);
            NewHandler handler = reinterpret_cast<GetNewHandlerFn>(kGetNewHandler)();
            if (handler == nullptr)
                reinterpret_cast<void (*)()>(kNewAbort)();
            else
                handler();
        }
    }

    /* Before main, constructors use the stock heap. Once main starts,
     * small objects and 512-byte deque growth may use only the non-reserved
     * pool partition: long-lived objects can also be created after boot.
     * Other thread allocations stay on the stock heap. */
    bool pool_request = size <= kSmallSize || size == kBigSize;
    if (pool_request && diag(D_POOL_READY) != 0u) {
        void *p = size <= kSmallSize
                ? pool_take(kPoolMeta, kSmallThreadCount / 32u, kSmallBase, kSmallSize,
                            kSmallThreadCount, D_POOL_SMALL_USED, D_POOL_SMALL_HIGH)
                : pool_take(kPoolMeta + 0x20u, 1u, kBigBase, kBigSize,
                            kBigThreadCount, D_POOL_BIG_USED, D_POOL_BIG_HIGH);
        if (p != nullptr)
            return p;
        atomic_add(&diag(D_POOL_THREAD_FALLBACK), 1u);
    }

    for (;;) {
        void *p = reinterpret_cast<MallocFn>(kStockMalloc)(size);
        if (p != nullptr)
            return p;
        atomic_add(&diag(D_MALLOC_FAIL), 1u);
        NewHandler handler = reinterpret_cast<GetNewHandlerFn>(kGetNewHandler)();
        if (handler == nullptr)
            reinterpret_cast<void (*)()>(kNewAbort)();
        else
            handler();
    }
}

extern "C" void np_delete(void *p)
{
    if (p == nullptr)
        return;
    if (pool_release(p, kSmallBase, kSmallSize, kSmallCount, kPoolMeta,
                     D_POOL_SMALL_USED, D_POOL_SMALL_HIGH))
        return;
    if (pool_release(p, kBigBase, kBigSize, kBigCount, kPoolMeta + 0x20u,
                     D_POOL_BIG_USED, D_POOL_BIG_HIGH))
        return;
    reinterpret_cast<FreeFn>(kStockFree)(p);
}

extern "C" void np_prof_record(uint32_t cycles, uint32_t kind)
{
    if (kind >= kStatCount)
        return;
    volatile uint32_t *s = reinterpret_cast<volatile uint32_t *>(
        kDiag + D_STATS + kind * kStatWords * sizeof(uint32_t));
    s[S_COUNT] += 1u;
    uint32_t old = s[S_TOTAL_LO];
    uint32_t sum = old + cycles;
    s[S_TOTAL_LO] = sum;
    if (sum < old)
        s[S_TOTAL_HI] += 1u;
    if (cycles > s[S_MAX])
        s[S_MAX] = cycles;
    if (cycles > diag(D_IMAX + kind * 4u))
        diag(D_IMAX + kind * 4u) = cycles;      /* reset by the transition record */
    s[S_LAST] = cycles;
    if (kind == P_AUDIO && cycles > diag(D_AUDIO_DEADLINE))
        s[S_OVERRUN] += 1u;
}

extern "C" void np_usb_defer(void *usb)
{
    diag(D_USB_PTR) = reinterpret_cast<uintptr_t>(usb);
    dmb();
    if (atomic_exchange(&diag(D_USB_PENDING), 1u) != 0u)
        atomic_add(&diag(D_USB_COALESCED), 1u);
    atomic_add(&diag(D_USB_DEFERRED), 1u);
}

/* SysTick is the sole writer of these fields. No waiting for SPI: a busy
 * frame keeps its dirty flag and is retried on a later scan. EOT proves the
 * STM32 shifted the bytes, NOT that a write-only LED chip accepted them. */
extern "C" bool np_led_ready()
{
    uint32_t sr = word(kSpi4Sr), cr1 = word(kSpi4Cr1);
    uint32_t pending = diag(D_LED_PENDING);
    uint32_t age = word(kDwtCyccnt) - diag(D_LEDTS);
    if (pending && age > diag(D_LED_AGE_MAX))
        diag(D_LED_AGE_MAX) = age;
    if ((sr & 0x3E0u) != 0u || (cr1 & 1u) == 0u) {
        diag(D_LED_FAULT) += 1u;
        diag(D_LED_ERROR_SR) = sr;
        return false;
    }
    if ((cr1 & 0x200u) != 0u || (pending && (sr & 8u) == 0u)) {
        diag(D_LED_BUSY) += 1u;
        return false;
    }
    if (pending) {
        reinterpret_cast<volatile uint8_t *>(kDiag + D_LED_COMPLETED)[pending & 0x7Fu]
            = static_cast<uint8_t>(pending >> 8);
        diag(D_LED_EOT) += 1u;
        diag(D_LED_PENDING) = 0u;
    }
    if (diag(D_DWT_READY) != 0u) {
        if (diag(D_LED_HAS_STARTED) != 0u && age < 8000u) {
            diag(D_LEDWAIT) += 1u;
            return false;
        }
    } else {
        /* Failed DWT startup: conservative fixed >=20 us at 400 MHz,
         * bounded even with a frozen counter, after hardware is idle.
         * No escape hatch that can raise CS while CSTART is still set. */
        unsigned delay = 8000u;
        __asm volatile("1: nop\n\tsubs %0, #1\n\tbne 1b" : "+r"(delay) :: "cc");
    }
    return true;
}

extern "C" void np_led_started(const uint8_t *driver)
{
    diag(D_LEDTS) = word(kDwtCyccnt);   /* after CSTART: conservative time */
    unsigned i = driver[0x60c], value = driver[0x92];
    if (i < 70u) {
        auto *last = reinterpret_cast<volatile uint8_t *>(kDiag + D_LED_SUBMITTED);
        if (last[i] != value)
            diag(D_LED_CHANGED) += 1u;
        last[i] = value;
        diag(D_LED_PENDING) = 0x80000000u | (value << 8) | i;
    }
    diag(D_LEDSENT) += 1u;
    diag(D_LED_HAS_STARTED) = 1u;     /* does not wrap with the frame count */
}

extern "C" void np_diag_enqueue(void *object, const uint8_t *const *event)
{
    if (reinterpret_cast<uintptr_t>(object) != 0x2000DE54u)
        return;
    atomic_add(&diag(D_UI_ENQUEUE), 1u);  /* attempts, before stock insertion */
    const uint8_t *e = *event;
    if (e == nullptr)
        return;
    uint32_t vt = *reinterpret_cast<const uint32_t *>(e);
    if (vt == 0x0815E0C0u) {
        atomic_add(&diag(D_UI_POTS), 1u);
        if (e[6] < 23u) atomic_add(&diag(D_POT_EVENTS + 4u * e[6]), 1u);
        diag(D_UI_LAST_POT) = (static_cast<uint32_t>(e[6]) << 16)
                           | *reinterpret_cast<const uint16_t *>(e + 8);
    } else if (vt == 0x0815E238u) {
        atomic_add(&diag(D_UI_SWITCHES), 1u);
        diag(D_UI_LAST_SWITCH) = e[4] | (e[5] << 8) | (e[6] << 16);
    }
}

extern "C" void np_diag_dispatch(void *object, void *event)
{
    bool ui = reinterpret_cast<uintptr_t>(object) == 0x2000DE54u;
    if (ui)
        diag(D_UI_DISPATCH) += 1u;
    reinterpret_cast<void (*)(void *, void *)>(0x0815748Du)(object, event);
    if (ui)
        diag(D_UI_DISPATCH_DONE) += 1u;
}

/* ------------------------------------------------------------------------
 * Telemetry over MIDI.
 *
 * The unit has no debug port the user can reach, so the diagnostic block is
 * pushed out as MIDI control changes on channel 16, which any MIDI monitor
 * on the host records.  Encoding: a tag CC (119) announces a record, then
 * every 32-bit word is five CCs 102..106, MSB first (4 + 7 + 7 + 7 + 7
 * bits).  tools/diagmidi.py decodes a MIDI Monitor export.
 *
 *   record 1  boot: magic, boots, RCC_RSR, fault record, the previous
 *             boot's pool/heap/DWT numbers (D_LASTSTATS), PC histogram
 *   record 2  the whole retained PC ring, oldest first (256 words)
 *   record 3/4: legacy heartbeat and base-brightness diagnostic callers
 *   record 5: normal P6 heartbeat every 3 s, 95 frozen words with full word
 *             indices, build identity, real TX/EOT witnesses and CRC32.
 *             Its first data CC is 32+(word_index>>3), not 102.
 *
 * The boot record and the ring go out once, ~2 s after boot, when the
 * previous reset was not a power-on (so a watchdog reset after a hang
 * delivers the last 64 ms of PC samples) or a fault was recorded.  Messages
 * are written into the stock 4-byte output ring (0x0811A904 fans it out to
 * DIN and USB) a few at a time and only while there is room, so none of
 * the stock's own traffic is displaced.
 * --------------------------------------------------------------------- */
namespace {

constexpr uintptr_t kMidiObject = 0x0811A6A5u;   /* instance getter */
constexpr unsigned kMidiRing = 0x408u, kMidiTail = 0x808u, kMidiHead = 0x80Cu;
constexpr uint8_t kTlChannel = 0xBFu;            /* CC, channel 16 */
constexpr uint8_t kTlTag = 119u, kTlData0 = 102u;
constexpr uintptr_t kUwTick = 0x20000174u;
constexpr uintptr_t kUi = 0x2000DE54u;
constexpr uintptr_t kKeyShadow = 0x3003C040u;
constexpr uintptr_t kSystemInstance = 0x08109615u;
constexpr uintptr_t kSystemDriver = 0x0810977Du;
constexpr unsigned kLedDriverSlot = 4u;
constexpr uintptr_t kRefreshFlag = 0x2000B864u;
constexpr uintptr_t kProject = 0x200047F0u;      /* the project singleton */
constexpr unsigned kSlotParamBase = 0x181Cu;     /* + slot*9 + local -> CParam* */
constexpr uint32_t kRsrPowerOn = (1u << 23) | (1u << 21);   /* PORRSTF, BORRSTF */
constexpr uint32_t kTlMagic = 0x4E50544Cu;       /* 'NPTL' */
constexpr unsigned kRingWords = 256u;
constexpr unsigned kHeartbeatMs = 30000u;  /* the 616-CC full snapshot every
                                            * 30 s only: MIDI Monitor keeps ~1000
                                            * messages and the 1 s transition
                                            * record (91 CCs) must own that window.
                                            * (was 3 s) diagnosing a freeze: a short
                                            * capture must contain several */
constexpr unsigned kBootDelayMs = 2000u;
/* CCs per millisecond, not per main-loop pass.  It used to be six per pass,
 * and a pass took 3-12 ms, so a record trickled out and the USB host read
 * it in between.  Once the audio block was cut to half its budget the main
 * loop ran a pass every few hundred microseconds, a 91-CC record was in the
 * ring within two milliseconds, and the stock USB-MIDI stack delivered
 * exactly 64 of every burst and dropped the rest -- every transition record
 * came back 12 words of 18 with one timestamp on all of them.  Two per
 * millisecond is a sixth of the old peak and smoother than it: a record is
 * 46 ms, the full snapshot 320 ms, the boot record with its ring 710 ms. */
constexpr unsigned kBurst = 2u;

enum : uint32_t { TL_IDLE, TL_BOOT_HDR, TL_RING, TL_HEARTBEAT, TL_LEDS, TL_P6, TL_TR,
                  TL_EV, TL_PM };      /* the phase number is the record's tag */

typedef void *(*InstanceFn)();
typedef void *(*DriverFn)(void *, unsigned);

inline uint32_t &tl(unsigned off) { return *reinterpret_cast<uint32_t *>(kDiag + off); }

/* One CC into the stock output ring; false when it is too full. */
bool midi_cc(uint8_t number, uint8_t value)
{
    auto *m = static_cast<uint8_t *>(reinterpret_cast<InstanceFn>(kMidiObject)());
    if (m == nullptr)
        return false;
    uint32_t primask;
    __asm volatile("mrs %0, primask\n\tcpsid i" : "=r"(primask) :: "memory");
    uint32_t &head = *reinterpret_cast<uint32_t *>(m + kMidiHead);
    uint32_t tail = *reinterpret_cast<uint32_t *>(m + kMidiTail);
    /* Leave the stock producers three quarters of the ring.  The main-loop
     * MIDI producer's full-ring race is still unfixed, and the sequencer's
     * clock (24 ppqn) shares this ring: filling it to the brim made the
     * telemetry lose whole CCs in a capture from the unit. */
    bool ok = ((tail - head - 1u) & 0xFFu) >= 192u;
    if (ok) {
        uint8_t *slot = m + kMidiRing + (head & 0xFFu) * 4u;
        slot[0] = 0x0Bu;                          /* USB-MIDI CIN: control change */
        slot[1] = kTlChannel;
        slot[2] = number & 0x7Fu;
        slot[3] = value & 0x7Fu;
        head = (head + 1u) & 0xFFu;
    }
    __asm volatile("msr primask, %0" :: "r"(primask) : "memory");
    return ok;
}

/* A word as five CCs.  Returns how many are out (0..5); the caller resumes
 * from `done` when the ring was full. */
/* The first fragment only needs four bits for the word's MSBs, and a CC
 * value carries seven: bits 4..6 hold the word's own index mod 8, so a
 * receiver that loses CCs can tell how many words went missing and keep the
 * remaining ones on their proper field.  Without it one dropped CC shifted
 * every field after it by one, silently. */
unsigned midi_word(uint32_t w, unsigned done, unsigned limit, unsigned wi, bool full_index)
{
    static const uint8_t shift[5] = { 28u, 21u, 14u, 7u, 0u };
    unsigned n = done;
    while (n < 5u && limit != 0u) {
        uint8_t v = (w >> shift[n]) & 0x7Fu;
        if (n == 0u)
            v = static_cast<uint8_t>(((wi & 7u) << 4) | (v & 0x0Fu));
        /* P6 first controller encodes the high index bits. Same five CCs,
         * but losing 8/16/... whole words no longer shifts the record. */
        uint8_t cc = (n == 0u && full_index) ? 32u + (wi >> 3) : kTlData0 + n;
        if (!midi_cc(cc, v))
            break;
        ++n;
        --limit;
    }
    return n;
}

uint32_t last_stat(unsigned kind, unsigned field)
{
    return tl(D_LASTSTATS + (D_STATS - 0x60u) + kind * kStatWords * 4u + field * 4u);
}

uint32_t live_stat(unsigned kind, unsigned field)
{
    return tl(D_STATS + kind * kStatWords * 4u + field * 4u);
}

uint32_t boot_word(unsigned i)
{
    switch (i) {
    case 0:  return kTlMagic;
    case 1:  return tl(0x2c);                      /* boots */
    case 2:  return tl(D_RSR);
    case 3:  return tl(0x04);                      /* fault kind */
    case 4:  return tl(0x0c);                      /* stacked PC */
    case 5:  return tl(0x10);                      /* stacked LR */
    case 6:  return tl(0x18);                      /* CFSR */
    case 7:  return tl(0x20);                      /* faults since power on */
    case 8:  return tl(0x34);                      /* heap max ever */
    case 9:  return tl(D_LASTSTATS + (D_POOL_FAIL - 0x60u));
    case 10: return tl(D_LASTSTATS + (D_POOL_SMALL_HIGH - 0x60u));
    case 11: return tl(D_LASTSTATS + (D_POOL_BIG_HIGH - 0x60u));
    case 12: return tl(D_LASTSTATS + (D_MALLOC_FAIL - 0x60u));
    case 13: return tl(D_LASTSTATS + (D_HEAP_REMAINING - 0x60u));
    case 14: return last_stat(P_AUDIO, S_MAX);
    case 15: return last_stat(P_AUDIO, S_OVERRUN);
    case 16: return last_stat(P_AUDIO, S_COUNT);
    case 17: return last_stat(P_MAIN, S_MAX);
    case 18: return last_stat(P_MAIN, S_COUNT);
    case 19: return last_stat(P_SYSTICK, S_MAX);
    case 20: return tl(D_PCHIST);
    case 21: return tl(D_PCHIST + 4);
    case 22: return tl(D_PCHIST + 8);
    case 23: return tl(D_PCHIST + 12);
    case 24: return tl(D_LASTIDX);
    case 25: return tl(0x14);                      /* stacked xPSR: which
                                                      IRQ was running       */
    case 26: return tl(0x1c);                      /* HFSR: FORCED means the
                                                      escalated fault is in
                                                      CFSR, DEBUGEVT/VECTTBL
                                                      mean it is not         */
    case 27: return tl(0x08);                      /* the faulting SP        */
    default: return 0u;
    }
}
constexpr unsigned kBootWords = 28u;

/* The previous session's ring as np_early froze it, oldest sample first. */
uint32_t ring_word(unsigned i)
{
    unsigned idx = (tl(D_LASTIDX) + i) & 0xFFu;
    return tl(D_LASTRING + idx * 4u);
}

/* The live ring's newest `n` samples end at D_PCIDX-1; `back` counts from
 * the newest (0) towards the oldest. */
/* Relative to the index frozen when this record started, so the 24 samples
 * a heartbeat carries are a consecutive history even though the sampler
 * keeps writing the ring while the record is being sent. */
uint32_t live_sample(unsigned back)
{
    unsigned idx = (tl(D_TL_BASE) - 1u - back) & 0xFFu;
    return tl(D_PCRING + idx * 4u);
}

uint32_t shadow_bits(unsigned half)
{
    const volatile uint8_t *k = reinterpret_cast<const volatile uint8_t *>(kKeyShadow);
    uint32_t bits = 0u;
    for (unsigned i = 0u; i < 24u; ++i)
        if (k[half * 24u + i] & 1u)
            bits |= 1u << i;
    return bits;
}

constexpr unsigned kHeartbeatWords = 63u;
constexpr unsigned kLedWords = 18u;          /* 70 brightness bytes, 4 per word */

/* Legacy tag 4: base brightness only, NOT pulse/blink/overlay or actual TX.
 * Kept for old diagnostic callers. Normal P6 sends the actual TX shadows. */
uint32_t led_word(unsigned i)
{
    void *sys = reinterpret_cast<InstanceFn>(kSystemInstance)();
    auto *led = static_cast<const uint8_t *>(reinterpret_cast<DriverFn>(kSystemDriver)(sys, kLedDriverSlot));
    uint32_t v = 0u;
    for (unsigned n = 0u; n < 4u; ++n) {
        unsigned idx = i * 4u + n;
        uint32_t b = idx < 70u ? led[0x94u + idx * 20u] : 0u;
        v |= (b & 0xFFu) << (n * 8u);
    }
    return v;
}

/* The parameter indicators, stage by stage, at one instant.  The SysTick
 * renderer 0x0814F80C takes the slot from UI+0x92, reads the CParam pointer
 * for locals 0..4 from project + 4*(0x181C + slot*9 + local) + 4, scales
 * [ptr+0xC] by 65535 (bipolar locals 1 and 4 as distance from 32767) and
 * sends bits 11..18 to LEDs 18..22.  Reporting what the renderer would
 * compute next to what the LED entry actually holds, and next to the raw
 * floats, says which link stops following the knob. */
uint32_t slot_param(unsigned slot, unsigned local)
{
    if (slot >= 18u || local >= 9u)
        return 0u;
    uint32_t p = word(kProject + 4u * (kSlotParamBase + slot * 9u + local) + 4u);
    return (p >= 0x20000000u && p < 0x38010000u) ? p : 0u;
}

uint32_t param_led(unsigned slot, unsigned local)
{
    uint32_t p = slot_param(slot, local);
    if (p == 0u)
        return 63u;                              /* no such parameter */
    float f;
    uint32_t bits = word(p + 0xCu);
    __builtin_memcpy(&f, &bits, sizeof f);
    float v = f * 65535.0f;
    if (local == 1u || local == 4u) {
        v -= 32767.0f;
        if (v < 0.0f)
            v = -v;
    }
    uint32_t u = v <= 0.0f ? 0u : (v >= 65535.0f ? 65535u : static_cast<uint32_t>(v));
    return (u >> 11) & 0x1Fu;
}

uint32_t led_requested(unsigned n)
{
    void *sys = reinterpret_cast<InstanceFn>(kSystemInstance)();
    auto *led = static_cast<const uint8_t *>(reinterpret_cast<DriverFn>(kSystemDriver)(sys, kLedDriverSlot));
    return led[0x94u + n * 20u] & 0x3Fu;
}

uint32_t heartbeat_word(unsigned i)
{
    const uint8_t *ui = reinterpret_cast<const uint8_t *>(kUi);
    switch (i) {
    case 0:  return kTlMagic ^ 1u;
    case 1:  return tl(D_TL_SEQ);
    case 2:  return word(kUwTick);
    case 3:  return tl(D_PCHIST);
    case 4:  return tl(D_PCHIST + 4);
    case 5:  return tl(D_PCHIST + 12);
    case 6:  return live_stat(P_AUDIO, S_MAX);
    case 7:  return live_stat(P_AUDIO, S_OVERRUN);
    case 8:  return live_stat(P_AUDIO, S_LAST);
    case 9:  return live_stat(P_MAIN, S_MAX);
    case 10: return live_stat(P_MAIN, S_COUNT);
    case 11: return tl(D_POOL_SMALL_USED);
    case 12: return tl(D_POOL_SMALL_HIGH);
    case 13: return tl(D_POOL_FAIL);
    case 14: return tl(D_MALLOC_FAIL);
    case 15: return tl(D_HEAP_REMAINING);
    case 16: return tl(D_LEDWAIT);
    /* mode, page, view (1 = parameter LEDs refreshed at 1 kHz), override */
    case 17: return static_cast<uint32_t>(ui[0x84]) | (static_cast<uint32_t>(ui[0x8b]) << 8)
                  | (static_cast<uint32_t>(ui[0x8f]) << 16) | (static_cast<uint32_t>(ui[0x91]) << 24);
    case 18: return *reinterpret_cast<const uint32_t *>(ui + 0x7c);   /* modifier mask */
    case 19: return *reinterpret_cast<const uint32_t *>(ui + 0x330);  /* held keys */
    case 20: return shadow_bits(0);
    case 21: return shadow_bits(1);
    case 22: {
        void *sys = reinterpret_cast<InstanceFn>(kSystemInstance)();
        auto *led = static_cast<const uint8_t *>(reinterpret_cast<DriverFn>(kSystemDriver)(sys, kLedDriverSlot));
        /* LED writer index, main-loop refresh flag, the UI's own dirty
         * flags (+0x320 render requested, +0x8e view dirty, +0x87 page
         * dirty, +0x8d), the mod note under the cursor */
        return static_cast<uint32_t>(led[0x60c]) | ((word(kRefreshFlag) & 0xFFu) << 8)
             | (static_cast<uint32_t>((ui[0x320] & 1u) | ((ui[0x8e] & 1u) << 1)
                                     | ((ui[0x87] & 1u) << 2) | ((ui[0x8d] & 1u) << 3)) << 16)
             | (static_cast<uint32_t>(ui[0xe8]) << 24);
    }
    case 23: return tl(D_PCIDX);
    /* The LED transport itself: whether frames keep being started, and what
     * the peripheral thinks.  Only status registers are read -- never TXDR
     * or RXDR, which would disturb the transfer. */
    case 24: return tl(D_LEDSENT);
    case 25: return word(kSpi4Sr);
    case 26: return word(kSpi4Cr1);
    /* Which half of the LED path died.  Entry i of the driver's 70 LEDs is
     * at +0x94 + 20*i: requested brightness at +0, dirty at +3, the value
     * the writer has smoothed towards it at +12.  The renderer only writes
     * `requested`; only the writer clears `dirty` and moves `smoothed`.
     *   requested moves, dirty stays high, smoothed frozen -> transport dead
     *   requested frozen, dirty falls to 0, smoothed catches up -> renderer dead
     *   nothing moves at all                                  -> both stopped */
    /* The UI's own event deque (iterators at kUi +0x1C..+0x40, 128 pointers
     * per block): how many events are waiting, and where the two cursors sit
     * in the block map.  A consumer that has stopped draining shows a depth
     * that only grows; a corrupted start iterator shows a node outside the
     * map.  This is the measurement that separates "the UI is not being fed"
     * from "the UI is not drawing". */
    case 29: {
        auto *q = reinterpret_cast<const uint32_t *>(kUi + 0x1c);
        uint32_t map = q[0], msize = q[1];
        uint32_t scur = q[2], sfirst = q[3], snode = q[5];
        uint32_t fcur = q[6], ffirst = q[7], fnode = q[9];
        if (map == 0u || msize == 0u || snode < map || fnode < map)
            return 0xFFFFFFFFu;                 /* iterators out of the map */
        uint32_t blocks = (fnode - snode) / 4u;
        return blocks * 128u + (fcur - ffirst) / 4u - (scur - sfirst) / 4u;
    }
    case 30: {
        auto *q = reinterpret_cast<const uint32_t *>(kUi + 0x1c);
        uint32_t map = q[1] ? q[0] : 0u;
        uint32_t snode = q[5], fnode = q[9];
        /* map capacity, and each cursor's index in it; 0xFF = outside */
        uint32_t si = (map && snode >= map) ? (snode - map) / 4u : 0xFFu;
        uint32_t fi = (map && fnode >= map) ? (fnode - map) / 4u : 0xFFu;
        return (q[1] & 0xFFu) | ((si & 0xFFu) << 8) | ((fi & 0xFFu) << 16);
    }
    case 31: return live_stat(P_MAIN, S_LAST);   /* the most recent main period */
    /* Who else K squeezes.  The key/pot scanner is DMA1_Stream0 and the
     * transport tick is IRQ50; both sit at NVIC priority 2, under the CV
     * engine at 1, so if anything is being starved it shows here.  A scanner
     * count far off 4 kHz, or a maximum near its own period, is starvation. */
    case 32: return live_stat(P_SYSTICK, S_MAX);
    case 33: return live_stat(P_DMA1_STREAM0, S_COUNT);
    case 34: return live_stat(P_DMA1_STREAM0, S_MAX);
    case 35: {                                   /* slot, renderer's values 0..3 */
        unsigned slot = ui[0x92];
        return static_cast<uint32_t>(slot) | (param_led(slot, 0) << 8) | (param_led(slot, 1) << 13)
             | (param_led(slot, 2) << 18) | (param_led(slot, 3) << 23);
    }
    case 36: {                                   /* renderer's value 4, LEDs 18..21 as held */
        unsigned slot = ui[0x92];
        return param_led(slot, 4) | (led_requested(18) << 5) | (led_requested(19) << 11)
             | (led_requested(20) << 17) | (led_requested(21) << 23);
    }
    case 37: { uint32_t p = slot_param(ui[0x92], 0); return p ? word(p + 0xCu) : 0xFFFFFFFFu; }
    case 38: { uint32_t p = slot_param(ui[0x92], 1); return p ? word(p + 0xCu) : 0xFFFFFFFFu; }
    case 27: case 28: {
        void *sys = reinterpret_cast<InstanceFn>(kSystemInstance)();
        auto *led = static_cast<const uint8_t *>(reinterpret_cast<DriverFn>(kSystemDriver)(sys, kLedDriverSlot));
        uint32_t requested = 0u, smoothed = 0u, dirty = 0u;
        for (unsigned n = 0u; n < 70u; ++n) {
            const uint8_t *e = led + 0x94u + n * 20u;
            requested += e[0];
            dirty += e[3] & 1u;
            float f;
            __builtin_memcpy(&f, e + 12, sizeof f);
            smoothed += (f > 0.0f) ? static_cast<uint32_t>(f) : 0u;
        }
        if (i == 27u)
            return requested;
        return dirty | (smoothed << 8);
    }
    default: return live_sample(kHeartbeatWords - 1u - i);   /* the last 24, oldest first */
    }
}

/* Freeze a complete record BEFORE transmission, not one field per packet.
 * Collection is preemptible: snapshot_cycles reports the collection window,
 * not a claim that all 95 words were observed at the same instant. */
void freeze_p6()
{
    uint32_t start = word(kDwtCyccnt);
    auto *s = reinterpret_cast<volatile uint32_t *>(kDiag + D_TL_SNAPSHOT);
    static_assert(D_TL_SNAPSHOT + NP_TL_WORDS * 4u <= D_POTSPAN,
                  "P6 snapshot overlaps live pot spans");
    s[0] = 0x4E505436u; s[1] = 8u; s[2] = np_build_id;
    s[3] = tl(D_TL_SEQ); s[4] = word(kUwTick);
    auto *pri = reinterpret_cast<const volatile uint8_t *>(0xE000E400u);
    s[6] = pri[8] | (pri[11] << 8) | (pri[50] << 16)
         | (static_cast<uint32_t>(*reinterpret_cast<const volatile uint8_t *>(0xE000ED23u)) << 24);
    s[7] = live_stat(P_MAIN, S_COUNT); s[8] = live_stat(P_MAIN, S_LAST);
    s[9] = live_stat(P_MAIN, S_MAX); s[10] = live_stat(P_AUDIO, S_COUNT);
    s[11] = live_stat(P_AUDIO, S_LAST); s[12] = live_stat(P_AUDIO, S_MAX);
    s[13] = live_stat(P_AUDIO, S_OVERRUN); s[14] = live_stat(P_SYSTICK, S_COUNT);
    s[15] = live_stat(P_SYSTICK, S_MAX); s[16] = live_stat(P_DMA1_STREAM0, S_COUNT);
    s[17] = live_stat(P_DMA1_STREAM0, S_MAX);
    static const uint16_t offsets[] = {D_UI_ENQUEUE, D_UI_DISPATCH, D_UI_DISPATCH_DONE,
        D_UI_POTS, D_UI_SWITCHES, D_UI_LAST_POT, D_UI_LAST_SWITCH, D_UI_RENDER};
    for (unsigned i = 0; i < 8u; ++i) s[18u + i] = diag(offsets[i]);
    s[26] = heartbeat_word(17); s[27] = heartbeat_word(18);
    s[28] = word(kSpi4Cr1 + 4u); s[29] = shadow_bits(0); s[30] = shadow_bits(1);
    s[31] = heartbeat_word(22);
    /* A deque producer can relocate its map. Snapshot geometry together;
     * neither helper dereferences map/block pointers. */
    uint32_t mask;
    __asm volatile("mrs %0, primask\n\tcpsid i" : "=r"(mask) :: "memory");
    s[32] = heartbeat_word(29); s[33] = heartbeat_word(30);
    s[34] = word(kSpi4Sr); s[35] = word(kSpi4Cr1);
    __asm volatile("msr primask, %0" :: "r"(mask) : "memory");
    /* schema 8: word 36 was SPI4 CFG1, a constant 0x30000007 in every
     * capture; it now counts project journal writes this boot, and word 43
     * (was the LED observation age, a constant since boot) holds the
     * longest one in cycles.  Same 128 words, same CRC. */
    s[36] = diag(0xF94u);        s[37] = word(kSpi4Cr1 + 12u);
    static const uint16_t transport[] = {D_LEDSENT, D_LED_EOT, D_LED_BUSY,
        D_LEDWAIT, D_LED_FAULT, 0xF98u /* longest journal write */, D_LED_ERROR_SR, D_LED_CHANGED,
        D_POOL_SMALL_USED, D_POOL_FAIL, D_MALLOC_FAIL, D_HEAP_REMAINING,
        D_IRQOFF_MAX, D_HEAP_LARGEST};
    for (unsigned i = 0; i < 14u; ++i) s[38u + i] = diag(transport[i]);
    for (unsigned i = 0; i < 18u; ++i) {
        s[52u + i] = diag(D_LED_SUBMITTED + i * 4u);
        s[70u + i] = diag(D_LED_COMPLETED + i * 4u);
    }
    __asm volatile("mrs %0, primask\n\tcpsid i" : "=r"(mask) :: "memory");
    uint32_t idx = diag(D_PCIDX);
    for (unsigned i = 0; i < 6u; ++i)
        s[88u + i] = diag(D_PCRING + ((idx - 6u + i) & 255u) * 4u);
    __asm volatile("msr primask, %0" :: "r"(mask) : "memory");
    /* schema 2: the parameter indicators stage by stage -- the selected
     * slot, what the renderer would compute for locals 0..4, what LED
     * entries 18..21 actually hold, and the raw floats behind locals 0/1 */
    for (unsigned i = 0; i < 4u; ++i)
        s[94u + i] = heartbeat_word(35u + i);
    /* schema 3: the pot scanner's input side.  Every SysTick sets DMA1 S0
     * EN and ADC1 ADSTART; the stream moves three conversions to the buffer
     * at driver2+0x6C and its TC handler 0x0810F208 folds them into the
     * 32-sample ring of each pot in the current mux row (record = driver2 +
     * pot*104: ring index +0xBA, average +0xBC, current +0x72, last
     * published +0x76) and enqueues an event only when the average moves
     * more than 384 counts and system+0x1A6D is zero.  A frozen ring index
     * means rows are not being processed; a frozen average with a moving
     * knob means the ADC input is dead; ADC1_ISR.OVR names the reason. */
    {
        void *sysp = reinterpret_cast<InstanceFn>(kSystemInstance)();
        auto *scan = static_cast<const volatile uint8_t *>(reinterpret_cast<DriverFn>(kSystemDriver)(sysp, 2u));
        auto *gate = static_cast<const volatile uint8_t *>(sysp) + 0x1A6Du;
        const volatile uint8_t *p18 = scan + 18u * 104u, *p12 = scan + 12u * 104u;
        auto h16 = [](const volatile uint8_t *q) -> uint32_t { return q[0] | (q[1] << 8); };
        uint32_t buf = *reinterpret_cast<const volatile uint32_t *>(scan + 0x6Cu);
        auto *raw = reinterpret_cast<const volatile uint16_t *>(buf);
        s[98] = (word(0x40022000u) & 0xFFFu) | (static_cast<uint32_t>(*gate) << 12)
              | ((word(0x40022008u) & 0xFFu) << 16) | (static_cast<uint32_t>(scan[0xA32]) << 24);
        s[99] = (word(0x40020000u) & 0xFFFFu) | ((word(0x40020010u) & 0x1Fu) << 16)
              | ((word(0x40020014u) & 0xFFu) << 24);
        s[100] = h16(p18 + 0x72) | (h16(p18 + 0x76) << 16);
        s[101] = h16(p18 + 0xBC) | (h16(p18 + 0xBA) << 16);
        s[102] = (buf >= 0x30000000u && buf < 0x30048000u) ? (raw[0] | (raw[1] << 16)) : 0xFFFFFFFFu;
        s[103] = ((buf >= 0x30000000u && buf < 0x30048000u) ? raw[2] : 0xFFFFu) | (h16(p12 + 0xBC) << 16);
        s[104] = word(0x4002200Cu);
    }
    /* schema 4: patch O's counters -- ticks on which the re-arm found the
     * previous row unconsumed (skipped), and re-arms that cleared OVR */
    s[105] = diag(D_SCANSKIP);
    s[106] = diag(D_SCANOVR);
    /* schema 5: who holds the small pool.  On the unit a healthy session
     * sits at 80..120 occupied blocks and every frozen capture at exactly
     * 192 -- the thread partition full -- so the objects that fill it are
     * the question.  Read the first word of every occupied block (a vtable
     * for a polymorphic object) and keep the four most common. */
    s[107] = diag(D_POOL_THREAD_FALLBACK);
    s[108] = diag(D_POOL_BIG_USED) | (diag(D_POOL_BIG_HIGH) << 16);
    {
        /* explicit stores: an aggregate initialiser becomes a memset call
         * and there is no libc in the blob */
        uint32_t keys[4], counts[4];
        for (unsigned k = 0u; k < 4u; ++k) { keys[k] = 0u; counts[k] = 0u; }
        uint32_t other = 0u;
        auto *bitmap = reinterpret_cast<const volatile uint32_t *>(kPoolMeta);
        for (unsigned i = 0u; i < kSmallCount; ++i) {
            if ((bitmap[i / 32u] >> (i % 32u) & 1u) == 0u)
                continue;
            uint32_t first = word(kSmallBase + i * kSmallSize);
            unsigned k = 0u;
            for (; k < 4u; ++k)
                if (counts[k] != 0u && keys[k] == first) { ++counts[k]; break; }
            if (k == 4u) {
                for (k = 0u; k < 4u; ++k)
                    if (counts[k] == 0u) { keys[k] = first; counts[k] = 1u; break; }
                if (k == 4u)
                    ++other;
            }
        }
        for (unsigned k = 0u; k < 4u; ++k) {
            s[109u + 2u * k] = keys[k];
            s[110u + 2u * k] = counts[k];
        }
        s[117] = other;
        s[118] = diag(D_P18MIN);             /* pot 18 min|max since the last snapshot */
        diag(D_P18MIN) = 0u;
    }
    /* schema 6: the project event manager at 0x2000C68C.  Every parameter
     * change and every sequencer event is submitted as a ProjectEvent on its
     * list (+0x1C, count +0x24) and applied by 0x08157650 from the main loop
     * under a wall-clock budget (+0x54, microseconds) that is checked before
     * every item.  A backlog here is a parameter change that has not been
     * applied yet -- and a note that has not sounded. */
    {
        constexpr uintptr_t kMgr = 0x2000C68Cu;
        s[119] = word(kMgr + 0x24u);
        s[120] = word(kMgr + 0x54u);
        uint32_t head = kMgr + 0x1Cu, node = word(head), n = 0u;
        while (node != head && n < 4096u && node >= 0x20000000u && node < 0x38010000u) {
            node = word(node);
            ++n;
        }
        s[121] = n;
    }
    s[122] = diag(D_UI_SERVICE_MAX);
    s[123] = diag(D_UI_SERVICE_YIELDS);
    s[124] = diag(D_PROJECT_FIRST);
    uint32_t busiest = 0u, potmask = 0u, potid = 255u;
    for (unsigned pot = 0; pot < 23u; ++pot) {
        const uint32_t count = atomic_exchange(&diag(D_POT_EVENTS + 4u * pot), 0u);
        if (count) potmask |= 1u << pot;
        if (count > busiest) { busiest = count; potid = pot; }
    }
    s[125] = (potid << 24) | (busiest > 0xFFFFFFu ? 0xFFFFFFu : busiest);
    s[126] = potmask;
    s[5] = word(kDwtCyccnt) - start;
    uint32_t crc = ~0u;
    for (unsigned i = 0; i < NP_TL_WORDS - 1u; ++i) {
        uint32_t v = s[i];
        for (unsigned byte = 0; byte < 4u; ++byte) {
            crc ^= v & 255u; v >>= 8;
            for (unsigned bit = 0; bit < 8u; ++bit)
                crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
        }
    }
    s[NP_TL_WORDS - 1u] = ~crc;
}

uint32_t p6_word(unsigned i) { return diag(D_TL_SNAPSHOT + i * 4u); }

/* The transition record: small enough to send every second, so a capture
 * saved a few seconds after a freeze still holds the records from before
 * it.  Interval maxima (not since-boot), and which pot actually moved the
 * most -- the ADC's own reading, event or no event -- with whether its row
 * was processed meanwhile. */
void freeze_tr()
{
    auto *s = reinterpret_cast<volatile uint32_t *>(kDiag + D_TR_SNAP);
    void *sysp = reinterpret_cast<InstanceFn>(kSystemInstance)();
    auto *scan = static_cast<const volatile uint8_t *>(reinterpret_cast<DriverFn>(kSystemDriver)(sysp, 2u));
    const uint8_t *ui = reinterpret_cast<const uint8_t *>(kUi);
    unsigned best = 0u; uint32_t bestspan = 0u;
    for (unsigned pot = 0u; pot < 24u; ++pot) {
        uint32_t sp = diag(D_POTSPAN + pot * 4u);
        uint32_t span = sp ? (sp >> 16) - (sp & 0xFFFFu) : 0u;
        if (span > bestspan) { bestspan = span; best = pot; }
    }
    const volatile uint8_t *rec = scan + best * 104u;
    auto h16 = [](const volatile uint8_t *q) -> uint32_t { return q[0] | (q[1] << 8); };
    uint32_t ring = h16(rec + 0xBA);
    uint32_t pots = diag(D_UI_POTS), disp = diag(D_UI_DISPATCH), skips = diag(D_SCANSKIP);
    s[0] = 0x4E505452u;                                   /* 'NPTR' */
    s[1] = word(kUwTick);
    s[2] = tl(D_TR_SEQ);
    s[3] = best | (bestspan << 8) | ((((ring - diag(D_TR_LASTRING)) & 0xFFFFu) > 255u ? 255u
                                       : ((ring - diag(D_TR_LASTRING)) & 0xFFu)) << 24);
    s[4] = h16(rec + 0x72) | (h16(rec + 0x76) << 16);    /* that pot: current | published */
    s[5] = ((pots - diag(D_TR_LASTPOTS)) & 0xFFFFu) | (((disp - diag(D_TR_LASTDISP)) & 0xFFFFu) << 16);
    s[6] = (word(0x40022000u) & 0xFFFu) | ((skips - diag(D_TR_LASTSKIP)) & 0xFFu) << 12
         | (static_cast<uint32_t>(scan[0xA32]) << 20) | (diag(D_SCANOVR) & 0xFFu) << 24;
    for (unsigned k = 0u; k < 5u; ++k) {                  /* interval maxima */
        s[7u + k] = diag(D_IMAX + k * 4u);
        diag(D_IMAX + k * 4u) = 0u;
    }
    s[12] = diag(D_POOL_SMALL_USED) | (diag(D_POOL_FAIL) << 16);
    s[13] = static_cast<uint32_t>(ui[0x84]) | (ui[0x8b] << 8) | (ui[0x8f] << 16) | (ui[0x91] << 24);
    s[14] = static_cast<uint32_t>(ui[0x92]) | (led_requested(18) << 8) | (led_requested(19) << 14)
          | (led_requested(20) << 20) | (led_requested(21) << 26);
    { uint32_t p = slot_param(ui[0x92], 1); s[15] = p ? word(p + 0xCu) : 0xFFFFFFFFu; }
    s[16] = (heartbeat_word(29) & 0xFFFFu) | (word(0x2000C68Cu + 0x24u) << 16);
    uint32_t crc = ~0u;
    for (unsigned i = 0; i < NP_TR_WORDS - 1u; ++i) {
        uint32_t v = s[i];
        for (unsigned byte = 0; byte < 4u; ++byte) {
            crc ^= v & 255u; v >>= 8;
            for (unsigned bit = 0; bit < 8u; ++bit)
                crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
        }
    }
    s[NP_TR_WORDS - 1u] = ~crc;
    diag(D_TR_LASTPOTS) = pots; diag(D_TR_LASTDISP) = disp; diag(D_TR_LASTSKIP) = skips;
    diag(D_TR_LASTRING) = ring;
    for (unsigned pot = 0u; pot < 24u; ++pot)
        diag(D_POTSPAN + pot * 4u) = 0u;
    diag(D_P18MIN) = 0u;
}

uint32_t tr_word(unsigned i) { return diag(D_TR_SNAP + i * 4u); }

/* CRC-32 (IEEE, reflected) over n words, little endian -- zlib.crc32. */
uint32_t crc_words(const volatile uint32_t *s, unsigned n)
{
    uint32_t crc = ~0u;
    for (unsigned i = 0; i < n; ++i) {
        uint32_t v = s[i];
        for (unsigned byte = 0; byte < 4u; ++byte) {
            crc ^= v & 255u; v >>= 8;
            for (unsigned bit = 0; bit < 8u; ++bit)
                crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
        }
    }
    return ~crc;
}

/* EV record (tag 7): the oldest unsent entries of the X-Env event log,
 * up to NP_EV_PER_RECORD.  Copied with interrupts masked: the audio
 * handler may be ORing a bit into the newest entry. */
bool freeze_ev()
{
    auto *s = reinterpret_cast<volatile uint32_t *>(kDiag + D_EV_SNAP);
    const auto *ring = reinterpret_cast<const volatile uint32_t *>(kDiag + D_EV_RING);
    uint32_t mask;
    __asm volatile("mrs %0, primask\n\tcpsid i" : "=r"(mask) :: "memory");
    uint32_t head = diag(D_EV_HEAD), sent = diag(D_EV_SENT);
    if (head == sent) {
        __asm volatile("msr primask, %0" :: "r"(mask) : "memory");
        return false;
    }
    if (head - sent > NP_EV_RING_N) {
        diag(D_EV_LOST) += head - sent - NP_EV_RING_N;
        sent = head - NP_EV_RING_N;
    }
    unsigned n = head - sent;
    if (n > NP_EV_PER_RECORD)
        n = NP_EV_PER_RECORD;
    s[0] = 0x4E504556u;                                   /* 'NPEV' */
    s[1] = word(kUwTick);
    s[2] = n | ((diag(D_EV_LOST) & 0xFFFFu) << 16);
    s[3] = sent;                                          /* index of the first */
    for (unsigned k = 0; k < NP_EV_PER_RECORD; ++k) {
        unsigned slot = (sent + k) & (NP_EV_RING_N - 1u);
        for (unsigned w = 0; w < 3u; ++w)
            s[4u + 3u * k + w] = k < n ? ring[3u * slot + w] : 0u;
    }
    diag(D_EV_SENT) = sent + n;
    __asm volatile("msr primask, %0" :: "r"(mask) : "memory");
    s[NP_EV_WORDS - 1u] = crc_words(s, NP_EV_WORDS - 1u);
    return true;
}

uint32_t ev_word(unsigned i) { return diag(D_EV_SNAP + i * 4u); }

/* PM record (tag 8): what is in the pattern.  Both tracks of the pattern
 * store (sequencer 0x2000CFF4, +0x8C and +0x6C8, 64 step lists each), one
 * byte per step: bit 0 a note, 1 a parameter lock/automation, 2 a param
 * trig (unlinked X-Env), 3 XoX, 4 expressivity, 5 anything else.  Then up
 * to 14 param trigs as track << 31 | step << 24 | bytes 5..7 of the event
 * (byte 7 is the slot).  Main thread only -- the store is main's. */
void freeze_pm()
{
    auto *s = reinterpret_cast<volatile uint32_t *>(kDiag + D_PM_SNAP);
    constexpr uintptr_t kSeq = 0x2000CFF4u;
    const uint8_t *mgr = static_cast<const uint8_t *>(
        reinterpret_cast<InstanceFn>(0x0811F855u)());
    for (unsigned i = 0; i < NP_PM_WORDS; ++i)
        s[i] = 0u;
    s[0] = 0x4E50504Du;                                   /* 'NPPM' */
    s[1] = word(kUwTick);
    s[2] = mgr ? (mgr[0x183] | (mgr[0x184] << 8)) : 0xFFFFu;
    s[3] = *reinterpret_cast<const volatile uint16_t *>(kSeq + 0x8Cu + 0x614u)
         | (static_cast<uint32_t>(*reinterpret_cast<const volatile uint16_t *>(kSeq + 0x6C8u + 0x614u)) << 16);
    s[4] = word(0x2000AF84u);                             /* transport position */
    unsigned trigs = 0;
    for (unsigned track = 0; track < 2u; ++track) {
        const uintptr_t base = kSeq + (track ? 0x6C8u : 0x8Cu);
        for (unsigned step = 0; step < 64u; ++step) {
            const uintptr_t head = base + step * 0xCu;
            uintptr_t p = word(head);
            uint32_t flags = 0;
            for (unsigned guard = 0; p != head && guard < 64u; ++guard) {
                if (p < 0x20000000u || p >= 0x38010000u) { flags |= 0x20u; break; }
                const uintptr_t ev = word(p + 0xCu);
                const uint32_t vt = (ev >= 0x20000000u && ev < 0x38010000u) ? word(ev) : 0u;
                switch (vt) {
                case 0x081642A4u: case 0x081642C0u: flags |= 0x01u; break;   /* NoteData */
                case 0x081642D8u: case 0x081642F4u: flags |= 0x02u; break;   /* ParamAutomationData */
                case 0x08164374u: case 0x08164390u:                           /* ParamTrigData */
                    flags |= 0x04u;
                    if (trigs < 14u)
                        s[37u + trigs++] = (track << 31) | (step << 24) | (word(ev + 4u) >> 8);
                    break;
                case 0x08164340u: case 0x0816435Cu: flags |= 0x08u; break;   /* XoXData */
                case 0x0816430Cu: case 0x08164328u: flags |= 0x10u; break;   /* ExpressivityData */
                default: flags |= 0x20u; break;
                }
                p = word(p);
            }
            s[5u + track * 16u + step / 4u] |= flags << (8u * (step & 3u));
        }
    }
    s[NP_PM_WORDS - 1u] = crc_words(s, NP_PM_WORDS - 1u);
}

uint32_t pm_word(unsigned i) { return diag(D_PM_SNAP + i * 4u); }

/* Advance the sender by at most kBurst CCs.  D_TL_POS packs the word index
 * (bits 3..) and how many of its five CCs are already out (bits 0..2). */
void telemetry_pump(uint32_t phase, unsigned nwords, uint32_t (*wordfn)(unsigned), uint32_t next_phase)
{
    constexpr uint32_t kTagSent = 1u << 31;
    uint32_t pos = tl(D_TL_POS);
    unsigned budget = kBurst;
    if ((pos & kTagSent) == 0u) {
        if (!midi_cc(kTlTag, static_cast<uint8_t>(phase)))
            return;
        pos = kTagSent;
        tl(D_TL_POS) = pos;
        --budget;
    }
    while (budget != 0u) {
        unsigned wi = (pos & ~kTagSent) >> 3, done = pos & 7u;
        if (wi >= nwords) {
            tl(D_TL_POS) = 0u;
            tl(D_TL_BASE) = tl(D_PCIDX);
            tl(D_TL_PHASE) = next_phase;
            return;
        }
        /* A burst is only kBurst CCs, so a five-fragment word is routinely
         * split across services.  Latch it whole on the first fragment:
         * re-reading a live counter for the tail would put out a value that
         * never existed (and, with the fragments straddling a carry, one
         * that decodes to nothing like either). */
        if (done == 0u)
            tl(D_TL_WORD) = wordfn(wi);
        unsigned now = midi_word(tl(D_TL_WORD), done, budget, wi,
                                 phase == TL_P6 || phase == TL_TR || phase == TL_EV || phase == TL_PM);
        budget -= (now - done);
        if (now < 5u) {
            tl(D_TL_POS) = kTagSent | (wi << 3) | now;   /* ring full or budget spent */
            return;
        }
        pos = kTagSent | ((wi + 1u) << 3);
        tl(D_TL_POS) = pos;
    }
}

void telemetry_service()
{
    uint32_t ms = word(kUwTick);
    /* once per millisecond, whatever the main loop's pace -- see kBurst */
    if (ms == tl(D_TL_LAST_MS))
        return;
    tl(D_TL_LAST_MS) = ms;
    switch (tl(D_TL_PHASE)) {
    case TL_IDLE:
        if (tl(D_TL_NEXT_MS) == 0u) {              /* first pass this boot */
            tl(D_TL_NEXT_MS) = ms + kBootDelayMs;
            return;
        }
        if (static_cast<int32_t>(ms - tl(D_TL_NEXT_MS)) < 0) {
            /* between full snapshots: X-Env events first -- they are what
             * a capture is for in this build -- at most every 20 ms */
            /* (not before the first heartbeat decision: after a warm reset
             * the boot record and the PC ring must go out first) */
            const bool booted = tl(D_TL_SEQ) != 0u;
            if (NP_EV_LOG && booted && diag(D_EV_HEAD) != diag(D_EV_SENT) && ms - diag(D_EV_LAST_MS) >= 20u) {
                diag(D_EV_LAST_MS) = ms;
                if (freeze_ev()) {
                    tl(D_TL_POS) = 0u;
                    tl(D_TL_PHASE) = TL_EV;
                    return;
                }
            }
            /* the one-second transition record -- also only once the
             * first heartbeat decision is made: it used to go out at +1 s,
             * ahead of a warm reset's boot record at +2 s */
            if (!booted)
                return;
            if (diag(D_TR_NEXT_MS) == 0u)
                diag(D_TR_NEXT_MS) = ms + 1000u;
            if (static_cast<int32_t>(ms - diag(D_TR_NEXT_MS)) >= 0) {
                diag(D_TR_NEXT_MS) = ms + 1000u;
                tl(D_TR_SEQ) += 1u;
                freeze_tr();
                tl(D_TL_POS) = 0u;
                tl(D_TL_PHASE) = TL_TR;
                return;
            }
            /* the pattern map, every five seconds */
            if (NP_EV_LOG && booted && ms - diag(D_PM_LAST_MS) >= 5000u) {
                diag(D_PM_LAST_MS) = ms;
                freeze_pm();
                tl(D_TL_POS) = 0u;
                tl(D_TL_PHASE) = TL_PM;
            }
            return;
        }
        tl(D_TL_POS) = 0u;
        tl(D_TL_BASE) = tl(D_PCIDX);
        /* After a warm reset (watchdog, fault, software) the previous
         * session's ring is worth more than anything live.
         *
         * Announced on the first EIGHT heartbeats of the boot, not just the
         * first.  The fault record outlives the reset it caused -- np_early
         * clears from D_FORMAT up, and the record is below it -- so it is
         * still true four minutes later, and a capture is almost never
         * running at the moment the unit comes back.  Every capture so far
         * has started mid-session and missed the only announcement.  Eight
         * chances at 30 s each; a cold boot clears the record and reports
         * nothing. */
        {
            bool warm = (tl(D_RSR) & kRsrPowerOn) == 0u && tl(D_RSR) != 0u;
            if ((warm || tl(0x04) != 0u) && tl(D_TL_FAULT_SENT) < 8u) {
                tl(D_TL_FAULT_SENT) += 1u;
                tl(D_TL_PHASE) = TL_BOOT_HDR;
            } else {
                tl(D_TL_PHASE) = TL_P6;
            }
        }
        tl(D_TL_SEQ) += 1u;
        if (tl(D_TL_PHASE) == TL_P6)
            freeze_p6();
        tl(D_TL_NEXT_MS) = ms + kHeartbeatMs;
        break;
    case TL_BOOT_HDR:
        telemetry_pump(TL_BOOT_HDR, kBootWords, boot_word, TL_RING);
        break;
    case TL_RING:
        telemetry_pump(TL_RING, kRingWords, ring_word, TL_IDLE);
        break;
    case TL_HEARTBEAT:
        telemetry_pump(TL_HEARTBEAT, kHeartbeatWords, heartbeat_word, TL_LEDS);
        break;
    case TL_LEDS:
        telemetry_pump(TL_LEDS, kLedWords, led_word, TL_IDLE);
        break;
    case TL_P6:
        telemetry_pump(TL_P6, NP_TL_WORDS, p6_word, TL_IDLE);
        break;
    case TL_TR:
        telemetry_pump(TL_TR, NP_TR_WORDS, tr_word, TL_IDLE);
        break;
    case TL_EV:
        telemetry_pump(TL_EV, NP_EV_WORDS, ev_word, TL_IDLE);
        break;
    case TL_PM:
        telemetry_pump(TL_PM, NP_PM_WORDS, pm_word, TL_IDLE);
        break;
    default:
        tl(D_TL_PHASE) = TL_IDLE;
        break;
    }
}

} /* namespace */

extern "C" void np_choke_flush();

extern "C" void np_main_service()
{
    /* CH: a held note-on whose fade has run out goes now */
    if (diag(D_CHOKE_PEND) != 0u && diag(D_CHOKE_N) == 0u)
        np_choke_flush();

    uint32_t now = word(kDwtCyccnt);
    uint32_t last = diag(D_MAIN_LAST);
    diag(D_MAIN_LAST) = now;
    if (last != 0u)
        np_prof_record(now - last, P_MAIN);
    diag(D_POOL_READY) = 1u;

    if (atomic_exchange(&diag(D_USB_PENDING), 0u) != 0u) {
        dmb();
        void *usb = reinterpret_cast<void *>(diag(D_USB_PTR));
        if (usb != nullptr)
            reinterpret_cast<UsbServiceFn>(kUsbService)(usb);
    }

    uintptr_t brk = word(kBrkPtr);
    if (brk >= kHeapBase && brk <= kHeapLimit)
        diag(D_HEAP_REMAINING) = static_cast<uint32_t>(kHeapLimit - brk);

    uint32_t sampled = diag(D_HEAP_SAMPLE_LAST);
    now = word(kDwtCyccnt);
    if (now - sampled >= 400000000u) {
        diag(D_HEAP_SAMPLE_LAST) = now;
        heap_snapshot();
    }

    /* Pot 18 (AMOUNT of the selected slot) as the scanner currently reads
     * it, tracked so a snapshot carries the whole span of the last three
     * seconds, not one instant: a knob swept on a frozen panel either shows
     * up here (the ADC sees it; the fault is downstream) or it does not (the
     * ADC is blind).  Once per millisecond, not once per pass: a pot's
     * reading moves at most every 2 ms (one mux row per SysTick, eight
     * rows), and since S1/S2 a main pass is ~3,000 instructions, of which
     * this walk over 24 pots was ~15 %. */
#if NP_TELEMETRY
    if (word(kUwTick) != diag(D_POTSPAN_MS)) {
        diag(D_POTSPAN_MS) = word(kUwTick);
        auto in_ram = [](uintptr_t a) { return a >= 0x20000000u && a < 0x38010000u; };
        uintptr_t sysp = reinterpret_cast<uintptr_t>(reinterpret_cast<InstanceFn>(kSystemInstance)());
        uintptr_t scan = in_ram(sysp)
            ? reinterpret_cast<uintptr_t>(reinterpret_cast<DriverFn>(kSystemDriver)(reinterpret_cast<void *>(sysp), 2u))
            : 0u;
        if (in_ram(scan)) {
            for (unsigned pot = 0u; pot < 24u; ++pot) {
                const volatile uint8_t *rec = reinterpret_cast<const volatile uint8_t *>(scan + pot * 104u + 0x72u);
                uint32_t cur = rec[0] | (rec[1] << 8);
                uint32_t span = diag(D_POTSPAN + pot * 4u);
                uint32_t lo = span & 0xFFFFu, hi = span >> 16;
                if (span == 0u) { lo = cur; hi = cur; }
                if (cur < lo) lo = cur;
                if (cur > hi) hi = cur;
                diag(D_POTSPAN + pot * 4u) = lo | (hi << 16);
                if (pot == 18u)
                    diag(D_P18MIN) = lo | (hi << 16);
            }
        }
    }
    telemetry_service();
#endif
}

/* ---------------------------------------------------------------------
 * Q1: no direct flash access while the QSPI operation queue is busy.
 *
 * FUNC+RECORD saves the project asynchronously: 0x081262FC serialises it
 * into 0x24000000 and queues a coroutine on driver 8 that erases (status
 * polling by interrupt, the driver's Borrowable read-modify-write buffer
 * held across the wait) and programs page by page by DMA, resumed from
 * main() at 0x08119A0E.  A project load is queued the same way.  Five
 * project events reach the flash directly and synchronously instead: the
 * copy/paste of a patch parameter, a patch, a pattern, a mod note (when
 * the source is another project: save this one, read that one, read this
 * one back) and of a whole project (two saves).  Arriving while a queued
 * operation is still in flight, the first erase finds the buffer borrowed
 * -- "Borrowable already borrowed", abort(), `b .` in main: the stock
 * freezes for good, this firmware reboots after the 16 s watchdog -- and
 * a sync command finding the HAL busy goes to Error_Handler's bkpt.
 * Reproduced on sysemu (tests/test_qspi_defer.py).
 *
 * The project service (0x08157650) calls this for the event at the head
 * of its list; nonzero leaves it there and ends the pass.  main() runs
 * the queue at the top of the next pass, so the paste is applied, in
 * order, once the queued operation has finished.
 * --------------------------------------------------------------------- */
extern "C" uint32_t np_qspi_defer(const void *event)
{
    if (event == nullptr)
        return 0u;
    switch (*static_cast<const uint32_t *>(event)) {
    case 0x081702F0u:   /* ProjectEvent<CopyPastePatchParamData> */
    case 0x08170308u:   /* ProjectEvent<CopyPastePatchData>      */
    case 0x08170320u:   /* ProjectEvent<CopyPasteProjectData>    */
    case 0x081648CCu:   /* ProjectEvent<CopyPastePatternData>    */
    case 0x08172D54u:   /* ProjectEvent<CopyPasteModNoteData>    */
        break;
    default:
        return 0u;
    }
    void *sys = reinterpret_cast<InstanceFn>(kSystemInstance)();
    auto *q = static_cast<const uint8_t *>(reinterpret_cast<DriverFn>(kSystemDriver)(sys, 8u)) + 8;
    /* std::deque of 8-byte coroutine handles at q+0x48: start.cur (+0x50)
     * == finish.cur (+0x60) is empty -- the processor pops an element only
     * once its coroutine has completed. */
    uint32_t start = *reinterpret_cast<const volatile uint32_t *>(q + 0x50);
    uint32_t finish = *reinterpret_cast<const volatile uint32_t *>(q + 0x60);
    const auto *pl = reinterpret_cast<const volatile uint8_t *>(0x2000E400u + D_PLOAD);
    if (start == finish && pl[0] == pl[2])      /* PL: nor while a load waits */
        return 0u;
    diag(D_QSPI_DEFER) += 1u;
    return 1u;
}

/* ---------------------------------------------------------------------
 * PL: a project loaded while the sequencer plays takes over at the end of
 * the playing pattern, from its first step -- the way a pattern change
 * does.  The stock: SEQ in LOAD mode (0x08149DC6) calls 0x0811F8DC, which
 * queues a read of the slot's 512 KB into the store image at 0x24000000;
 * the read's completion (0x081244D0, an std::function invoker whose
 * functor is the manager) replays the journal there (0x08124054), and the
 * whole project changes wherever the transport stands -- the tracks keep
 * their positions, mid-pattern.
 *
 * With the transport running a request counts (D_PLOAD byte 0) and its
 * completion counts (byte 1) without replaying.  main's project service
 * (np_proj_service, at 0x08119A70, before the ticks are processed) replays
 * once every requested read is in and track 0 is in the last step of its
 * pattern (kLead ticks), then puts both tracks as far before the end of their
 * new patterns: step 1 of the new project falls where step 1 of the old one
 * would have.  Until then the image holds the new project while the old one
 * plays, so nothing that serialises or reads it may run: a promoted pattern
 * change is dropped, FUNC+SAVE is ignored (np_save_gate, np_patch.S) and
 * the flash-touching pastes wait (np_qspi_defer).  Stopped: as stock.
 * --------------------------------------------------------------------- */
namespace {
constexpr uintptr_t kTransport = 0x2000AF78u;  /* +0x6C: running            */
constexpr uintptr_t kSeq = 0x2000CFF4u;        /* track t at +t*36: +0x44,
                                                * +0x48 position in ticks,
                                                * +0x60 pattern length      */
constexpr uint32_t kLead = 224u;               /* into the last step: the
                                                * step's notes fire once the
                                                * position is past their
                                                * tick (a swap at exactly
                                                * the step's tick lost its
                                                * note, sysemu), so 32 ticks
                                                * in (16 ms at 120 BPM); the
                                                * rest of the step is the
                                                * replay's: 0.3 M
                                                * instructions for a small
                                                * project, 41 M (~100 ms)
                                                * with 40 full mod notes    */
using ReplayFn = void (*)(void *);

inline volatile uint8_t *pload() { return reinterpret_cast<volatile uint8_t *>(0x2000E400u + D_PLOAD); }
inline bool transport_running() { return *reinterpret_cast<volatile const uint8_t *>(kTransport + 0x6C) != 0u; }
} /* namespace */

using LoadFn = void (*)(void *, unsigned);

/* The read itself is one polled HAL_QSPI_Receive of the whole 512 KB in
 * main (0x08103E9C; QPI fast read at kernel/12): ~63 ms on the unit, 101
 * ms in sysemu, and every step that falls in it comes late -- the stock's
 * load stumbles the moment it is asked for.  Playing, the read waits for
 * the start of a step (byte 3: the slot | 0x80) and has the step to run in;
 * not the last step, which is the replay's. */
extern "C" void np_load_request(void *mgr, unsigned slot)
{
    if (!transport_running()) {
        reinterpret_cast<LoadFn>(0x0811F8DDu)(mgr, slot);
        return;
    }
    volatile uint8_t *pl = pload();
    pl[0] += 1u;
    pl[3] = uint8_t(slot | 0x80u);
}

extern "C" void np_load_done(void *const *functor)
{
    volatile uint8_t *pl = pload();
    if (pl[0] == pl[1]) {                       /* not a synced request: stock */
        reinterpret_cast<ReplayFn>(0x08124055u)(*functor);
        return;
    }
    pl[1] += 1u;
}

extern "C" void np_proj_service(uint8_t *mgr)
{
    volatile uint8_t *pl = pload();
    if (pl[0] != pl[2]) {
        mgr[0x7BC] = 0xFFu;                     /* no pattern change meanwhile */
        auto *seq = reinterpret_cast<uint8_t *>(kSeq);
        const uint32_t pos = *reinterpret_cast<volatile uint32_t *>(seq + 0x48);
        const uint32_t length = *reinterpret_cast<volatile uint16_t *>(seq + 0x60);
        const uint32_t left = length - pos;
        const bool playing = transport_running();
        const uint32_t into = pos & 0xFFu;      /* ticks into the step */
        /* not in the last step -- unless it is the only one */
        if ((pl[3] & 0x80u) && (!playing || (into >= 16u && into < 64u && (left > 256u || length <= 256u)))) {
            const unsigned slot = pl[3] & 0x7Fu;
            pl[3] = 0u;
            reinterpret_cast<LoadFn>(0x0811F8DDu)(mgr, slot);
        }
        if (pl[1] == pl[0] && (!playing || left <= kLead)) {
            reinterpret_cast<ReplayFn>(0x08124055u)(mgr);
            mgr[0x18A] = 0u;                    /* and none queued from before */
            mgr[0x18C] = 0u;
            for (unsigned t = 0; playing && t < 2u; ++t) {
                uint8_t *tr = seq + t * 36u;
                const uint32_t n = *reinterpret_cast<volatile uint16_t *>(tr + 0x60);
                const uint32_t p = n > left ? n - left : 0u;
                *reinterpret_cast<volatile uint32_t *>(tr + 0x44) = p;
                *reinterpret_cast<volatile uint32_t *>(tr + 0x48) = p;
            }
            pl[2] = pl[0];
        }
    }
    reinterpret_cast<ReplayFn>(0x08125AC5u)(mgr);
}

/* ---------------------------------------------------------------------
 * PB: slot 0 failing its checks at power-on starts an empty project.
 *
 * main loads slot 0 at power-on (0x081199AE: bl 0x08124440, the read and
 * the replay).  A journal that fails one of the replay's checks -- 0xA1
 * no EOF (with PJ, a save cut short by a power loss), 0xA2 the pattern
 * table, 0xA3, and PJ's 0xA4 for a first event that is no event at all --
 * went to the error screen 0x0814FA54, which never returns: the code on
 * SEQ 1-8, keys 30+31 send the project out over USB, then b .  At power-on
 * that was every power-on, with no way out.  During the power-on load the
 * failure is noted and the replay goes on (0xA4 returns from it); then the
 * image buffer gets an empty journal -- a zero header, SOF, EOF -- which is
 * replayed, and all 64 patterns get 16 steps (an empty journal leaves them
 * at 0, and a rotate of a 0-step pattern overran its store: RG).  Slot 0 in flash stays as it is until a save.  Any later load
 * keeps the stock's screen (the watchdog restarts the unit from it).
 * --------------------------------------------------------------------- */
namespace {
inline volatile uint8_t *pboot() { return reinterpret_cast<volatile uint8_t *>(kDiag + D_PBOOT); }
constexpr uintptr_t kPatternParamVt = 0x0815EE94u;   /* ProjectEvent<PatternParamData> */
} /* namespace */

extern "C" void np_project_fail(void *ui, unsigned code)
{
    volatile uint8_t *pb = pboot();
    if (pb[0] == 1u) {
        if (pb[1] == 0u)
            pb[1] = uint8_t(code);
        pb[2] = uint8_t(pb[2] + 1u);
        return;
    }
    reinterpret_cast<void (*)(void *, unsigned)>(0x0814FA55u)(ui, code);
}

extern "C" void np_boot_load(uint8_t *mgr, unsigned slot)
{
    volatile uint8_t *pb = pboot();
    pb[0] = 1u;
    pb[1] = pb[2] = pb[3] = 0u;
    reinterpret_cast<void (*)(uint8_t *, unsigned)>(0x08124441u)(mgr, slot);
    if (pb[1] != 0u) {
        auto *image = reinterpret_cast<volatile uint8_t *>(0x24000000u);
        for (unsigned i = 0; i < 0x100u; ++i)
            image[i] = 0u;
        image[0x100] = 254u;                    /* SOF */
        image[0x101] = 9u;                      /* EOF */
        reinterpret_cast<ReplayFn>(0x08124055u)(mgr);
        /* one transaction tag, as 0x08157578 assigns them */
        *reinterpret_cast<volatile uint32_t *>(mgr + 0x58) = 0u;
        volatile uint32_t &count = *reinterpret_cast<volatile uint32_t *>(mgr + 0x4C);
        count = count + 1u;
        const uint32_t tag = count & 0x3FFFu;
        /* every one of the 64 patterns gets 16 steps.  The event names a
         * pattern (byte 6) and takes effect only on one a layer is playing,
         * so layer 0 visits each (0x08125A90(mgr, pattern, layer), the
         * stock's switch, immediate with the transport stopped) and ends on
         * pattern 0; layer 1's pattern gets its event again at the end, to
         * refresh that layer's copy */
        using SwitchFn = void (*)(uint8_t *, unsigned, unsigned);
        for (unsigned step = 0; step <= 64u; ++step) {
            const unsigned pattern = step < 64u ? 63u - step : mgr[0x184];
            if (step < 64u)
                reinterpret_cast<SwitchFn>(0x08125A91u)(mgr, pattern, 0u);
            auto *e = static_cast<uint8_t *>(reinterpret_cast<void *(*)(unsigned)>(0x081591D5u)(12u));
            if (!e)
                break;
            for (unsigned i = 0; i < 12u; ++i)
                e[i] = 0u;
            *reinterpret_cast<uint32_t *>(e) = kPatternParamVt;
            e[4] = uint8_t(tag);
            e[5] = uint8_t(tag >> 8);
            e[6] = uint8_t(pattern);
            e[8] = 16u;                         /* parameter 0 (byte 7), the length */
            void *p = e;
            reinterpret_cast<void (*)(void *, void **)>(0x081576D5u)(mgr, &p);
            if (p) {
                const uint32_t vt = *static_cast<const uint32_t *>(p);
                reinterpret_cast<void (*)(void *)>(reinterpret_cast<const uint32_t *>(vt)[1])(p);
            }
        }
        pb[3] = 1u;
    }
    pb[0] = 0u;
}

/* ---------------------------------------------------------------------
 * N2: a mod note's parameters applied without an event per parameter.
 *
 * 0x081209F4, reached on every mod-note note-on (and when a release falls
 * back to a held mod note), resets the layer's bank table and then, for
 * each of the note's stored parameters, allocates a 12-byte
 * ControllerEvent<CPatternParamData>, dispatches it through the
 * controller's type registry (std::type_info name hashing) and frees it.
 * The handler that receives it (0x08117DFC, type 7 at 0x08117E94) does
 * two things: the bank slot gets the value, and 0x08116D9C re-evaluates
 * that one parameter -- unless the layer's patch-restore flag
 * (project+0x6034+layer) is set, in which case the event is dropped.
 * Measured on sysemu per parameter: ~90 instructions in operator new,
 * ~510 in the dispatch, ~70 in delete around ~270 of actual work -- a
 * full mod note (108 parameters) spent ~100k instructions, ~250 us,
 * between the step and its sound.  This does the handler's two things
 * directly, in the same order, for the same records.
 * --------------------------------------------------------------------- */
extern "C" void np_modnote_replay(uint8_t *mgr, uint32_t note, uint32_t layer)
{
    using ProjectFn = uint8_t *(*)();
    using ResetFn = void (*)(uint8_t *, uint32_t);
    using EvalFn = void (*)(uint8_t *, uint32_t, uint32_t);
    note &= 0xFFu;
    layer &= 0xFFu;
    mgr[0x1D0 + layer] = static_cast<uint8_t>(note);
    uint8_t *project = reinterpret_cast<ProjectFn>(0x0811621Du)();
    reinterpret_cast<ResetFn>(0x0811702Du)(project, layer);
    for (const uint8_t *rec = *reinterpret_cast<uint8_t *const *>(mgr + 0x1BC);
         rec != nullptr; rec = *reinterpret_cast<const uint8_t *const *>(rec)) {
        if (rec[4] != note || rec[5] != 7u)
            continue;
        if (project[0x6034 + layer] != 0u)        /* the handler's own gate */
            continue;
        uint32_t slot = rec[6], local = rec[7];
        if (slot >= 19u || local >= 9u)           /* a damaged project's record */
            continue;
        uint16_t raw = static_cast<uint16_t>(rec[8] | rec[9] << 8);
        *reinterpret_cast<float *>(project + 0x4268 + layer * 0x180Cu + (slot * 9u + local) * 4u) =
            static_cast<float>(raw);
        reinterpret_cast<EvalFn>(0x08116D9Du)(project, slot, local);
    }
}

/* ---------------------------------------------------------------------
 * CH: an instrument switch fades the sounding one out first -- the main
 * loop's half (the why, and the DSP's half, are at np_cnote_entry in
 * np_patch.S and np_env_main in np_xenv.S).
 * --------------------------------------------------------------------- */
extern "C" const uint32_t np_choke_samples;
extern "C" void np_choke_reset_envs();

namespace {
constexpr uintptr_t kNoteOnHandler = 0x081170E9u;  /* CNoteData note-on       */
constexpr uint32_t kCNoteDataVptr = 0x0815EA28u;    /* ControllerEvent<CNoteData> */
constexpr uintptr_t kAmpEnvLevel = 0x2000E400u + 0x180u;  /* RC entry 0, E_Y */

inline uint32_t mask_irq()
{
    uint32_t primask;
    __asm volatile("mrs %0, primask\n\tcpsid i" : "=r"(primask) :: "memory");
    return primask;
}

inline void unmask_irq(uint32_t primask)
{
    __asm volatile("msr primask, %0" :: "r"(primask) : "memory");
}
} /* namespace */

/* The held note-on goes now: the fade is stopped wherever it is, every
 * envelope is put at rest at 0 (the DSP may already have done it), and the
 * stock handler applies the note -- its mod note, its parameters, its
 * attack, from silence. */
extern "C" void np_choke_flush()
{
    void *ev = reinterpret_cast<void *>(diag(D_CHOKE_PEND));
    if (ev == nullptr)
        return;
    diag(D_CHOKE_PEND) = 0u;
    uint32_t primask = mask_irq();
    diag(D_CHOKE_N) = 0u;
    np_choke_reset_envs();
    unmask_irq(primask);
    void *slot = ev;
    using NoteOnFn = void (*)(void *, void **);
    reinterpret_cast<NoteOnFn>(kNoteOnHandler)(reinterpret_cast<void *>(diag(D_CHOKE_PROJ)), &slot);
    if (slot != nullptr) {                 /* not taken: delete it as the caller would */
        using DeleteFn = void (*)(void *);
        reinterpret_cast<DeleteFn>(reinterpret_cast<const uint32_t *>(*static_cast<uint32_t *>(slot))[1])(slot);
    }
}

/* At 0x08117AD0, in place of the note-on handler: 1 = held back (the event
 * is ours now), 0 = run the stock handler as usual. */
extern "C" uint32_t np_choke_note_on(uint8_t *project, uint8_t **uptr)
{
    uint8_t *ev = *uptr;
    if (ev == nullptr || *reinterpret_cast<const uint32_t *>(ev) != kCNoteDataVptr)
        return 0u;
    np_choke_flush();                      /* one already waiting goes first */
    if (np_choke_samples == 0u)
        return 0u;
    unsigned layer = ev[5] & 1u;
    uint32_t id = (ev[0x11] != 0u ? ev[0x10] : 0xFFu) | 0x100u;
    volatile uint32_t &last = diag(layer ? D_CHOKE_LAST1 : D_CHOKE_LAST0);
    uint32_t prev = last;
    last = id;
    if ((prev & 0x100u) == 0u || prev == id)
        return 0u;                         /* first note, or the same instrument */
    uint8_t *held = project + 0x6028;      /* the held-note list: not empty = legato */
    if (*reinterpret_cast<uint8_t **>(held) != held)
        return 0u;
    float level = *reinterpret_cast<volatile const float *>(kAmpEnvLevel);
    if (!(level >= 0.01f))
        return 0u;                         /* already (nearly) silent: nothing to fade */
    *uptr = nullptr;
    diag(D_CHOKE_PROJ) = reinterpret_cast<uintptr_t>(project);
    diag(D_CHOKE_PEND) = reinterpret_cast<uintptr_t>(ev);
    diag(D_CHOKE_STARTED) = 0u;
    dmb();
    diag(D_CHOKE_N) = np_choke_samples;
    diag(D_CHOKES) += 1u;
    return 1u;
}
