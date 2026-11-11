#!/usr/bin/env python3
"""
patch.py -- apply the audit fixes to the Norand Mono application image.

Assembles patch/np_patch.S, appends it past the end of the stock image and
rewrites a handful of instructions so the new code is reached:

  A1  four vector table entries          -> real fault handlers
  A4  _sbrk's bkpt                       -> return -1
  C   Reset_Handler's "bl SystemInit"    -> enable I-cache, then SystemInit
  A2  Reset_Handler's "bl main"          -> prepare diagnostics, then main
      main loop's "bl System::instance"  -> kick the watchdog, then instance
  E1  optional CRC32 of the image, checked at boot

Every site is verified against its expected original bytes first, so the
tool refuses to touch an image it does not recognise.

    patch.py build                 -> build/firmware-patched.bin + .syx
    patch.py build --no-watchdog
    patch.py build --crc
    patch.py build --wdg-timeout 30
    patch.py diag <hexdump>        decode a diagnostic block
"""

import argparse
import os
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import norand_syx  # noqa: E402

BASE = 0x08100000
BLOB_BASE = 0x08175200            # must match the -Ttext below
BLOB_WINDOW = 0x80000 - (BLOB_BASE - BASE)  # "is this target in the blob": anywhere under the 512 KB cap
STOCK_SIZE = 0x751D8
LSI_HZ = 32000
PRESCALERS = [4, 8, 16, 32, 64, 128, 256, 256]

SRC_ASM = "patch/np_patch.S"
SRC_CPP = "patch/np_lfo.cpp"
SRC_MIDI = "patch/np_midi.cpp"
SRC_RUNTIME = "patch/np_runtime.cpp"
SRC_UI = "patch/np_ui.cpp"
SRC_RANDOM = "patch/np_random.cpp"
SRC_MNRAND = "patch/np_mnrand.cpp"
LDS = "patch/np.ld"
ELF = "build/np_patch.elf"
BIN = "build/np_patch.bin"

# must match the firmware: the stock code uses d-registers (vpush {d8}),
# so this is an FPv5-D16 hard-float build
ARCH = ["-mcpu=cortex-m7", "-mthumb", "-mfpu=fpv5-d16", "-mfloat-abi=hard"]
CXXFLAGS = ARCH + [
    "-std=c++17", "-ffreestanding", "-fno-exceptions", "-fno-rtti",
    "-fno-threadsafe-statics", "-fno-unwind-tables",
    "-fno-asynchronous-unwind-tables", "-fomit-frame-pointer",
    "-ffunction-sections", "-fdata-sections",
    "-Wall", "-Wextra", "-Werror",
]

FLAG_CRC = 0x01
FLAG_WDG = 0x02
FLAG_LFO = 0x04
FLAG_TL = 0x08          # built with --telemetry (no code reads it; the tests do)


# ---------------------------------------------------------------------------
# Thumb-2 BL encoding
# ---------------------------------------------------------------------------

def thumb_bl(pc, target):
    off = target - (pc + 4)
    if off & 1 or not -(1 << 24) <= off < (1 << 24):
        raise ValueError("BL out of range: 0x%08X -> 0x%08X" % (pc, target))
    off >>= 1
    s = (off >> 23) & 1
    i1 = (off >> 22) & 1
    i2 = (off >> 21) & 1
    hw1 = 0xF000 | (s << 10) | ((off >> 11) & 0x3FF)
    hw2 = 0xD000 | (((i1 ^ 1 ^ s) & 1) << 13) | (((i2 ^ 1 ^ s) & 1) << 11) | (off & 0x7FF)
    return struct.pack("<HH", hw1, hw2)


def thumb_b_w(pc, target):
    """
    Unconditional wide branch, B.W (T4).  Same first halfword as BL, and the
    second differs only in bit 14: BL is 11 J1 1 J2, B.W is 10 J1 1 J2.
    (Clearing bit 12 instead produces BLX, which switches to ARM state and
    faults immediately -- worth stating, having made exactly that mistake.)
    """
    hw1, hw2 = struct.unpack("<HH", thumb_bl(pc, target))
    return struct.pack("<HH", hw1, hw2 & ~0x4000)


def thumb_b_cond_w(pc, target, cond):
    """Conditional wide branch, B<c>.W (T3), +-1 MB."""
    off = target - (pc + 4)
    if off & 1 or not -(1 << 20) <= off < (1 << 20):
        raise ValueError("B<c>.W out of range: 0x%08X -> 0x%08X" % (pc, target))
    s = (off >> 20) & 1
    j2 = (off >> 19) & 1
    j1 = (off >> 18) & 1
    hw1 = 0xF000 | (s << 10) | (cond << 6) | ((off >> 12) & 0x3F)
    hw2 = 0x8000 | (j1 << 13) | (j2 << 11) | ((off >> 1) & 0x7FF)
    return struct.pack("<HH", hw1, hw2)


def thumb_b_n(pc, target):
    """Unconditional narrow branch, B (T2), +-2 KB."""
    off = target - (pc + 4)
    if off & 1 or not -2048 <= off < 2048:
        raise ValueError("B out of range: 0x%08X -> 0x%08X" % (pc, target))
    return struct.pack("<H", 0xE000 | ((off >> 1) & 0x7FF))


def thumb_cbz(pc, target, rn):
    """CBZ rn, target (forward 0..126)."""
    off = target - (pc + 4)
    if off & 1 or not 0 <= off <= 126 or not 0 <= rn < 8:
        raise ValueError("CBZ out of range: 0x%08X -> 0x%08X" % (pc, target))
    return struct.pack("<H", 0xB100 | ((off >> 6) & 1) << 9 | ((off >> 1) & 0x1F) << 3 | rn)


