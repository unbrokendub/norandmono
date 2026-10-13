/*
 * np_midi.cpp -- combined USART1 RX/TXE interrupt handler.
 *
 * The stock transmitter is called by the 1 kHz arm of SysTick and writes
 * one byte unconditionally.  At 31250 baud a byte takes 320 us, so that
 * original schedule is slow (one byte/ms) but does not overwrite TDR as an
 * earlier audit claimed.  A polling replacement is not a real improvement:
 * TXE normally clears after its first write and the rest of its loop exits.
 *
 * The replacement below makes the existing USART1 vector service both RX
 * and TXE.  The producer/main loop only enables TXEIE; the peripheral then
 * drains exactly at wire rate and disables the interrupt when the ring is
 * empty.  No heap and no wait loop are reachable from this handler.
 */

#include <stdint.h>
#include "np_diag.h"

namespace {

constexpr uintptr_t kUsart1 = 0x40011000u;
constexpr unsigned kCr1 = 0x00u;
constexpr unsigned kIsr = 0x1Cu;
constexpr unsigned kIcr = 0x20u;
constexpr unsigned kRdr = 0x24u;
constexpr unsigned kTdr = 0x28u;

constexpr uint32_t kFe = 1u << 1;
constexpr uint32_t kNe = 1u << 2;
constexpr uint32_t kOre = 1u << 3;
constexpr uint32_t kRxne = 1u << 5;
constexpr uint32_t kTxe = 1u << 7;
constexpr uint32_t kTxeie = 1u << 7;

constexpr unsigned kRing = 0x0A18u;
constexpr unsigned kHead = 0x0C18u;
constexpr unsigned kTail = 0x0C1Cu;
constexpr uint32_t kMask = 0x1FFu;

constexpr uintptr_t kDiag = 0x2000E400u;
constexpr unsigned D_MIDIERR = 0x3Cu;
constexpr unsigned D_MIDI_Q_HIGH = 0x58u;
constexpr unsigned D_MIDI_Q_FULL = 0x5Cu;
constexpr unsigned D_MIDI_TX_BYTES = 0x60u;

using MidiObjFn = void *(*)();
constexpr uintptr_t kMidiObject = 0x0811A6A5u;

/* DIN receive byte ring: 512 bytes at +0x810, written here (head +0xA14),
 * read by the main-loop parser 0x0811A7A0 (tail +0xA10). */
constexpr unsigned kRxRing = 0x0810u;
constexpr unsigned kRxHead = 0x0A14u;
constexpr unsigned kRxTail = 0x0A10u;

/* USB-MIDI receive packet ring: 512 words, written from the USB OTG
 * interrupt (the class data-out callback), read by 0x081102B4 in main. */
constexpr uintptr_t kUsbRxRing = 0x20003260u;
constexpr uintptr_t kUsbRxTail = 0x20003A60u;
constexpr uintptr_t kUsbRxHead = 0x20003A64u;

inline volatile uint32_t &reg(unsigned off)
{
    return *reinterpret_cast<volatile uint32_t *>(kUsart1 + off);
}

inline volatile uint32_t &diag(unsigned off)
{
    return *reinterpret_cast<volatile uint32_t *>(kDiag + off);
}

inline uint32_t &u32(void *p, unsigned off)
{
    return *reinterpret_cast<uint32_t *>(reinterpret_cast<char *>(p) + off);
}

inline void barrier()
{
    __asm volatile("dmb" ::: "memory");
}

void note_depth(void *port)
{
    uint32_t used = (u32(port, kTail) - u32(port, kHead)) & kMask;
    if (used > diag(D_MIDI_Q_HIGH))
        diag(D_MIDI_Q_HIGH) = used;
    /* The stock producer resolves a full ring by moving head.  Observing
     * 511 therefore counts full-ring episodes, not every overwritten byte. */
    if (used == kMask)
        diag(D_MIDI_Q_FULL) += 1u;
}

void tx_one(void *port)
{
    uint32_t head = u32(port, kHead);
    uint32_t tail = u32(port, kTail);
    if (((tail - head) & kMask) == 0u) {
        reg(kCr1) &= ~kTxeie;
        return;
    }
    reg(kTdr) = reinterpret_cast<const uint8_t *>(port)[kRing + head];
    u32(port, kHead) = (head + 1u) & kMask;
    diag(D_MIDI_TX_BYTES) += 1u;
}

/* The stock receive path, 0x0811A738, resolves a full ring by advancing
 * the TAIL -- the parser's index -- from this interrupt.  The parser in
 * main keeps the tail in a register across a message and writes it back
 * per byte, so an overflow landing inside that window is undone: tail is
 * set back behind head by one, which reads as an empty ring, and all 511
 * unread bytes vanish at once -- note-offs among them.  It needs the
 * parser to fall 163 ms behind a saturated wire (a long main stall, such
 * as a cross-project paste writing flash), but then it loses everything.
 * Here the producer owns only the head: a byte that does not fit is the
 * one dropped, and counted. */
void din_rx_put(void *port, uint32_t byte)
{
    uint32_t head = u32(port, kRxHead) & kMask;
    uint32_t next = (head + 1u) & kMask;
    if (next == (u32(port, kRxTail) & kMask)) {
        diag(D_MIDI_RX_DROP) += 1u;
        return;
    }
    reinterpret_cast<uint8_t *>(port)[kRxRing + head] = static_cast<uint8_t>(byte);
    barrier();
    u32(port, kRxHead) = next;
}

/* Output event ring: 256 four-byte events {cable/CIN, status, d1, d2} at
 * +0x408, produced in main (and, in CV-clock mode, by TIM5's stop), tail
 * +0x808 owned by the fan-out below. */
constexpr unsigned kOutRing = 0x0408u;
constexpr unsigned kOutTail = 0x0808u;
constexpr unsigned kOutHead = 0x080Cu;
constexpr uintptr_t kUsbTxLen = 0x20000704u;     /* USB staging, bytes (256 max) */
constexpr uintptr_t kTick = 0x2000DE48u;         /* the stock fan-out's ms clock */
constexpr uintptr_t kGpioWrite = 0x081029CDu;    /* HAL_GPIO_WritePin           */
constexpr uintptr_t kSysInstance = 0x08109615u;
constexpr uintptr_t kSysDriver = 0x0810977Du;
constexpr uintptr_t kUsbConnected = 0x08110241u; /* device state CONFIGURED     */
constexpr uintptr_t kUsbPut = 0x081101DDu;       /* event -> USB-MIDI packet    */

inline unsigned din_length(uint32_t status)
{
    switch (status & 0xF0u) {
    case 0xF0u: return 1u;                       /* realtime: the status only    */
    case 0xC0u: case 0xD0u: return 2u;
    default: return 3u;
    }
}

} /* namespace */