def crc32(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


# ---------------------------------------------------------------------------

def assemble(follow_osc1=False, dsp_opt="O3", midi=True, ev_log=True, telemetry=False):
    """Build the blob: np_patch.S plus np_lfo.cpp, linked at BLOB_BASE.

    Both sources read NP_XMOD_FOLLOW and they have to agree: the parameter
    handler and the DSP splice compute the same position for X-Mod type 3,
    one when a knob moves and one every frame.
    """
    os.makedirs("build", exist_ok=True)
    define = ["-DNP_XMOD_FOLLOW=%d" % (1 if follow_osc1 else 0),
              "-DNP_EV_LOG=%d" % (1 if ev_log else 0),
              "-DNP_DSP_OPT=%d" % (3 if dsp_opt == "O3" else 1),
              "-DNP_MIDI_ENABLE=%d" % (1 if midi else 0),
              "-DNP_TELEMETRY=%d" % (1 if telemetry else 0)]
    objs = []

    obj = "build/np_patch.o"
    subprocess.run(["arm-none-eabi-gcc", "-c"] + ARCH + define +
                   ["-x", "assembler-with-cpp", SRC_ASM, "-o", obj], check=True)
    objs.append(obj)

    obj = "build/np_lfo.o"
    env_obj = "build/np_xenv.o"
    subprocess.run(["arm-none-eabi-gcc", "-c"] + ARCH +
                   ["-DNP_EV_LOG=%d" % (1 if ev_log else 0),
                    "patch/np_xenv.S", "-o", env_obj], check=True)
    objs.append(env_obj)

    # np_lfo.cpp runs inside the 32 kHz frame with the D-cache off, where a
    # pc-relative literal is an uncached flash read (~10 cycles); have GCC
    # materialise constants with movw/movt instead (docs/envelope-cost.md).
    subprocess.run(["arm-none-eabi-g++", "-c", "-" + dsp_opt, "-mslow-flash-data"] +
                   CXXFLAGS + define + [SRC_CPP, "-o", obj], check=True)
    objs.append(obj)

    obj = "build/np_midi.o"
    subprocess.run(["arm-none-eabi-g++", "-c", "-Os"] + CXXFLAGS + define +
                   [SRC_MIDI, "-o", obj], check=True)
    objs.append(obj)

    obj = "build/np_runtime.o"
    subprocess.run(["arm-none-eabi-g++", "-c", "-Os"] + CXXFLAGS + define +
                   [SRC_RUNTIME, "-o", obj], check=True)
    objs.append(obj)

    obj = "build/np_ui.o"
    subprocess.run(["arm-none-eabi-g++", "-c", "-Os"] + CXXFLAGS + define +
                   [SRC_UI, "-o", obj], check=True)
    objs.append(obj)

    # no libraries: an undefined libgcc helper must fail the link rather than
    # silently pull in code that is not in the image
    obj = "build/np_random.o"
    subprocess.run(["arm-none-eabi-g++", "-c", "-Os"] + CXXFLAGS + define +
                   [SRC_RANDOM, "-o", obj], check=True)
    objs.append(obj)
    # main-loop code, never timed: no inlining buys ~1 KB of the 512 KB cap
    obj = "build/np_mnrand.o"
    subprocess.run(["arm-none-eabi-g++", "-c", "-Os", "-fno-inline"] + CXXFLAGS + define +
                   [SRC_MNRAND, "-o", obj], check=True)
    objs.append(obj)
    subprocess.run(["arm-none-eabi-ld", "-T", LDS, "--no-warn-rwx-segments",
                    "--gc-sections", "-e", "np_base"] + objs + ["-o", ELF],
                   check=True)
    subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", ELF, BIN], check=True)

    blob = open(BIN, "rb").read()
    if blob[:4] != b"NPAT":
        raise SystemExit("linker did not place np_base first in the blob")
    return blob


class Header:
    """the NPAT header at the start of the blob"""

    def __init__(self, blob):
        if blob[:4] != b"NPAT":
            raise ValueError("patch blob has no NPAT signature")
        self.version = blob[4]
        (self.early, self.late, self.wdg_hook, self.hardfault, self.memmanage,
         self.busfault, self.usagefault, self.sbrk, self.diag,
         self.diag_size) = struct.unpack_from("<10I", blob, 0x10)
        (self.mod_tick, self.lfo_poll, self.xmod_time, self.xmod_speed,
         self.xmod_pos, self.ratio_value, self.ratio_offset,
         self.ratio_count) = struct.unpack_from("<8I", blob, 0x44)
        self.xmod_follow = blob[0x64]
        (self.sine, self.tri, self.square,
         self.saw) = struct.unpack_from("<4I", blob, 0x68)
        (self.midi_tx, self.midi_rx) = struct.unpack_from("<2I", blob, 0x78)
        (self.usb_defer, self.audio_irq, self.systick_irq, self.tim5_irq,
         self.dma_irq, self.new, self.delete, self.malloc_lock,
         self.malloc_unlock, self.main_service, self.note_hook,
         self.note_addr) = struct.unpack_from("<12I", blob, 0x80)
        # the six waveform dispatches in the per-slot evaluators
        self.wave_thunks = struct.unpack_from("<6I", blob, 0xB0)
        self.ui_switch, self.ui_begin_unlink = struct.unpack_from("<2I", blob, 0xCC)
        self.ui_helpers = struct.unpack_from("<6I", blob, 0xD4)
        self.ui_consume_modifier = struct.unpack_from("<I", blob, 0xF0)[0]
        self.random_splice = struct.unpack_from("<I", blob, 0xF4)[0]
        self.xenv_thunks = struct.unpack_from("<7I", blob, 0xF8)
        self.env_main = struct.unpack_from("<I", blob, 0x118)[0]
        self.led_frame = struct.unpack_from("<I", blob, 0x12C)[0]
        self.led_done = struct.unpack_from("<I", blob, 0x130)[0]
        self.ui_queue_pop = struct.unpack_from("<I", blob, 0x134)[0]
        (self.led_configure, self.diag_render, self.diag_enqueue,
         self.diag_dispatch) = struct.unpack_from("<4I", blob, 0x138)
        self.scan_rearm = struct.unpack_from("<I", blob, 0x14C)[0]
        self.ui_service, self.project_first = struct.unpack_from("<2I", blob, 0x150)
        (self.eval_linad, self.eval_expad, self.set_rate,
         self.slot_walk, self.after_walk) = struct.unpack_from("<5I", blob, 0x158)
        self.sqrtlut = struct.unpack_from("<3I", blob, 0x16C)
        self.flash_write = struct.unpack_from("<I", blob, 0x178)[0]
        self.led_smooth = struct.unpack_from("<I", blob, 0x17C)[0]
        self.list_sort = struct.unpack_from("<I", blob, 0x180)[0]
        self.seq_worker = struct.unpack_from("<I", blob, 0x184)[0]
        self.ev_trig = struct.unpack_from("<I", blob, 0x188)[0]
        self.morph_trig = struct.unpack_from("<I", blob, 0x18C)[0]
        (self.project_gate, self.midi_fanout, self.polyat,
         self.usb_rx_put, self.note_start, self.dsp_sample,
         self.modnote_replay, self.cnote_entry, self.noteoff_entry,
         self.choke_samples, self.modnote_random, self.mn_key_press,
         self.mn_rec_press, self.mn_rec_release,
         self.mn_chord_check, self.nm_key_press,
         self.nm_key_release, self.clear_match, self.load_request,
         self.load_done, self.proj_service,
         self.save_gate, self.boot_load,
         self.project_fail, self.rotate_guard, self.led_level,
         self.led_target) = struct.unpack_from("<27I", blob, 0x190)

    OFF_FLAGS = 0x05
    OFF_WDG_PR = 0x06
    OFF_CRC = 0x08
    OFF_CRC_LEN = 0x0C
    OFF_WDG_RLR = 0x38
    OFF_BRK_PTR = 0x3C
    OFF_HEAPSIZE_PTR = 0x40


def wdg_params(seconds):
    """smallest prescaler whose reload counter still fits 12 bits"""
    for code, div in enumerate(PRESCALERS):
        count = round(seconds * LSI_HZ / div)
        if 1 <= count <= 4095:
            return code, count, count * div / LSI_HZ
    raise ValueError("watchdog timeout out of range: %s s" % seconds)


def build(args):
    image = bytearray(open("build/firmware.bin", "rb").read())
    if len(image) != STOCK_SIZE:
        raise SystemExit("unexpected image size 0x%X (want 0x%X); "
                         "run `make extract` first" % (len(image), STOCK_SIZE))

    blob = bytearray(assemble(follow_osc1=args.xmod_follow_osc1,
                              dsp_opt=args.dsp_opt, midi=args.midi,
                              ev_log=args.ev_log, telemetry=args.telemetry))
    hdr = Header(blob)

    # ---- assemble the final image ------------------------------------
    image += b"\x00" * (BLOB_BASE - BASE - len(image))
    blob_off = len(image)
    image += blob

    log = []

    def poke(addr, want, new, what):
        if len(new) != len(want):
            raise SystemExit("0x%08X: replacement is %d bytes for a %d-byte site"
                             % (addr, len(new), len(want)))
        off = addr - BASE
        have = bytes(image[off:off + len(want)])
        if have != want:
            raise SystemExit("0x%08X: expected %s, found %s -- wrong image?"
                             % (addr, want.hex(" "), have.hex(" ")))
        image[off:off + len(new)] = new
        log.append((addr, what, have.hex(" "), bytes(new).hex(" ")))

    # ---- A1: vector table --------------------------------------------
    for slot, (name, target) in enumerate([
            ("HardFault", hdr.hardfault), ("MemManage", hdr.memmanage),
            ("BusFault", hdr.busfault), ("UsageFault", hdr.usagefault)], start=3):
        addr = BASE + 4 * slot
        old = bytes(image[4 * slot:4 * slot + 4])
        poke(addr, old, struct.pack("<I", target),
             "vector %s -> 0x%08X" % (name, target))

    # Profile the complete wall-clock duration of the four hot handlers.
    # Repointing vectors leaves their original bodies byte-for-byte intact.
    for slot, name, target, original in (
            (15, "SysTick", hdr.systick_irq, 0x081196D9),
            (24, "audio/EXTI2", hdr.audio_irq, 0x08119845),
            (27, "DMA1_Stream0", hdr.dma_irq, 0x0810F209),
            (66, "TIM5 (sequencer tick)", hdr.tim5_irq, 0x081194A5)):
        addr = BASE + 4 * slot
        poke(addr, struct.pack("<I", original), struct.pack("<I", target),
             "vector %s -> DWT wrapper 0x%08X" % (name, target))

    # ---- A4/B1: _sbrk redirected to the replacement -------------------
    # take the two pointers the stock _sbrk keeps in its literal pool so the
    # replacement uses the same brk variable and heap size constant
    heapsize_ptr, brk_ptr = struct.unpack_from("<II", image, 0x08119698 - BASE)
    if not (0x08100000 <= heapsize_ptr < 0x08200000 and
            0x20000000 <= brk_ptr < 0x20020000):
        raise SystemExit("_sbrk literals look wrong: 0x%08X / 0x%08X"
                         % (heapsize_ptr, brk_ptr))
    struct.pack_into("<II", image, blob_off + Header.OFF_BRK_PTR,
                     brk_ptr, heapsize_ptr)
    poke(0x0815EA38, struct.pack("<I", 0x40000),
         struct.pack("<I", 0x3C000),
         "D2 heap 256 KiB -> 240 KiB (top 16 KiB fixed event/ISR pools)")
    poke(0x08119678, bytes.fromhex("074a0346"),
         thumb_b_w(0x08119678, hdr.sbrk & ~1),
             "_sbrk -> np_sbrk (returns -1, tracks the high-water mark)")

    # All C++ allocations made from exception context use bounded pools;
    # after startup the hot event/deque size classes do too.  Both delete
    # entry points are live in this binary, so patch both.
    poke(0x081591D4, bytes.fromhex("012838bf"),
         thumb_b_w(0x081591D4, hdr.new & ~1),
         "operator new -> bounded event/ISR pools / ordinary malloc")
    poke(0x0815906C, bytes.fromhex("01f030b8"),
         thumb_b_w(0x0815906C, hdr.delete & ~1),
         "operator delete helper -> pool-aware delete")
    poke(0x08159070, bytes.fromhex("fff7fcbf"),
         thumb_b_w(0x08159070, hdr.delete & ~1),
         "operator delete -> pool-aware delete")

    # Preserve the allocator's PRIMASK semantics, but time every interval in
    # which it globally masks interrupts.
    poke(0x081196A0, bytes.fromhex("84b08de8"),
         thumb_b_w(0x081196A0, hdr.malloc_lock & ~1),
         "__malloc_lock -> profiled PRIMASK lock")
    poke(0x081196B8, bytes.fromhex("84b010b4"),
         thumb_b_w(0x081196B8, hdr.malloc_unlock & ~1),
         "__malloc_unlock -> profiled PRIMASK unlock")

    # ---- U: event-ordered UI state and explicit unlink release --------
    # --no-ui-chords leaves the stock UI helpers in place: an A/B switch for
    # UI-state complaints (a mode that cannot be left, stale LEDs).
    if args.ui_chords:
      poke(0x08155C7C, bytes.fromhex("70b50546"),
         thumb_b_w(0x08155C7C, hdr.ui_switch & ~1),
         "switch event -> TYPE/WAVE unlink release guard")
      poke(0x081504FA, thumb_bl(0x081504FA, 0x08149994),
         thumb_bl(0x081504FA, hdr.ui_begin_unlink & ~1),
         "TYPE/WAVE unlink entry -> set mode 5 (not toggle)")
      for address, prologue, target, name in zip(
            (0x0814A314, 0x0814A33C, 0x0814A358, 0x0814A374, 0x0814A390, 0x0814A3AC),
            ("10b50446", "10b50c46", "10b50c46", "10b50c46", "10b50c46", "10b50c46"),
            hdr.ui_helpers,
            ("all-up", "consumed", "consume", "long hold", "pressed", "allow")):
        poke(address, bytes.fromhex(prologue), thumb_b_w(address, target & ~1),
             "UI %s -> consumer-owned button state" % name)
      poke(0x0814A734, bytes.fromhex("38b50e29"),
         thumb_b_w(0x0814A734, hdr.ui_consume_modifier & ~1),
         "UI modifier/group suppression -> consumer-owned state")

    # ---- R: scale-aware oscillator/cutoff automation randomization ---
    poke(0x08143C14, thumb_bl(0x08143C14, 0x081576D4),
         thumb_bl(0x08143C14, hdr.random_splice & ~1),
         "RandAutomationData submit -> scale-aware OSC1/OSC2 FREQ + cutoff")

    # ---- X: output-only exponential X-envelope, no timing/state edits --
    for site, original, target in zip(
            (0x0811C69E, 0x0811129A, 0x08110ABA, 0x0811271E,
             0x08112952, 0x08112F34, 0x0811255A),
            ("94ed777a", "94f82620", "94f82620", "36ee866a",
             "04f68443", "04f5cc53", "94ed9e5a"), hdr.xenv_thunks):
        poke(site, bytes.fromhex(original), thumb_b_w(site, target & ~1),
             "X-envelope sample -> normalized exponential (state unchanged)")
    # The main ADSR publishes its level from s17 to engine+0x2F2C once per
    # tick; the filter ENV AMOUNT term, the VCA level and the CV-out
    # envelope all read that value.  np_env_rc replaces it with an RC
    # network driven by the stock stage byte; raw state at engine+0x40 and
    # every stage timer stay linear and untouched.
    poke(0x08112DDA, bytes.fromhex("05f62c73"),
         thumb_b_w(0x08112DDA, hdr.env_main & ~1),
         "main ADSR level -> RC model (VCA, filter env, CV out)")

    # ---- Y: a modulation slot is evaluated once per block ------------
    # The stock runs every slot's evaluator on all eight samples of the
    # block whatever the slot does.  Measured: a factory-default project
    # costs the same 28,670 instructions as a loaded one, and stubbing both
    # evaluators leaves 13,052 with the output bit-identical.  The wrapper
    # holds a non-audio-rate slot's level for the rest of the block; an
    # audio-rate slot (owner+0x25) is tail-called unchanged.
    for site in (0x08111858, 0x08111D08, 0x081127D4, 0x08112BF4,
                 0x08112D16, 0x08112D28, 0x0811317E, 0x081131C8):
        poke(site, thumb_bl(site, 0x0811108C),
             thumb_bl(site, hdr.eval_linad & ~1),
             "LinAD slot evaluator -> once per block (Y)")
    for site in (0x08112CEC, 0x08112CFC):
        poke(site, thumb_bl(site, 0x08110894),
             thumb_bl(site, hdr.eval_expad & ~1),
             "ExpAD slot evaluator -> once per block (Y)")
    # ---- Y2: setRate only when its input moved -----------------------
    poke(0x08112CD8, thumb_bl(0x08112CD8, 0x08156910),
         thumb_bl(0x08112CD8, hdr.set_rate & ~1),
         "per-sample setRate -> skipped when the rate is unchanged (Y2)")

    # ---- Y3: the audio-rate slot walk, once per block ----------------
    # 0x08112C70 walks all nineteen slot pointers on every sample to find
    # the audio-rate ones: ~114 instructions a sample for the same handful.
    # The first sample of a block collects them; the other seven hand the
    # stock loop the compact list.  Two splices: the entry jump (the vstr
    # before it is replayed) and the bl right after the loop's exit.
    poke(0x08112C4A, bytes.fromhex("c3ed007a0fe0"),
         thumb_b_w(0x08112C4A, hdr.slot_walk & ~1) + b"\x00\xbf",
         "audio-rate slot walk -> cached per block (Y3)")
    poke(0x08112CE2, thumb_bl(0x08112CE2, 0x08111848),
         thumb_bl(0x08112CE2, hdr.after_walk & ~1),
         "...and its exit restores r6/fp (Y3)")

    # ---- Y4 (WITHDRAWN): memoising the three per-sample sqrt lookups ---
    # These looked like pure functions of (s0, [r0+0x38]), but site A (the
    # main ADSR attack increment) is called ~2560 times per settle with an
    # input that varies, and the memo's hit path re-stores a stale cached
    # increment over the freshly computed one -- so the attack knob stopped
    # changing the attack time (the increment latched at its default while
    # decay, on a different cache slot, still worked).  Bisected against
    # pure stock: stock 0.000004..0.010670 across the knob, memo build stuck
    # at 0.000070.  The win was ~200 instructions at rest only; not worth a
    # broken knob.  np_sqrtlut stays in the blob, uninstalled.
    #     for site, real, wrapper in zip((0x081132C2, 0x0811336E, 0x081134C6),
    #                                    (0x081560A8, 0x08156114, 0x08156180),
    #                                    hdr.sqrtlut):
    #         poke(site, thumb_bl(site, real), thumb_bl(site, wrapper & ~1), ...)

    # ---- A2b: the journal writer must not trip the watchdog -----------
    # 0x08143378 erases bank-1 sectors 2..5 (4 x 128 KB) and programs
    # 512 KB with main spinning in the HAL wait loop -- seconds, and past
    # the 16 s IWDG at the erase-time maximum.  The fence lets SysTick feed
    # the dog for the duration (60 s cap) and records how long it took.
    poke(0x0812407C, thumb_bl(0x0812407C, 0x08143378),
         thumb_bl(0x0812407C, hdr.flash_write & ~1),
         "project journal write -> fenced for the watchdog (A2b)")

    # ---- M2: the LED fade converges ----------------------------------
    # 0.33 s + 0.66 t settles at 0.985 t, so a brightening LED's floor is
    # t-1 for ever and it stays dirty: 15-23 LEDs re-sent every 70 ticks.
    poke(0x0810A098, bytes.fromhex("27ee067aa7eea67a"),
         thumb_bl(0x0810A098, hdr.led_smooth & ~1) + b"\x00\xbf\x00\xbf",
         "LED fade 0.33/0.66 -> exact thirds with a landing (M2)")

    # ---- S1: the pattern store re-sorts only a list that is out of order
    # main() commits pending step edits through 0x0815888C every iteration
    # and 0x081585C0 ends each step with std::list::sort -- also when there
    # was nothing to commit -- so an unchanged pattern is re-sorted on every
    # pass, under a 125 us budget per track.  The sort is stable on the byte
    # at node+8, so a list already in order comes back identical: skip it.
    if args.seq_sort:
        poke(0x08158708, thumb_bl(0x08158708, 0x081584AC),
             thumb_bl(0x08158708, hdr.list_sort & ~1),
             "pattern step std::list::sort -> skipped when already in order (S1)")
    # ---- S2: ...and main()'s pass is skipped when it would find nothing
    # No clear, no rotation, no pending edit on any step, every step in
    # order: the stock pass changes nothing but its own cursor, which only
    # the worker and the 0x08158A48 flush (left on the stock call) read.
    if args.seq_idle:
        poke(0x08146E76, thumb_bl(0x08146E76, 0x0815888C),
             thumb_bl(0x08146E76, hdr.seq_worker & ~1),
             "pattern store pass, track 0 -> skipped when idle (S2)")
        poke(0x08146E82, thumb_b_w(0x08146E82, 0x0815888C),
             thumb_b_w(0x08146E82, hdr.seq_worker & ~1),
             "pattern store pass, track 1 -> skipped when idle (S2)")

    # ---- T1: the morph pattern's unlinked X-Env trigs follow the knob ---
    # The step processor ticks every pending event of both tracks; notes of
    # the morph track (1) are dropped below morph 0.02 by 0x08117A44, its
    # ParamTrig events are not gated at all.  Skip their tick on track 1
    # while morph <= 0.01 (0x0811612C's own threshold for them).
    if args.morph_trig_fix:
        poke(0x08146804, bytes.fromhex("9047aee7"),
             thumb_b_w(0x08146804, hdr.morph_trig & ~1),
             "step processor event tick -> morph track's trigs gated by morph (T1)")

    # ---- V: the dim SEQ-key state, dimmer --------------------------------
    # The SEQ-key renderers light a step with a note, a step with a lock of
    # the selected parameter and the current pattern at 16 (24), and the
    # "also there" state -- a long note's tail, a note step in a parameter
    # view, a pattern with content -- at 5.  The HT16D35A's 64 PWM levels are
    # linear and the eye is roughly logarithmic: 5/63 next to 16/63 reads as
    # almost the same.  The dim level is the immediate of one instruction in
    # each of the five renderers; pattern mode's rarer class (a pattern with
    # a patch and no steps, 2) becomes 1 so the two stay apart.
    if args.seq_dim != 5:
        n = args.seq_dim
        for addr, stock, new_bytes, what in (
                (0x0814F05E, "0527", "%02x27" % n, "step view: long-note tail"),
                (0x0814F0CE, "4ff0050a", "4ff0%02x0a" % n, "pattern mode: pattern with content"),
                (0x0814F1E4, "4ff00509", "4ff0%02x09" % n, "step view variant: note step"),
                (0x0814F28C, "4ff00509", "4ff0%02x09" % n, "step view variant: note step"),
                (0x0814F33C, "4ff0050b", "4ff0%02x0b" % n, "parameter view: note step")):
            poke(addr, bytes.fromhex(stock), bytes.fromhex(new_bytes),
                 "SEQ LED dim 5 -> %d (%s) (V)" % (n, what))
        if n <= 2:
            poke(0x0814F0F0, bytes.fromhex("6400"), bytes.fromhex("2400"),
                 "pattern mode: patch-only pattern 2 -> 1 (V)")

    # ---- Q1: a flash-touching paste waits for the QSPI queue ------------
    # FUNC+RECORD saves asynchronously (a coroutine on the QSPI driver's
    # queue, erase/program by interrupt and DMA); the five paste events
    # that reach the flash directly (patch param, patch, pattern, mod note
    # across projects; whole project) run synchronously.  One arriving
    # mid-save finds the erase buffer borrowed -> abort() -> `b .` in
    # main (stock: frozen for good; here: the watchdog reboot), or the HAL
    # busy -> Error_Handler.  The project service now leaves such an event
    # at the head of its list until the queue is empty.
    if args.qspi_defer:
        poke(0x08157672, bytes.fromhex("e3699a68"),
             thumb_b_w(0x08157672, hdr.project_gate & ~1),
             "project service loop head -> paste deferred while QSPI queue busy (Q1)")

    # ---- MF: MIDI flow ---------------------------------------------------
    # MF1/MF2: the output fan-out, rewritten (np_midi_fanout): with USB up an
    # event waits for USB staging room (stock: past 64 packets per flush,
    # dropped silently) and its DIN copy is whole or skipped (stock: the DIN
    # ring dropped its oldest byte, mid-message); with USB down it waits for
    # DIN room.  MF3 (DIN receive ring) lives in J's np_midi_rx; MF4: the USB
    # receive ring stops moving the reader's index from the interrupt.
    if args.midi_flow:
        poke(0x08119A2C, thumb_bl(0x08119A2C, 0x0811A904),
             thumb_bl(0x08119A2C, hdr.midi_fanout & ~1),
             "main: MIDI out fan-out -> np_midi_fanout, per-port flow (MF1/MF2)")
        poke(0x08108D4A, thumb_bl(0x08108D4A, 0x08110330),
             thumb_bl(0x08108D4A, hdr.usb_rx_put & ~1),
             "USB-MIDI receive ring push -> drops the newest when full (MF4)")
    if args.polyat_fix:
        poke(0x0811B754, bytes.fromhex("23d058d8"),
             thumb_b_cond_w(0x0811B754, hdr.polyat & ~1, 0x2),
             "MIDI in: poly pressure takes its second data byte (MF5)")

    # ---- N1: a note starts on its own parameters ----------------------
    # Every CV destination passes a 100 Hz one-pole de-zipper; a note-on
    # that switches the parameters (a mod note, a step lock) slid ~4 ms
    # from the previous note's values.  The note start asks the DSP for
    # eight samples with the smoothers bypassed (np_dsp_sample).  Legato
    # notes do not pass here, so slides keep their glide.
    if args.note_snap:
        poke(0x08116D30, thumb_b_w(0x08116D30, 0x0811612C),
             thumb_b_w(0x08116D30, hdr.note_start & ~1),
             "note start -> parameter smoothers snapped for the new note (N1)")
        poke(0x0811503C, thumb_bl(0x0811503C, 0x08112488),
             thumb_bl(0x0811503C, hdr.dsp_sample & ~1),
             "per-sample DSP call -> snaps the smoothers when asked (N1)")

    # ---- N2: a mod note's parameters without an event each -----------
    # 0x081209F4 allocated, type-dispatched and freed a ControllerEvent per
    # stored parameter only for its handler to store the bank value and
    # re-evaluate the parameter; np_modnote_replay does those two directly.
    if args.fast_replay:
        poke(0x081209F4, bytes.fromhex("8318 2de9"),
             thumb_b_w(0x081209F4, hdr.modnote_replay & ~1),
             "mod-note parameter replay -> direct bank writes (N2)")

    # ---- CH: an instrument switch fades the sounding one out first ----
    # A note-on that switches instrument while the amp envelope still
    # sounds is held back; the DSP fades the VCA level to 0 over the fade,
    # puts every envelope at rest at 0, and the note then starts from
    # silence -- the same as after itself.  --choke-ms 0 turns it off.
    samples = int(round(args.choke_ms * 32))
    struct.pack_into("<I", image, blob_off + 0x1B4, samples)
    if samples:
        poke(0x08117AD0, thumb_bl(0x08117AD0, 0x081170E8),
             thumb_bl(0x08117AD0, hdr.cnote_entry & ~1),
             "note-on -> held back across an instrument switch (CH)")
        poke(0x08117594, bytes.fromhex("2de9f043"),
             thumb_b_w(0x08117594, hdr.noteoff_entry & ~1),
             "note-off -> lets a held note-on go first (CH)")

    # ---- R1: the mod-note randomizer builds an instrument --------------
    # TEMPO+FUNC+note in mod-note mode.  The stock handler gives some thirty
    # unrelated parameters independent values; np_modnote_random picks an
    # instrument by weight and builds it from a recipe.  The entry of the
    # stock handler becomes a branch; its one caller (the thunk 0x0811EC6C)
    # and its contract (take the event, delete it) are unchanged.
    if args.smart_random:
        poke(0x0814476C, bytes.fromhex("08680028"),
             thumb_b_w(0x0814476C, hdr.modnote_random & ~1),
             "mod-note randomizer -> instrument recipes (R1)")

    # ---- R2: black + white keys pick the instrument group --------------
    # In mod-note mode a white key pressed while black keys are held builds
    # an instrument from their groups (C# kick, D# other drums, F# basses,
    # G# tonal, A# effects) in that key's mod note and plays it at once;
    # neither that press nor its release records a step.  The black keys'
    # state is the UI consumer's (np_ui.cpp), so this needs the UI fix.
    if args.smart_random and args.black_keys and args.ui_chords:
        poke(0x081521AA, thumb_b_w(0x081521AA, 0x0814FE5C),
             thumb_b_w(0x081521AA, hdr.mn_key_press & ~1),
             "mod-note key press -> black keys pick the instrument (R2)")
        poke(0x0814FEB8, bytes.fromhex("94f8ed30"),
             thumb_bl(0x0814FEB8, hdr.mn_rec_press & ~1),
             "mod-note key press: RECORD flag, masked for R2")
        poke(0x081521FE, bytes.fromhex("94f8ed20"),
             thumb_bl(0x081521FE, hdr.mn_rec_release & ~1),
             "mod-note key release: RECORD flag, masked for R2")

    # ---- K1-K3: TEMPO+FUNC + a black key in mod-note mode ---------------
    # The TEMPO+FUNC randomize handler (0x081524CC) checks its key with
    # 0x0814A3E4, which turns a black key away; the replacement builds for
    # C# eight drums sharing one character (K1), for D# a bass kit over the
    # page in the scale (K2), for A# every mod note (K3), and answers "no".
    if args.smart_random and args.kit:
        poke(0x081524D4, thumb_bl(0x081524D4, 0x0814A3E4),
             thumb_bl(0x081524D4, hdr.mn_chord_check & ~1),
             "mod-note TEMPO+FUNC key check -> C# drum kit, D# bass kit, A# all (K1-K3)")

    # ---- P1: TEMPO+FUNC+note builds the pattern's patch (normal mode) ---
    # Stock, the chord only plays the note.  The thunks that hand a key's
    # press and release to the normal-mode handlers (0x08151DA4 /
    # 0x08151FFC) end in a `b.w`; it goes to np_nm_key_press / _release,
    # which build an instrument into the patch on the chord (a black key
    # picks the group, as R2) and call the stock handler, with the RECORD
    # flag held at 0 for the chord's key so it records no step.
    if args.smart_random and args.patch_random and args.ui_chords:
        poke(0x08147B98, thumb_b_w(0x08147B98, 0x08151DA4),
             thumb_b_w(0x08147B98, hdr.nm_key_press & ~1),
             "normal-mode key press -> TEMPO+FUNC builds the patch (P1)")
        poke(0x08147BA0, thumb_b_w(0x08147BA0, 0x08151FFC),
             thumb_b_w(0x08147BA0, hdr.nm_key_release & ~1),
             "normal-mode key release -> after a P1 chord, records nothing")

    # ---- D1: DELETE+RECORD clears the patch, not mod note 0 -------------
    # ClearPatchData's handler resets every record whose byte +4 is the
    # layer; a mod note's records (type 7) keep the mod note there, so the
    # stock wipes mod note 0 (and, for layer 1, mod note 1) with the patch.
    if args.clear_fix:
        poke(0x08127A12, bytes.fromhex("1a79b242"),
             thumb_bl(0x08127A12, hdr.clear_match & ~1),
             "clear patch: record match skips mod notes (D1)")

    # ---- PJ: a project journal cut short no longer hangs the load -------
    # The replay reads [id][payload] events; an id with no factory (0xFF
    # where a save was cut off by a power loss, or an erased slot) called
    # an empty std::function: __throw_bad_function_call -> abort -> b .,
    # before the stock's own checks.  Slot 0 is loaded at power-on, so the
    # unit hung there for good.  Now such an id ends the journal as id 0
    # already does, so a journal without its EOF fails the stock's length
    # check (0xA1); a first event that is no event fails as 0xA4 before
    # anything is touched (the stock's "not a SOF" branch would import the
    # bytes as an old-format project: 255-step patterns from 0xFF).  A
    # failed check is the stock's error screen (0x0814FA54: the code on SEQ
    # 1-8, keys 30+31 send the project out over USB, then it halts); at
    # power-on PB starts an empty project instead.
    if args.journal_fix:
        poke(0x08123CAE, bytes.fromhex("2368027c"),
             bytes.fromhex("0023") + thumb_b_n(0x08123CB0, 0x08123C2E),
             "journal reader: an unknown event id ends the journal (PJ)")
        poke(0x08124406, bytes.fromhex("037c07f201178df82430"),
             bytes.fromhex("a421") + thumb_bl(0x08124408, hdr.project_fail & ~1)
             + thumb_b_n(0x0812440C, 0x08124356) + bytes.fromhex("00bf"),
             "journal replay: a first event that is no event fails as 0xA4 (PJ)")

    # ---- PB: a damaged slot 0 at power-on starts an empty project -------
    # The replay's checks (0xA1..0xA4) go through np_project_fail: during
    # the power-on load of slot 0 (np_boot_load) a failure is noted and an
    # empty project replaces the journal; otherwise the stock screen.
    if args.boot_fallback:
        poke(0x081199AE, thumb_bl(0x081199AE, 0x08124440),
             thumb_bl(0x081199AE, hdr.boot_load & ~1),
             "power-on load of slot 0 -> an empty project if it fails (PB)")
        for site, code in ((0x08124166, 0xA2), (0x08124362, 0xA3), (0x0812436E, 0xA1)):
            poke(site, thumb_bl(site, 0x0814FA54),
                 thumb_bl(site, hdr.project_fail & ~1),
                 "project check 0x%02X -> noted at power-on (PB)" % code)

    # ---- LV: the LEDs levelled ------------------------------------------
    # np_led_smooth (M2, at 0x0810A098) levels each LED's target before its
    # fade: every lit LED to one low level, the playhead and the tempo blink
    # brighter; TEMPO+FOLLOW steps the preset until the next power-on.
    # --led-level picks the one it starts on (5 = the stock's own levels).
    struct.pack_into("<I", image, blob_off + 0x1F4, args.led_level)

    # ---- RG: a pattern rotate stays inside the step buckets ------------
    # FUNC+UP/DOWN on a pattern of length 0 asked 0x081583AC for the range
    # 0..255 of a 128-bucket store, and the rotate pass swapped its way
    # through the memory past it until a list node faulted.  A reversed
    # range wraps the pass's byte index the same way: only
    # start <= end <= 127 goes through.
    if args.rotate_guard:
        poke(0x08145EE0, thumb_b_w(0x08145EE0, 0x081583AC),
             thumb_b_w(0x08145EE0, hdr.rotate_guard & ~1),
             "pattern rotate setup -> only start <= end <= 127 (RG)")

    # ---- PL: a project load while playing waits for the pattern's end ---
    # The LOAD key's SEQ press calls the load (0x0811F8DC) directly; the
    # read's completion replays the new project at once, mid-pattern.  The
    # call, the completion (the std::function invoker literal the load
    # builds its callback from), main's project service and the save key
    # go through np_runtime.cpp / np_patch.S: playing, the replay waits for
    # the last kLead ticks of the pattern and the tracks restart at step 1.
    if args.sync_load:
        poke(0x08149DC6, thumb_bl(0x08149DC6, 0x0811F8DC),
             thumb_bl(0x08149DC6, hdr.load_request & ~1),
             "project load request -> counted when playing (PL)")
        poke(0x0811F938, struct.pack("<I", 0x081244D1),
             struct.pack("<I", hdr.load_done | 1),
             "project load completion -> replay deferred when playing (PL)")
        poke(0x08119A70, thumb_bl(0x08119A70, 0x08125AC4),
             thumb_bl(0x08119A70, hdr.proj_service & ~1),
             "main project service -> new project at the pattern's end (PL)")
        poke(0x081516A2, thumb_bl(0x081516A2, 0x081263C0),
             thumb_bl(0x081516A2, hdr.save_gate & ~1),
             "save key -> ignored while a load waits (PL)")

    # ---- EV: X-Env event log (diagnostic) -----------------------------
    # 0x08156074 is the stock X-Env trigger, `movs r3,#1; strb r3,[r0,#0x2f];
    # strb r3,[r0,#0x2d]; bx lr`, called by the note path and the param-trig
    # (unlinked X-Env) path.  The first two instructions become a branch to
    # np_ev_trig, which logs the slot and replays them.  np_xenv.S logs the
    # RC envelopes' restarts (compiled out with --no-ev-log).
    if args.ev_log:
        poke(0x08156074, bytes.fromhex("012380f82f30"),
             thumb_b_w(0x08156074, hdr.ev_trig & ~1) + b"\x00\xbf",
             "X-Env trigger -> logged by slot and caller (EV, diagnostic)")

    # ---- G1/G2/G2b: synced X-Mod LFO ----------------------------------
    # The stock handler never refreshes rate.  Its modulo gate is live on
    # the second caller, the MIDI F8 path, but integer-truncates fractional
    # divisions and drifts.  Redirect the whole function to the replacement,
    # which clamps the table index and tests exact rational boundaries.
    if args.lfo:
        poke(0x0811CC70, bytes.fromhex("836893f8"),
             thumb_b_w(0x0811CC70, hdr.mod_tick & ~1),
             "mod slot tick -> np_mod_tick (live rate, rational phase gate)")

    # ---- G3: withdrawn ------------------------------------------------
    # This slot used to rewrite four float literals, 126 -> 127, in the two
    # X-Mod handlers, on the theory that the audio-rate position was scaled
    # by the wrong constant.  Both premises turned out to be wrong.
    #
    # The value being divided is project+0x2F20 (LFOSpeed) or project+0x2F60
    # (LFOTime).  Scanning every instruction in the image that can form those
    # two addresses -- as a displacement off a folded base, not just as a
    # literal -- gives four readers, all in the handlers themselves, and one
    # writer: the zero-initialiser at 0x08147612.  Nothing else touches them,
    # so the term is 0/126 and the divisor is unobservable.
    #
    # The live audio-rate position is not computed here at all.  The per-block
    # DSP update walks the 19 slots at 0x08112C70, and for every slot flagged
    # audio-rate (owner+0x25) recomputes the position and calls setRate again,
    # overriding whatever the handler wrote.  There the 1/126 is applied to
    # project+0x2EDC, which was itself built as 126 * position + fine tune at
    # 0x08112810 -- the divide undoes that multiply, so 126 is correct and the
    # normalised position still spans the table's full MIDI 0..127.
    #
    # Kept as a comment rather than deleted: the four literal addresses are
    # 0x0811BD2C, 0x0811BD40, 0x0811BF14, 0x0811BF20, and someone will
    # rediscover them.

    # ---- H: a fourth X-Mod type, audio rate with keytracking ----------
    # Stock has three types -- free LFO, synced LFO, audio rate -- selected
    # by the LFOTime parameter, whose value carries the type as n/65536.
    # In audio rate the modulator's pitch does not move with the keyboard,
    # so an FM timbre tuned at one note is a different timbre at the next.
    # Type 3 is audio rate plus one semitone of modulator pitch per key.
    #
    # Eleven sites.  The parameter table makes type 3 legal for the generic
    # parameter path, but the UI has three separate actions which each keep
    # their own hard-coded `% 3`.  All three must become `% 4`; changing only
    # the table is why the previous image still cycled 0 -> 1 -> 2 -> 0 from
    # the physical TYPE key.
    if args.xmod:
        # The encoder wraps a discrete parameter with `raw % table[local]`
        # in the modulation evaluator at 0x08115804.  Entry 7 is LFOTime.
        # Its five neighbours confirm the reading: entry 6 is 5 (the five
        # LFO waveforms) and entry 8 is 2 (the two X-Env destinations).
        # This literal pool address has exactly one reader in the image.
        # 4 also divides 256, so the cycle no longer stutters where the
        # underlying byte wraps, which 3 did.
        poke(0x0815E8D6, struct.pack("<H", 3), struct.pack("<H", 4),
             "X-Mod type count 3 -> 4 (wrap table entry 7)")

        # The three UI implementations of the type-cycle have a second,
        # independent limit.  In each case the conditional arm selected by
        # `param == 2` is local parameter 7 (X-Mod TYPE); the other arm is
        # local 6 (five LFO waveforms) and remains untouched.
        #
        # 0x08150020 is the ordinary TYPE action reached by the physical
        # button.  The `moveq r7,#3` below is its modulo divisor.
        poke(0x08150102, bytes.fromhex("0327"), bytes.fromhex("0427"),
             "TYPE UI cycle (direct) 3 -> 4")

        # 0x0815053C is the alternate/contextual editor path.  It uses udiv
        # with r8 as the divisor, so it needs the same change.
        poke(0x08150556, bytes.fromhex("4ff00308"), bytes.fromhex("4ff00408"),
             "TYPE UI cycle (alternate) 3 -> 4")

        # 0x081506C8 is the batch UI path: it loops over the eligible entries
        # and uses sdiv with r3 as the divisor.  Leaving it at three would
        # silently erase type 3 when that editor is used.
        poke(0x081506EC, bytes.fromhex("0323"), bytes.fromhex("0423"),
             "TYPE UI cycle (batch) 3 -> 4")

        # The LFO core's setMode is the only writer of owner+0x10C and it
        # drops anything above 2: `cmp r1,#2 / it ls / strbls.w r1,[r0,#0xE4]`.
        # Without this, type 3 would leave the previous type in place.
        # Three stock sites used to be widened here so that a mode byte of 3
        # would be accepted: setMode's bound and the evaluator's two
        # audio-rate tests.  None of them is touched any more.  np_xmod_time
        # publishes 2 for the ratio type, so every stock reader of
        # owner+0x10C sees an ordinary audio-rate slot and behaves exactly
        # as it always did; the fourth type is carried in owner+0x25, whose
        # only reader is the DSP splice below.

        # Both parameter handlers go to C++.  A fourth arm does not fit in
        # the stock `cmp #1 / cmp #2 / cmp #0 / else` dispatch, and the
        # replacements also stop LFOTime's copy from computing its
        # audio-rate position out of the type value instead of the rate.
        poke(0x0811BD48, bytes.fromhex("30b59fed"),
             thumb_b_w(0x0811BD48, hdr.xmod_time & ~1),
             "LFOTimeCParam::vfn2 -> np_xmod_time (X-Mod TYPE)")
        poke(0x0811BBA8, bytes.fromhex("30b50446"),
             thumb_b_w(0x0811BBA8, hdr.xmod_speed & ~1),
             "LFOSpeedCParam::vfn2 -> np_xmod_speed (X-Mod RATE)")

        # And the per-block DSP loop, which is what actually decides an
        # audio-rate modulator's pitch -- it recomputes and calls setRate
        # for every audio-rate slot, every block.  Two instructions are
        # displaced into the splice; the trailing halfword is never
        # executed but is left as a nop rather than as half of a vsub.
        poke(0x08112C88, bytes.fromhex("027977ee"),
             thumb_b_w(0x08112C88, hdr.xmod_pos & ~1),
             "DSP slot loop -> np_xmod_pos (ratio, and its pitch base)")
        # The note handler commits an incoming note to one of two per-
        # oscillator slots, and which one depends on whether the step is
        # legato or slid (0x0811728A vs 0x0811757C).  Capture it here, on
        # the call both of those paths make once the choice is made, so the
        # ratio has a note to work from that does not depend on the choice.
        poke(0x081172B0, bytes.fromhex("fff74afd"),
             thumb_bl(0x081172B0, hdr.note_hook & ~1),
             "note handler -> np_note_hook (records the note just played)")
        poke(0x08112C8C, bytes.fromhex("c77a"), bytes.fromhex("00bf"),
             "tail of the displaced vsub -> nop")

        # H4: the modulator's waveform, where it is actually rendered.
        #
        # H2/H3 band-limit MultiWaveOscFast's own renderers, but a
        # modulation slot's oscillator is not rendered there.  Each of the
        # three per-slot evaluators inlines all five waveforms twice, once
        # per arm of the X-Envelope branch -- six naive copies that share
        # only the phase and step fields with MultiWaveOscFast.  Replace
        # each `cmp r3,#4 / bhi.w <bail> / tbb [pc,r3]` with a jump to the
        # band-limited renderer, which reproduces the phase advance, the
        # wrap, the S&H behaviour and the out-of-range 0.0 the bail loads.
        for addr, entry, what in zip(
                (0x0811C6E0, 0x0811C892, 0x081112F8,
                 0x08111508, 0x08110B18, 0x08110D34),
                hdr.wave_thunks,
                ("QuantizedLinAD (slots 0,4,9) X-Env on",
                 "QuantizedLinAD X-Env off",
                 "LinAD (1,2,5,6,10,11,12,17) X-Env on",
                 "LinAD X-Env off",
                 "ExpAD (slots 3,7,8; X-Mod is 8) X-Env on",
                 "ExpAD X-Env off")):
            poke(addr, bytes.fromhex("042b00f2"),
                 thumb_b_w(addr, entry & ~1),
                 "waveform dispatch -> band-limited: " + what)

    # ---- H2: an honest sine for the modulator -------------------------
    # Waveform 0 of MultiWaveOscFast is the parabola y = x*(2-|x|): five
    # instructions, 3.8 % THD, third harmonic at -28.6 dB.  That is a
    # reasonable choice on a part without an FPU and a poor one here -- as
    # an FM modulator each of those harmonics opens its own family of
    # sidebands, so the tone is muddy however exact the ratio is.
    #
    # Replaced with a folded degree-7 polynomial: THD 0.00005 %, third
    # harmonic -167 dB, about ten instructions more.  The splice sits at
    # 0x08156656 over `vmov.f32 s13, #0.25`; the only branch into the region
    # it takes over is the `bpl` two instructions earlier, which targets the
    # splice itself.
    if args.sine:
        poke(0x08156656, bytes.fromhex("f5ee006a"),
             thumb_b_w(0x08156656, hdr.sine & ~1),
             "MultiWaveOscFast sine -> np_sine (parabola -> degree 7)")

    # ---- H3: band-limited saw, square and triangle --------------------
    # Waveforms 1..3 are TriangleOscNaive, SquareOscNaive and
    # SawtoothOscNaive.  The saw and the square are sampled straight through
    # their discontinuities, so everything above Nyquist folds back; at the
    # 32 kHz rate this firmware runs, an audio-rate saw carries about as
    # much aliasing as harmonics.  polyBLEP on the steps and polyBLAMP on
    # the triangle's corners: 16 dB, 16 dB and 4 dB less alias energy.
    #
    # Each splice sits where the stock renderer has finished wrapping the
    # phase and both wrap paths converge, so one branch catches every route
    # in.  The phase store the stock code does two instructions later is
    # reproduced in the thunk.
    if args.sine:
        for addr, old, entry, what in (
                (0x0815669A, "f0eee76a", hdr.tri, "triangle -> np_tri (polyBLAMP)"),
                (0x081566D2, "f5eec07a", hdr.square, "square -> np_square (polyBLEP)"),
                (0x0815670E, "37eea70a", hdr.saw, "sawtooth -> np_saw (polyBLEP)")):
            poke(addr, bytes.fromhex(old), thumb_b_w(addr, entry & ~1),
                 "MultiWaveOscFast " + what)

    # ---- J: MIDI DIN on the USART interrupt ----------------------------
    # The byte sender is reached by SysTick's once-per-fourth-tick arm, i.e.
    # 1 kHz, not 4 kHz.  It is safe from TDR overwrite but throttles a wire
    # capable of 3125 bytes/s to 1000 bytes/s.  The call now only enables
    # TXEIE; the combined USART handler drains one byte per hardware TXE.
    if args.midi:
        poke(0x0811AC90, bytes.fromhex("d0f81c3c"),
             thumb_b_w(0x0811AC90, hdr.midi_tx & ~1),
             "MIDI DIN transmit -> np_midi_tx (enable TXE IRQ)")

        # The receive ISR never reads USART1->ISR, so a byte that arrived
        # with a framing or noise error is fed to the MIDI parser as though
        # it were sound -- a note nobody played.  Drop those, count them,
        # and count overruns separately (the byte is valid there, an older
        # one was already lost).
        poke(0x0810EEA0, bytes.fromhex("10b5054c"),
             thumb_b_w(0x0810EEA0, hdr.midi_rx & ~1),
             "USART1 IRQ -> combined RX/error/TXE handler")

        # Do not let wire-rate TX interrupts preempt the audio engine.  This
        # changes only the NVIC call; r5 remains 1 for the UART init struct.
        poke(0x081093AA, bytes.fromhex("2946"), bytes.fromhex("0221"),
             "USART1 NVIC priority 1 -> 2")

    # USB polling is not interrupt work.  SysTick remembers the driver and
    # the main-loop hook calls the original service routine.
    poke(0x08119720, thumb_bl(0x08119720, 0x08110244),
         thumb_bl(0x08119720, hdr.usb_defer & ~1),
         "SysTick USB poll -> defer to main loop")

    # NVIC implements four priority bits.  Passing 16 is masked to zero,
    # accidentally making QSPI the highest-priority IRQ in the system.
    poke(0x0810903C, bytes.fromhex("1021"), bytes.fromhex("0f21"),
         "QSPI NVIC priority 16 -> 15")

    # ---- K: let the audio engine outrank MIDI -------------------------
    # The CV engine runs on EXTI2 at NVIC preemption 5.  Above it sit
    # SysTick, SPI3 and DMA1_Stream0 at 2 and TIM3 at 3 -- and SysTick is
    # where the USB stack is polled and where DIN bytes are pushed out, so
    # every MIDI byte can interrupt audio.  Raising EXTI2 to 1 puts it with
    # EXTI4 and above all of them.
    #
    # This does not change how much CPU the engine uses, only who waits.  It
    # is enabled by default now that USART TX can interrupt at wire rate.
    # An A/B on the unit isolated the panel freeze to exactly this byte: the same
    # image with the stock 5 runs clean, across eleven diagnostic builds.  K's
    # purpose -- keep USB polling and DIN bytes from preempting the engine --
    # is covered by P (USB deferred to the main loop) and J1 (TXE-driven DIN),
    # so since 2026-09-17 the default is the stock 5; --audio-priority opts in.
    # Level 2 is the middle ground --
    # equal to SysTick, DMA1_Stream0 and IRQ50, so none of them preempts any
    # other and the engine still never yields to a MIDI byte mid-block.
    exti2 = args.exti2_priority
    if exti2 is None:
        exti2 = 1 if args.audio_priority else 5
    if exti2 != 5:
        poke(0x0810F400, bytes.fromhex("0521"), bytes([exti2, 0x21]),
             "EXTI2 (CV engine) NVIC priority 5 -> %d%s" % (
                 exti2, ", above SysTick" if exti2 < 2 else
                 ", equal to SysTick (neither preempts the other)"))

    # ---- L/P6: close the previous frame only after hardware completion
    # and the 20 us start-to-CS guard. Busy calls defer, preserving dirty;
    # runtime CFG2 below supplies the distinct inter-BYTE delay as well.
    poke(0x0810A002, bytes.fromhex("10b50122"),
         thumb_b_w(0x0810A002, hdr.led_frame & ~1),
         "LED writer dirty path -> np_led_frame (hold off until the bus is idle)")
    poke(0x0810A11E, bytes.fromhex("02b010bd"),
         thumb_b_w(0x0810A11E, hdr.led_done & ~1),
         "LED writer epilogue -> np_led_done (timestamp the frame start)")

    # ---- N: make the UI event deque's start-iterator transition atomic
    # against the key/pot scanner that pushes into the same deque from
    # DMA1_Stream0.  A push landing between the load at 0x08157558 and the
    # store at 0x0815755C leaves the consumer writing back a stale map node;
    # its cursor then leaves the live map and the queue either aborts on a
    # null event or looks empty for ever -- the UI stops consuming, events
    # leak, and the panel freezes with everything else still running.
    # Only four bytes move: the stub replays the whole run and rejoins at
    # 0x0815756A, so 0x0815755C..0x08157569 is simply unreachable.
    poke(0x08157558, bytes.fromhex("236b1a1d"),
         thumb_b_w(0x08157558, hdr.ui_queue_pop & ~1),
         "UI deque block retire -> np_ui_queue_pop (atomic start iterator)")

    # Main must alternate panel dispatch and project application under load.
    # Preserve FIFO events, with bounded batches and project backpressure.
    poke(0x08119A60, thumb_bl(0x08119A60, 0x081574F8),
         thumb_bl(0x08119A60, hdr.ui_service & ~1),
         "main UI service -> bounded FIFO batches / project backpressure")
    poke(0x0815766C, bytes.fromhex("4ff00008"),
         thumb_b_w(0x0815766C, hdr.project_first & ~1),
         "project service -> first-item progress with a nonzero budget")

    # ---- O: re-arm the pot scan only when the previous row has been
    # consumed, and clear ADC1's sticky overrun before ADSTART.  The stock
    # SysTick tail re-enables DMA1 S0 and starts the ADC unconditionally and
    # nothing in the firmware ever clears OVR (ADC1's NVIC line is off).
    poke(0x0811972A, bytes.fromhex("1249124a0b6943f001030b61936843f004039360"),
         thumb_bl(0x0811972A, hdr.scan_rearm & ~1) + thumb_b_w(0x0811972E, 0x0811973E)
         + bytes.fromhex("01030b61936843f004039360"),
         "SysTick pot-scan re-arm -> np_scan_rearm (idle check, OVR clear)")

    # ---- M: publish an LED's brightness before marking it dirty.  Stock sets
    # dirty first, so a SysTick landing between the two stores sends the OLD
    # brightness and clears dirty on the way out -- the new value is then left
    # in place with nothing to send it, and that LED stops following until an
    # unrelated change happens to repair the flag.  Pure reorder of two
    # independent stores; the overlay setter at 0x0810A182 already does it
    # in this order.
    poke(0x0810A166, bytes.fromhex("80f8973080f89420"),
         bytes.fromhex("80f8942080f89730"),
         "LED brightness setter: store the value, then the dirty flag")

    # P6: configure runtime byte spacing after the last LED init command,
    # then observe the real producer / dispatch / render call sites.
    poke(0x08109DF4, bytes.fromhex("074a5368"),
         thumb_b_w(0x08109DF4, hdr.led_configure & ~1), "LED SPI mode3 / MIDI=6")
    poke(0x0814F80C, bytes.fromhex("f8b50746"),
         thumb_b_w(0x0814F80C, hdr.diag_render & ~1), "UI render counter")
    poke(0x08157324, bytes.fromhex("2de9f843"),
         thumb_b_w(0x08157324, hdr.diag_enqueue & ~1), "UI enqueue observer")
    poke(0x08157528, thumb_bl(0x08157528, 0x0815748C),
         thumb_bl(0x08157528, hdr.diag_dispatch & ~1), "UI dispatch observer")

    # ---- C: I-cache before the static constructors --------------------
    poke(0x08156022, thumb_bl(0x08156022, 0x081093F4),
         thumb_bl(0x08156022, hdr.early & ~1),
         "Reset_Handler: bl SystemInit -> np_early")

    # ---- A2/E1: diagnostics and watchdog ------------------------------
    poke(0x0815602A, thumb_bl(0x0815602A, 0x08119938),
         thumb_bl(0x0815602A, hdr.late & ~1),
         "Reset_Handler: bl main -> np_late")
    poke(0x08119A02, thumb_bl(0x08119A02, 0x08109614),
         thumb_bl(0x08119A02, hdr.wdg_hook & ~1),
         "main loop: bl System::instance -> np_wdg_hook")

    # ---- blob configuration ------------------------------------------
    flags = 0
    if args.watchdog:
        flags |= FLAG_WDG
        code, count, actual = wdg_params(args.wdg_timeout)
        image[blob_off + Header.OFF_WDG_PR] = code
        struct.pack_into("<I", image, blob_off + Header.OFF_WDG_RLR, count)
    else:
        actual = 0
    if args.lfo:
        flags |= FLAG_LFO
    if args.telemetry:
        flags |= FLAG_TL
    if args.crc:
        flags |= FLAG_CRC
        struct.pack_into("<I", image, blob_off + Header.OFF_CRC_LEN, STOCK_SIZE)
    image[blob_off + Header.OFF_FLAGS] = flags

    # CRC is taken after every code patch, over the stock extent only
    if args.crc:
        struct.pack_into("<I", image, blob_off + Header.OFF_CRC,
                         crc32(bytes(image[:STOCK_SIZE])))

    # keep the image a whole number of 32 byte flash words
    if len(image) % 32:
        image += b"\x00" * (32 - len(image) % 32)

    # Identity includes final options, all patch sites, and the blob. This
    # field is zero for the calculation. Not a cryptographic signature.
    build_id = crc32(bytes(image))
    struct.pack_into("<I", image, blob_off + 0x148, build_id)

    # ---- verify before overwriting the deliverables --------------------
    verify_sites(image, log, hdr)

    # ---- pack and round-trip before overwriting the deliverables -------
    suffix = "" if args.dsp_opt == "O3" else "-" + args.dsp_opt
    if not args.ui_chords:
        suffix += "-no-ui-chords"
    out_bin = "build/firmware-patched%s.bin" % suffix
    out_syx = "build/mono_V2.2.0r-patched%s.syx" % suffix
    packed = norand_syx.lzss_compress(bytes(image))
    syx = norand_syx.fw_pack_packed(packed)
    back = norand_syx.fw_unpack(syx)
    if back != bytes(image):
        raise SystemExit("FAILED: the generated .syx does not decode back to "
                         "the patched image")
    # The bootloader's size limits (protocol.md 3.3), all 0x80000:
    #  * the uncompressed size in the CF 00 header (0x08026746): over it the
    #    size reads as 0 and the update is refused -- AFTER bank 2 has been
    #    erased, which leaves the unit without an application (p43 did this
    #    at 525,440 bytes);
    #  * messages * 112 (0x08026C00) and the token stream (0x080264B2).
    if len(image) > 0x80000:
        raise SystemExit("FAILED: the image is %d bytes; the bootloader refuses "
                         "anything over %d (0x80000) after erasing the "
                         "application" % (len(image), 0x80000))
    messages = syx.count(b"\xf0")
    if messages * 112 >= 0x80000:
        raise SystemExit("FAILED: %d SysEx messages; the bootloader's check "
                         "(messages * 112 < 0x80000) would refuse it" % messages)

    # ---- write ---------------------------------------------------------
    open(out_bin, "wb").write(image)
    open(out_syx, "wb").write(syx)

    print("patched image : %d bytes (stock %d, blob %d at 0x%08X); "
          "%d bytes under the bootloader's 512 KB cap"
          % (len(image), STOCK_SIZE, len(blob), BLOB_BASE, 0x80000 - len(image)))
    print("MIDI build ID : %08X (P6 schema 1)" % build_id)
    print("sysex         : %d messages, %d packed bytes"
          % (len(syx) // 123, len(packed)))
    print("round trip    : OK -- %s decodes back to %s" % (out_syx, out_bin))
    print()
    print("enabled fixes :")
    print("  A1  fault handlers -> record at 0x%08X, then reset" % hdr.diag)
    print("  A4  _sbrk returns -1 on a full heap")
    print("  B1  heap high-water mark recorded at 0x%08X" % (hdr.diag + 0x34))
    print("  B2  event/ISR alloc : fixed 16 KiB pools; ordinary heap 240 KiB")
    print("  P   DWT telemetry  : audio, SysTick, TIM5 (sequencer tick), DMA1_Stream0, main")
    print("  U   UI chords: consumer-owned switch state, explicit TYPE/WAVE exit")
    print("  R   FUNC+TEMPO random FREQ/cutoff: pattern scale/root (OSC1/OSC2/filter)")
    print("  X   X-envelope: exponential output curve, original stage timing")
    print("  H   X-Mod type 4 : %s" %
          ("FM ratio, %d exact detents%s" %
           (hdr.ratio_count,
            ", against live VCO1 pitch" if args.xmod_follow_osc1
            else ", against the played note, free of VCO1")
           if args.xmod else "disabled (--xmod to enable)"))
    print("  G1  synced LFO   : %s" %
          ("rate tracks the transport, index clamped to the table"
           if args.lfo else "disabled (--lfo to enable)"))
    print("  H2  modulator sine : %s" %
          ("degree-7 polynomial, THD 0.00005 %"
           if args.sine else "stock parabola (--sine to replace)"))
    print("  H3  saw/square/tri : %s" %
          ("band limited, -16/-16/-4 dB alias"
           if args.sine else "stock naive (--sine to replace)"))
    print("  J1  MIDI DIN out  : %s" %
          ("USART TXE interrupt, one byte at wire cadence"
           if args.midi else "stock, one blind byte a tick"))
    print("  J2  MIDI DIN in   : %s" %
          ("framing errors dropped, counted at 0x%08X" % (hdr.diag + 0x3C)
           if args.midi else "stock, garbled bytes parsed as valid"))
    print("  K   audio priority : %s" %
          ("EXTI2 raised to 1, above SysTick"
           if args.audio_priority else "stock (5, below MIDI and USB)"))
    print("  K2  QSPI priority  : 15 (stock 16 was masked to priority 0)")
    print("  O   DSP compile    : -%s (build -Os with --dsp-opt Os for A/B)" %
          args.dsp_opt)
    print("  C   instruction cache enabled at start-up")
    print("  A2  watchdog       : %s" %
          ("%.1f s, armed on the first main-loop iteration" % actual
           if args.watchdog else "disabled (--watchdog to enable)"))
    print("  E1  boot self test : %s" %
          ("CRC32 0x%08X over 0x%X bytes" %
           (struct.unpack_from("<I", image, blob_off + Header.OFF_CRC)[0], STOCK_SIZE)
           if args.crc else "disabled (--crc to enable)"))
    print()
    print("sites rewritten:")
    for addr, what, old, new in log:
        print("  0x%08X  %-46s %s -> %s" % (addr, what, old, new))
    print()
    print("Hold [TEMPO] + [FUNC] and power on to enter the bootloader, then")
    print("send %s over USB with a 1 ms delay between messages." % out_syx)
    print("The secondary bootloader stays intact, so a bad image is recoverable")
    print("by flashing the stock file the same way.")


def verify_sites(image, log, hdr):
    """
    Decode every rewritten instruction back out of the finished image and
    check it is the branch we meant to write.  Hand-encoding Thumb branches
    is exactly the kind of thing that silently produces a working-looking
    image and a bricked synth.
    """
    from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_LITTLE_ENDIAN
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)

    expect = {
        0x08119678: ("b", hdr.sbrk & ~1),
        0x081196A0: ("b", hdr.malloc_lock & ~1),
        0x081196B8: ("b", hdr.malloc_unlock & ~1),
        0x0815906C: ("b", hdr.delete & ~1),
        0x08159070: ("b", hdr.delete & ~1),
        0x081591D4: ("b", hdr.new & ~1),
        0x08119720: ("bl", hdr.usb_defer & ~1),
        0x08156022: ("bl", hdr.early & ~1),
        0x0815602A: ("bl", hdr.late & ~1),
        0x08119A02: ("bl", hdr.wdg_hook & ~1),
    }
    if any(a == 0x08155C7C for a, _, _, _ in log):
        expect[0x08155C7C] = ("b", hdr.ui_switch & ~1)
        expect[0x081504FA] = ("bl", hdr.ui_begin_unlink & ~1)
        expect[0x0814A734] = ("b", hdr.ui_consume_modifier & ~1)
        for address, target in zip(
                (0x0814A314, 0x0814A33C, 0x0814A358, 0x0814A374, 0x0814A390, 0x0814A3AC),
                hdr.ui_helpers):
            expect[address] = ("b", target & ~1)
    if any(a == 0x0811CC70 for a, _, _, _ in log):
        expect[0x0811CC70] = ("b", hdr.mod_tick & ~1)
    if any(a == 0x0811BD48 for a, _, _, _ in log):
        expect[0x0811BD48] = ("b", hdr.xmod_time & ~1)
        expect[0x0811BBA8] = ("b", hdr.xmod_speed & ~1)
        expect[0x08112C88] = ("b", hdr.xmod_pos & ~1)
    if any(a == 0x0811AC90 for a, _, _, _ in log):
        expect[0x0811AC90] = ("b", hdr.midi_tx & ~1)
        expect[0x0810EEA0] = ("b", hdr.midi_rx & ~1)
    for addr, (mnem, want_target) in expect.items():
        off = addr - BASE
        ins = next(md.disasm(bytes(image[off:off + 4]), addr, 1), None)
        if ins is None or ins.mnemonic not in (mnem, mnem + ".w"):
            raise SystemExit("0x%08X: expected %s, decoded %s"
                             % (addr, mnem,
                                "nothing" if ins is None
                                else ins.mnemonic + " " + ins.op_str))
        target = int(ins.op_str.strip("# "), 0)
        if not BLOB_BASE <= target < BLOB_BASE + BLOB_WINDOW:
            raise SystemExit("0x%08X: %s %s does not reach the patch blob"
                             % (addr, ins.mnemonic, ins.op_str))
        if target != want_target:
            raise SystemExit("0x%08X: %s reaches 0x%08X, expected 0x%08X"
                             % (addr, ins.mnemonic, target, want_target))

    # Type 3 is selected by local parameter 7 in three separate UI actions.
    # Verify the complete conditional pairs, not merely a byte that happens
    # to contain the literal 4: the waveform arm must remain at 5 and local
    # 6 must remain untouched.
    # Each of the three UI cycle sites is one `itete` carrying *both* counts:
    # the waveform arm and the X-Mod type arm, plus the two local indices.
    # Verify the whole pair at each site, so a poke that landed on the wrong
    # arm fails the build instead of silently shortening the other list.
    if any(a == 0x08150102 for a, _, _, _ in log):
        w = "05"                    # the waveform list is stock
        for addr, want in (
                (0x081500FE, bytes.fromhex("15bf" + w + "27042706260726")),
                (0x08150550,
                 bytes.fromhex("15bf4ff0" + w + "084ff0040806270727")),
                (0x081506EA,
                 bytes.fromhex("15bf0423" + w + "234ff007094ff00609"))):
            got = bytes(image[addr - BASE:addr - BASE + len(want)])
            if got != want:
                raise SystemExit("0x%08X: TYPE/WAVE UI cycle is %s, "
                                 "expected %s"
                                 % (addr, got.hex(" "), want.hex(" ")))

    # the X-Mod pitch literals must still read 126, i.e. the withdrawn G3 is
    # really gone.  See the note at the G3 slot: the divide undoes the
    # multiply by 126 at 0x08112810, so 127 there would be the bug.
    for addr, want in ((0x0811BD2C, 1.0 / 126.0), (0x0811BF14, 1.0 / 126.0),
                       (0x0811BD40, 126.0), (0x0811BF20, 126.0)):
        got = struct.unpack_from("<f", image, addr - BASE)[0]
        if abs(got - want) > 1e-9:
            raise SystemExit("0x%08X: literal is %r, expected %r"
                             % (addr, got, want))

    # fault and profiled vector entries must be odd (Thumb), in the blob
    for slot in (3, 4, 5, 6, 15, 24, 27, 66):
        v = struct.unpack_from("<I", image, 4 * slot)[0]
        if not (v & 1) or not BLOB_BASE <= (v & ~1) < BLOB_BASE + BLOB_WINDOW:
            raise SystemExit("vector %d = 0x%08X is not a Thumb address in "
                             "the patch blob" % (slot, v))


DIAG_FIELDS = [
    ("magic", "0x%08X"), ("fault", "%d"), ("sp", "0x%08X"), ("pc", "0x%08X"),
    ("lr", "0x%08X"), ("xpsr", "0x%08X"), ("cfsr", "0x%08X"), ("hfsr", "0x%08X"),
    ("faults", "%d"), ("crc_expected", "0x%08X"), ("crc_actual", "0x%08X"),
    ("boots", "%d"), ("watchdog_armed", "%d"), ("heap_max", "0x%08X"),
    ("last_lfo_rate", "0x%08X"), ("midi_rx_errors", "0x%08X"),
]
FAULTS = {1: "HardFault", 2: "MemManage", 3: "BusFault", 4: "UsageFault",
          5: "fixed-pool exhaustion"}


def diag(args):
    if os.path.isfile(args.input):
        raw = open(args.input, "rb").read()
    else:
        raw = bytes.fromhex(args.input.replace(" ", "").replace("\n", ""))
    if len(raw) < 0x40:
        raise SystemExit("diagnostic dump is %d bytes; need at least 64" % len(raw))
    words = struct.unpack_from("<16I", raw)
    if words[0] != 0x4E4F5246:
        print("no 'NORF' magic -- not a diagnostic block (or never written)")
        return
    for (name, fmt), v in zip(DIAG_FIELDS, words):
        extra = ""
        if name == "fault":
            extra = "  (%s)" % FAULTS.get(v, "none")
        if name == "heap_max" and v:
            extra = "  (%d of 245760 bytes used at peak)" % (v - 0x30000000)
        print("  %-14s " % name + fmt % v + extra)

    if len(raw) < 0x130:
        print("  (legacy/short dump: read 304 bytes for profiler data)")
        return

    ext = {
        "format": 0x40, "core_hz": 0x44, "dwt_ready": 0x48,
        "sbrk_failures": 0x4c, "malloc_irqoff_max": 0x50,
        "midi_queue_high": 0x58, "midi_queue_full": 0x5c,
        "midi_tx_bytes": 0x60, "pool_small_used": 0x64,
        "pool_small_high": 0x68, "pool_big_used": 0x6c,
        "pool_big_high": 0x70, "pool_failures": 0x74,
        "pool_bad_free": 0x78, "malloc_failures": 0x7c,
        "heap_limit": 0x80, "heap_remaining": 0x84,
        "largest_free": 0x88, "usb_deferred": 0x94,
        "usb_coalesced": 0x98, "audio_deadline": 0x9c,
        "dsp_opt": 0xa8, "event_pool_ready": 0xac,
        "event_pool_fallback": 0xb0,
    }
    core_hz = struct.unpack_from("<I", raw, ext["core_hz"])[0] or 400000000
    print("  telemetry:")
    for name, offset in ext.items():
        v = struct.unpack_from("<I", raw, offset)[0]
        extra = ""
        if name in ("malloc_irqoff_max", "audio_deadline"):
            extra = "  (%.3f us)" % (v * 1e6 / core_hz)
        elif name == "dsp_opt":
            extra = "  (-O3)" if v == 3 else "  (-Os)" if v == 1 else ""
        print("    %-20s %u%s" % (name, v, extra))

    print("  DWT profiles:")
    for i, name in enumerate(("audio", "SysTick", "TIM5_seq",
                              "DMA1_Stream0", "main_period")):
        count, lo, hi, maximum, overruns, last = struct.unpack_from(
            "<6I", raw, 0xb8 + i * 24)
        total = lo | (hi << 32)
        average = total / count if count else 0.0
        print("    %-12s count=%u avg=%.1f (%.3f us) max=%u (%.3f us) "
              "last=%u overruns=%u" %
              (name, count, average, average * 1e6 / core_hz,
               maximum, maximum * 1e6 / core_hz, last, overruns))

    if len(raw) >= 0x940:
        rsr, idx = struct.unpack_from("<II", raw, 0x420)
        hist = struct.unpack_from("<4I", raw, 0x428)
        print("  retained: reset cause RCC_RSR=0x%08X (%s), PC samples=%u, "
              "thread/audio/other=%u/%u/%u" % (
                  rsr, ", ".join(n for b, n in RSR_BITS if rsr & (1 << b)) or "none",
                  idx, hist[0], hist[1], hist[3]))
        ring = struct.unpack_from("<256I", raw, 0x540)
        recent = [ring[(idx + i) & 0xFF] for i in range(256)][-16:]
        print("  last PC samples (oldest first): " + " ".join(
            "%06X/%u" % (w & 0xFFFFFF, w >> 24) for w in recent))
    if len(raw) >= 0xD50:
        delayed, _, started = struct.unpack_from("<3I", raw, 0xD44)
        print("  LED SPI: %u frames started, %u held back by the frame guard%s"
              % (started, delayed,
                 "" if delayed == 0 else "  (two SysTicks landed inside one frame)"))


RSR_BITS = ((17, "CPU"), (19, "D1"), (20, "D2"), (21, "BOR"), (22, "PIN"),
            (23, "POR"), (24, "SFT"), (26, "IWDG1"), (28, "WWDG1"), (30, "LPWR"))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build")
    b.add_argument("--no-watchdog", dest="watchdog", action="store_false",
                   help="leave the IWDG off")
    b.add_argument("--watchdog", dest="watchdog", action="store_true")
    b.add_argument("--wdg-timeout", type=float, default=16.0,
                   help="watchdog period in seconds (default 16)")
    b.add_argument("--crc", action="store_true",
                   help="verify a CRC32 of the image at boot (adds ~30 ms)")
    b.add_argument("--no-lfo", dest="lfo", action="store_false",
                   help="leave the stock synced-LFO behaviour alone")
    b.add_argument("--lfo", dest="lfo", action="store_true")
    b.add_argument("--no-xmod", dest="xmod", action="store_false",
                   help="leave the X-Mod type count at three")
    b.add_argument("--xmod", dest="xmod", action="store_true")
    b.add_argument("--xmod-follow-osc1", dest="xmod_follow_osc1",
                   action="store_true",
                   help="X-Mod type 3: hang the ratio on VCO1's live pitch, "
                        "as the stock audio-rate path does, instead of on "
                        "the played note. Right when VCO1 is the carrier, "
                        "wrong when it is silenced and VCO2 carries.")
    b.add_argument("--exti2-priority", type=int, choices=(1, 2, 5), default=None,
                   help="CV engine NVIC preemption priority: 1 above SysTick "
                        "(default), 2 equal to it so neither preempts the "
                        "other, 5 stock")
    b.add_argument("--audio-priority", dest="audio_priority",
                   action="store_true",
                   help="raise the CV engine above SysTick, so MIDI and USB "
                        "stop preempting audio")
    b.add_argument("--no-audio-priority", dest="audio_priority",
                   action="store_false",
                   help="leave the stock EXTI2 priority 5")
    b.add_argument("--dsp-opt", choices=("O3", "Os"), default="O3",
                   help="optimisation for the hot DSP/LFO object (default O3)")
    b.add_argument("--no-midi", dest="midi", action="store_false",
                   help="leave the stock MIDI DIN transmitter alone")
    b.add_argument("--midi", dest="midi", action="store_true")
    b.add_argument("--no-ui-chords", dest="ui_chords", action="store_false",
                   help="leave the stock UI button-state helpers (A/B for UI hangs)")
    b.add_argument("--ui-chords", dest="ui_chords", action="store_true")
    b.add_argument("--no-sqrt-memo", dest="sqrt_memo", action="store_false", default=True,
                   help="disable the Y4 sqrt-lookup memo (diagnostic)")
    b.add_argument("--no-sine", dest="sine", action="store_false",
                   help="leave the stock oscillator waveforms alone")
    b.add_argument("--sine", dest="sine", action="store_true")
    b.add_argument("--no-seq-sort", dest="seq_sort", action="store_false", default=True,
                   help="leave the pattern store re-sorting every step on every pass (A/B)")
    b.add_argument("--no-seq-idle", dest="seq_idle", action="store_false", default=True,
                   help="leave main() walking the whole pattern store when idle (A/B)")
    b.add_argument("--no-morph-trig-fix", dest="morph_trig_fix", action="store_false", default=True,
                   help="let the morph pattern's unlinked X-Env trigs fire at morph 0 (stock)")
    b.add_argument("--telemetry", dest="telemetry", action="store_true", default=False,
                   help="MIDI CC telemetry on channel 16 (boot record, PC ring, heartbeat, "
                        "snapshots; diagnostic, ~4.5 KB; off by default since p54)")
    b.add_argument("--ev-log", dest="ev_log", action="store_true", default=False,
                   help="X-Env event log (EV/PM telemetry records, diagnostic; off by default)")
    b.add_argument("--seq-dim", type=int, default=2, choices=range(0, 16), metavar="0..15",
                   help="brightness of the dim SEQ-key state (stock 5; bright is 16)")
    b.add_argument("--no-qspi-defer", dest="qspi_defer", action="store_false", default=True,
                   help="let a flash-touching paste run while a save/load is in flight (stock)")
    b.add_argument("--no-midi-flow", dest="midi_flow", action="store_false", default=True,
                   help="stock MIDI out (USB drops past 64, DIN drops mid-message) and USB-in ring")
    b.add_argument("--no-note-snap", dest="note_snap", action="store_false", default=True,
                   help="a note start glides 4 ms from the previous note's parameters (stock)")
    b.add_argument("--no-fast-replay", dest="fast_replay", action="store_false", default=True,
                   help="mod-note parameters through one heap event each (stock)")
    b.add_argument("--choke-ms", type=float, default=1.0,
                   help="fade before an instrument switch, ms (0 = off, the stock's continue)")
    b.add_argument("--no-polyat-fix", dest="polyat_fix", action="store_false", default=True,
                   help="poly aftertouch takes the key number as its pressure (stock)")
    b.add_argument("--no-smart-random", dest="smart_random", action="store_false", default=True,
                   help="TEMPO+FUNC+note randomizes a mod note's knobs independently (stock)")
    b.add_argument("--no-black-keys", dest="black_keys", action="store_false", default=True,
                   help="black keys do nothing in mod-note mode (stock)")
    b.add_argument("--no-kit", dest="kit", action="store_false", default=True,
                   help="TEMPO+FUNC+C#/D#/A# (drum kit, bass kit, every mod note) do nothing in mod-note mode (stock)")
    b.add_argument("--no-patch-random", dest="patch_random", action="store_false", default=True,
                   help="TEMPO+FUNC+note only plays the note in normal mode (stock)")
    b.add_argument("--no-sync-load", dest="sync_load", action="store_false", default=True,
                   help="a project loaded while playing takes over at once, mid-pattern (stock)")
    b.add_argument("--no-clear-fix", dest="clear_fix", action="store_false", default=True,
                   help="DELETE+RECORD also resets mod note 0 (layer 1: mod note 1) (stock)")
    b.add_argument("--no-journal-fix", dest="journal_fix", action="store_false", default=True,
                   help="a project slot cut short by a power loss hangs its load, at power-on for slot 0 (stock)")
    b.add_argument("--led-level", type=int, default=1, choices=range(1, 6), metavar="1-5",
                   help="LED preset at power-on: 1 every lit LED at the lowest step (playhead and "
                        "tempo blink brighter), 2-4 brighter with dim/normal/bright apart, "
                        "5 the stock levels (default 1); TEMPO+FOLLOW steps through them")
    b.add_argument("--no-rotate-guard", dest="rotate_guard", action="store_false", default=True,
                   help="FUNC+UP/DOWN on a pattern of length 0 corrupts memory (stock)")
    b.add_argument("--no-boot-fallback", dest="boot_fallback", action="store_false", default=True,
                   help="slot 0 failing its checks at power-on stops on the error screen (stock)")
    b.set_defaults(watchdog=True, lfo=True, xmod=True, sine=True, ui_chords=True,
                   midi=True, xmod_follow_osc1=False,
                   audio_priority=False, func=build)   # K off by default since 2026-09-17:
                                                       # every freeze needed it, P and J1 cover its purpose

    d = sub.add_parser("diag")
    d.add_argument("input", help="2368-byte binary dump of the block, or hexadecimal text"
                                 " (the MIDI telemetry is decoded by tools/diagmidi.py)")
    d.set_defaults(func=diag)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