/* ---------------------------------------------------------------------
 * MF1/MF2: the MIDI output fan-out, each port waiting for itself.
 *
 * Replaces the main loop's `bl 0x0811A904` at 0x08119A2C.  The stock
 * function advanced the tail and handed every event to USB and to the DIN
 * ring with no regard for room in either: USB staging holds 64 packets
 * between the 1 kHz flushes and the append's "full" return was ignored, so
 * the 65th packet of a burst and all after it were lost silently; the DIN
 * copy went in byte by byte and resolved "full" by dropping the ring's
 * oldest byte -- the middle of a message already queued, which the
 * receiver then reads under the wrong (running) status.
 *
 * Here, with USB up, an event is taken only when USB staging has room for
 * it (else it waits in the output ring for the next pass), and its DIN copy
 * is made whole or not at all -- DIN at 3125 bytes/s is not allowed to
 * throttle USB, nor a stalled USART to stop it.  With USB down, DIN is the
 * only port and the event waits for DIN room instead.  An output ring that
 * overflows meanwhile drops whole events, oldest first, by its producers'
 * own rule.  Everything else is as the stock: the MIDI-activity LED
 * timeouts first, the +0xD18 gate (SysEx dump in progress), the same
 * packet built by 0x081101DC, the same DIN bytes per status (1 for F0..FF,
 * 2 for C0/D0, 3 otherwise).  The USART's TXE kick stays with SysTick.
 * --------------------------------------------------------------------- */
extern "C" void np_midi_fanout(uint8_t *m)
{
    using GpioFn = void (*)(uintptr_t, uint32_t, uint32_t);
    using InstanceFn = void *(*)();
    using DriverFn = void *(*)(void *, unsigned);
    using UsbFn = uint32_t (*)(void *);
    using PutFn = void (*)(void *, uint32_t);

    uint32_t now = *reinterpret_cast<volatile uint32_t *>(kTick);
    if (m[0xCA8] != 0u && now - u32(m, 0xCA4) > 5u) {
        m[0xCA8] = 0u;
        reinterpret_cast<GpioFn>(kGpioWrite)(0x58020000u, 0x40u, 0u);
    }
    if (m[0xCB0] != 0u && now - u32(m, 0xCAC) > 5u) {
        m[0xCB0] = 0u;
        reinterpret_cast<GpioFn>(kGpioWrite)(0x58020000u, 0x80u, 0u);
    }
    if (m[0xD18] != 0u)
        return;

    void *usb = reinterpret_cast<DriverFn>(kSysDriver)(reinterpret_cast<InstanceFn>(kSysInstance)(), 7u);
    bool up = reinterpret_cast<UsbFn>(kUsbConnected)(usb) != 0u;
    volatile uint32_t &usb_len = *reinterpret_cast<volatile uint32_t *>(kUsbTxLen);
    volatile uint32_t &head = *reinterpret_cast<volatile uint32_t *>(m + kOutHead);
    volatile uint32_t &tail_ref = *reinterpret_cast<volatile uint32_t *>(m + kOutTail);
    volatile uint32_t &din_rd = *reinterpret_cast<volatile uint32_t *>(m + kHead);
    volatile uint32_t &din_wr = *reinterpret_cast<volatile uint32_t *>(m + kTail);

    uint32_t tail = tail_ref & 0xFFu;
    while (((head - tail) & 0xFFu) != 0u) {
        const uint8_t *e = m + kOutRing + tail * 4u;
        uint32_t ev = e[0] | e[1] << 8 | e[2] << 16 | static_cast<uint32_t>(e[3]) << 24;
        unsigned n = din_length(e[1]);
        uint32_t wr = din_wr & kMask;
        bool din_room = ((din_rd - wr - 1u) & kMask) >= n;
        if (up) {
            if (usb_len > 256u - 4u) {
                diag(D_MIDI_USB_HELD) += 1u;
                break;
            }
        } else if (!din_room) {
            diag(D_MIDI_DIN_HELD) += 1u;
            break;
        }
        tail = (tail + 1u) & 0xFFu;
        tail_ref = tail;
        if (up)
            reinterpret_cast<PutFn>(kUsbPut)(usb, ev);
        if (din_room) {
            for (unsigned i = 0; i < n; ++i) {
                m[kRing + wr] = e[1 + i];
                wr = (wr + 1u) & kMask;
            }
            barrier();
            din_wr = wr;
        } else {
            diag(D_MIDI_DIN_DROP) += 1u;
        }
    }
}

/* Replaces the stock USB-MIDI receive callback's ring push (0x08110330,
 * called at 0x08108D4A from the OTG interrupt with the packet buffer and
 * its length in bytes).  Same ring, same 4-byte packets, but the same
 * ownership rule as din_rx_put: the stock version advances the reader's
 * tail on overflow, racing 0x081102B4 in main exactly as above. */
extern "C" void np_usb_rx_put(const uint8_t *buf, uint32_t len)
{
    volatile uint32_t &head_ref = *reinterpret_cast<volatile uint32_t *>(kUsbRxHead);
    uint32_t head = head_ref & kMask;
    uint32_t tail = *reinterpret_cast<volatile uint32_t *>(kUsbRxTail) & kMask;
    uint32_t *ring = reinterpret_cast<uint32_t *>(kUsbRxRing);
    for (uint32_t i = 0; i < len; i += 4u) {
        uint32_t next = (head + 1u) & kMask;
        if (next == tail) {
            diag(D_USB_RX_DROP) += 1u;
            continue;
        }
        ring[head] = static_cast<uint32_t>(buf[i]) |
                     static_cast<uint32_t>(buf[i + 1u]) << 8 |
                     static_cast<uint32_t>(buf[i + 2u]) << 16 |
                     static_cast<uint32_t>(buf[i + 3u]) << 24;
        head = next;
    }
    barrier();
    head_ref = head;
}

/* Called from the old SysTick site and from the main-loop service.  It is a
 * kick only; bytes are written solely by np_midi_rx (the USART IRQ entry). */
extern "C" void np_midi_tx(void *port)
{
    if (port == nullptr)
        port = reinterpret_cast<MidiObjFn>(kMidiObject)();
    if (((u32(port, kTail) - u32(port, kHead)) & kMask) == 0u)
        return;
    barrier();
    reg(kCr1) |= kTxeie;
}

/* Kept under the old exported name because the NPAT header and old tooling
 * use it.  It is now the complete USART1 interrupt handler. */
extern "C" void np_midi_rx()
{
    uint32_t status = reg(kIsr);

    if ((status & (kRxne | kFe | kNe | kOre)) != 0u) {
        uint32_t byte = 0u;
        bool have_byte = (status & kRxne) != 0u;
        if (have_byte)
            byte = reg(kRdr) & 0xFFu;
        reg(kIcr) = status & (kFe | kNe | kOre);

        volatile uint32_t &errors = diag(D_MIDIERR);
        if ((status & (kFe | kNe)) != 0u) {
            errors = (errors & 0xFFFF0000u) | ((errors + 1u) & 0xFFFFu);
            have_byte = false;
        }
        if ((status & kOre) != 0u)
            errors = (errors & 0xFFFFu)
                   | ((errors + 0x10000u) & 0xFFFF0000u);

        if (have_byte)
            din_rx_put(reinterpret_cast<MidiObjFn>(kMidiObject)(), byte);
    }

    /* A single write per IRQ is intentional, and it is load-bearing twice
     * over.  The obvious reason is cadence: hardware deasserts TXE until
     * TDR moves to the shifter and raises the next IRQ at exactly wire
     * rate.
     *
     * The second reason is a race.  When the ring fills, the stock producer
     * in the main loop (`sub_0811A904`, around 0x0811A9F8) resolves it by
     * moving *head* -- the index this handler owns -- with a plain
     * read-modify-write and no critical section:
     *
     *     h = head;  ...  if (h == (tail+1) & 0x1FF) head = (h+1) & 0x1FF;
     *
     * If this IRQ lands inside that window it advances head by one, and the
     * producer then writes back the same value.  Harmless -- but only
     * because the step is one.  Drain N bytes per IRQ and the producer
     * rewinds head by up to N-1, re-transmitting bytes already on the wire
     * and corrupting message framing.  The window is a handful of
     * instructions against a 320 us IRQ period, so a second entry inside it
     * is not possible; the size of the step is the whole guarantee.
     *
     * Do not turn this into a loop without first making that producer
     * branch atomic. */
    if ((reg(kCr1) & kTxeie) != 0u && (reg(kIsr) & kTxe) != 0u) {
        void *port = reinterpret_cast<MidiObjFn>(kMidiObject)();
        note_depth(port);
        tx_one(port);
    }
}
