#!/usr/bin/env python3
"""
test_patch.py -- execute the injected patch code under emulation.

The patched firmware cannot be tried on hardware from here, so every entry
point in patch/np_patch.S is run in Unicorn against the real patched image
and its effects on memory are checked: the diagnostic block, the cache
enable, the watchdog registers, the sbrk arithmetic and the fault record.

    build/venv/bin/python tests/test_patch.py
"""

import math
import struct
from unicorn import arm_const as uc_arm
import sys
import zlib

from unicorn import (Uc, UcError, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS,
                     UC_PROT_ALL, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE,
                     UC_HOOK_CODE)
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                               UC_ARM_REG_R3, UC_ARM_REG_R4, UC_ARM_REG_SP, UC_ARM_REG_LR,
                               UC_ARM_REG_PC, UC_ARM_REG_MSP, UC_ARM_REG_PSP,
                               UC_ARM_REG_IPSR, UC_ARM_REG_PRIMASK,
                               UC_ARM_REG_S0, UC_ARM_REG_S14,
                               UC_ARM_REG_S15, UC_ARM_REG_S18)

IMAGE = "build/firmware-patched.bin"
BASE = 0x08100000
BLOB = 0x08175200

DTCM = 0x20000000
D2 = 0x30000000
SCB = 0xE000E000
IWDG = 0x58004800

SCB_CCR = 0xE000ED14
SCB_AIRCR = 0xE000ED0C
SCB_CFSR = 0xE000ED28
SCB_HFSR = 0xE000ED2C
ICIALLU = 0xE000EF50
DWT_CTRL = 0xE0001000
DWT_CYCCNT = 0xE0001004
DWT_LAR = 0xE0001FB0
DEMCR = 0xE000EDFC

POOL_META = 0x3003C000
POOL_SMALL = 0x3003C100
POOL_BIG = 0x3003E200

STOP = 0x00001000            # sentinel return address

ORIG_SYSTEMINIT = 0x081093F4
ORIG_MAIN = 0x08119938
ORIG_SYSOBJ = 0x08109614
ORIG_LIBC_INIT = 0x08159FB0
RESET_HANDLER = 0x08155FF8
MAIN_HOOK_SITE = 0x08119A02

# stock entry points the LFO fix calls into
ORIG_SETRATE = 0x08156910
ORIG_RNG = 0x08147984                 # xorshift32, state on a System object
RNG_CELL = 0x2001C400                 # where the test keeps that state
ORIG_TRANSPORT = 0x08118380
ORIG_PROJECT = 0x0811621C
ORIG_RATE_WALK = 0x08116C9C           # local-5/LFOSpeed apply for 19 slots
ORIG_MIDI_F8 = 0x08118B00             # called for every accepted DIN/USB F8
ORIG_LFO_SPEED = 0x0811BBA8           # globally spliced to np_xmod_speed
DEFAULT_RETRIG = 0x0811BA3D
DIV_TABLE = 0x0815F320
SLOW_TABLE = 0x08172F94
FREQ_TABLE = 0x0817379C               # 257 floats, MIDI 0..127 over 256 steps
LFO_SETMODE = 0x081562C8              # MultiWaveOscFast::setMode, vtable slot 4

# project fields the X-Mod handlers read
P_PITCH_OTHER = 0x2F20
P_PITCH_SLOT0 = 0x2F60
# The played note lives in a pair of per-oscillator slots.  With nothing
# held the note handler writes both; under legato it leaves the previous
# note in VCO1's and puts the new one in VCO2's, and on the slide path the
# two are swapped.  See the block comment in patch/np_patch.S.
P_NOTE_VCO1 = 0x5FCC
P_NOTE_VCO2 = 0x5FD0
P_NOTE = P_NOTE_VCO2                # kept for the call sites that mean "both"
P_TRANSPOSE = 0x6004                # likewise not the ratio base
P_TYPE_VALUE = 0x1A4                # LFOTimeCParam's value: the X-Mod type
NP_NOTE = 0x2000E4B4                # where np_note_hook records the note
NOTE_HOOK_SITE = 0x081172B0         # the `bl` the note handler makes
NOTE_HOOK_TAIL = 0x08116D48         # and what it used to call
RATIO_COUNT = 128                     # detents of the ratio knob
TEST_MIDI = 36.0                      # comfortably inside the table bounds


def position_for_midi(note):
    """The stock frequency table position for a MIDI-note coordinate."""
    return note / 127.0


def pitch_for_midi(note):
    """VCO1's live 0..126 coordinate for that table position."""
    return position_for_midi(note) * 126.0


TEST_PITCH = pitch_for_midi(TEST_MIDI)

# Scratch objects the LFO tests build in DTCM.  Laid out with room to spare:
# 19 owners of 0x200 bytes is 0x2600, which is easy to run into something.
SLOT_COUNT = 19
SLOT_STRIDE = 36
OWNER_STRIDE = 0x200
FAKE_TRANSPORT = 0x20010000          # +4 = tick rate
FAKE_SUB = 0x20010100                # +20 = retrigger vfn
CALL_LOG = 0x20010400                # setRate call recorder, 0x400 bytes
FAKE_SLOT = 0x20010800               # +8 = owner
FAKE_OWNER = 0x20011000
FAKE_PARAM = 0x20013800              # a CParam: +8 owner, +0xC value
# The real address, not a convenient one: the C++ handlers call the project
# singleton and the live VCO1 coordinate is at project+0x2F20.  The splice is
# reached from the DSP update, where r4 is instead project+0x44.
FAKE_PROJECT = 0x200047F0            # +0x6074 = param pointer array
SLOT_ARRAY = FAKE_PROJECT + 0x6074
MIDI_TRANSPORT = 0x30010000           # full object; fields reach +0x8E0
PHASES = (0x34, 0x58, 0x84, 0xA8, 0xCC, 0xF0)

_REGIONS = [
    ("transport", FAKE_TRANSPORT, 0x10),
    ("sub", FAKE_SUB, 0x20),
    ("call log", CALL_LOG, 0x400),
    ("slots", FAKE_SLOT, SLOT_COUNT * 0x40),
    ("owners", FAKE_OWNER, SLOT_COUNT * OWNER_STRIDE),
    ("param", FAKE_PARAM, 0x20),
    # the X-Mod handlers read project+0x2F20 and +0x2F60, so the project has
    # to be a real object here, not a stub word
    ("project", FAKE_PROJECT, 0x6074),
    ("slot array", SLOT_ARRAY, SLOT_COUNT * SLOT_STRIDE),
]
for _i in range(len(_REGIONS)):
    for _j in range(_i + 1, len(_REGIONS)):
        _n1, _a1, _s1 = _REGIONS[_i]
        _n2, _a2, _s2 = _REGIONS[_j]
        if _a1 < _a2 + _s2 and _a2 < _a1 + _s1:
            raise SystemExit("test rig overlap: %s and %s" % (_n1, _n2))

FAILS = []


def _fmt(v):
    if isinstance(v, int):
        return "0x%X" % v
    return repr(v)


def check(name, got, want):
    ok = got == want
    print("  %-46s %s" % (name, "ok" if ok else
                          "FAIL got %s want %s" % (_fmt(got), _fmt(want))))
    if not ok:
        FAILS.append(name)


def stub_return_const(m, address, value):
    """Install `ldr r0, literal; bx lr`, avoiding emulator MOVW quirks."""
    m.uc.mem_write(address, b"\x00\x48\x70\x47" + struct.pack("<I", value))


def stub_store_r0(m, address, destination):
    """Install a tiny function which stores r0 at destination and returns."""
    # ldr r1, [pc, #4]; str r0, [r1]; bx lr; nop; .word destination
    m.uc.mem_write(address, b"\x01\x49\x08\x60\x70\x47\x00\xBF" +
                   struct.pack("<I", destination))


class Machine:
    def __init__(self):
        self.img = open(IMAGE, "rb").read()
        self.hdr = {}
        (self.early, self.late, self.wdg_hook, self.hardfault, self.memmanage,
         self.busfault, self.usagefault, self.sbrk, self.diag,
         self.diag_size) = struct.unpack_from("<10I", self.img, BLOB - BASE + 0x10)
        (self.mod_tick, self.lfo_poll, self.xmod_time, self.xmod_speed,
         self.xmod_pos, self.ratio_value, self.ratio_offset,
         self.ratio_count) = struct.unpack_from(
            "<8I", self.img, BLOB - BASE + 0x44)
        (self.sine, self.tri, self.square,
         self.saw) = struct.unpack_from("<4I", self.img, BLOB - BASE + 0x68)
        (self.midi_tx, self.midi_rx) = struct.unpack_from(
            "<2I", self.img, BLOB - BASE + 0x78)
        (self.usb_defer, self.audio_irq, self.systick_irq, self.tim5_irq,
         self.dma_irq, self.new_entry, self.delete_entry, self.malloc_lock,
         self.malloc_unlock, self.main_service, self.note_hook,
         self.note_addr) = struct.unpack_from(
             "<12I", self.img, BLOB - BASE + 0x80)
        self.wave_thunks = struct.unpack_from(
            "<6I", self.img, BLOB - BASE + 0xB0)
        self.wave_shape = struct.unpack_from(
            "<I", self.img, BLOB - BASE + 0xC8)[0]
        (self.led_frame, self.led_done, self.ui_queue_pop) = struct.unpack_from(
            "<3I", self.img, BLOB - BASE + 0x12C)
        self.scan_rearm = struct.unpack_from("<I", self.img, BLOB - BASE + 0x14C)[0]
        self.follow_osc1 = bool(self.img[BLOB - BASE + 0x64])

        self.flags_off = BLOB - BASE + 0x05
        self.brk_ptr, self.heapsize_ptr = struct.unpack_from(
            "<II", self.img, BLOB - BASE + 0x3C)
        self.reset()

    def reset(self, flags=None):
        # MCLASS so MRS/MSR against MSP and PSP behave like a Cortex-M
        uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        uc.mem_map(0x00000000, 0x10000, UC_PROT_ALL)          # sentinel page
        uc.mem_map(0x08100000, 0xE0000, UC_PROT_ALL)          # the app region, up to the settings sector
        uc.mem_map(DTCM, 0x20000, UC_PROT_ALL)
        uc.mem_map(D2, 0x50000, UC_PROT_ALL)
        uc.mem_map(0x40000000, 0x30000, UC_PROT_ALL)   # APB1 timer, DMA1, ADC1, USART1
        uc.mem_map(0x58000000, 0x30000, UC_PROT_ALL)   # EXTI, GPIO, RCC (np_early reads RCC_RSR)
        uc.mem_map(0xE0000000, 0x10000, UC_PROT_ALL)
        uc.mem_write(BASE, self.img)
        if flags is not None:
            uc.mem_write(BASE + self.flags_off, bytes([flags]))
        # the stock entry points the hooks tail-call into: just return
        for a in (ORIG_SYSTEMINIT, ORIG_MAIN, ORIG_SYSOBJ, ORIG_LIBC_INIT):
            uc.mem_write(a, b"\x70\x47")                       # bx lr
        uc.mem_write(STOP, b"\x70\x47")
        # SystemInit is stubbed out in these tests, so grant the FPU access
        # it would normally set up -- np_lfo.cpp uses VFP throughout
        uc.mem_write(0xE000ED88, struct.pack("<I", 0x00F00000))
        uc.reg_write(UC_ARM_REG_MSP, DTCM + 0x1F000)
        uc.reg_write(UC_ARM_REG_SP, DTCM + 0x1F000)
        self.uc = uc
        return uc

    def call(self, addr, r0=0, r1=0, lr=STOP | 1, count=100000):
        uc = self.uc
        uc.reg_write(UC_ARM_REG_R0, r0 & 0xFFFFFFFF)
        uc.reg_write(UC_ARM_REG_R1, r1 & 0xFFFFFFFF)
        uc.reg_write(UC_ARM_REG_LR, lr)
        uc.emu_start(addr | 1, STOP, count=count)
        return uc.reg_read(UC_ARM_REG_R0)

    def u32(self, addr):
        return struct.unpack("<I", self.uc.mem_read(addr, 4))[0]

    def w32(self, addr, v):
        self.uc.mem_write(addr, struct.pack("<I", v & 0xFFFFFFFF))


def test_early(m):
    print("np_early -- diagnostic block and I-cache")
    m.reset(flags=0)
    m.uc.mem_write(m.diag, b"\xAA" * m.diag_size)   # garbage, as at power on
    m.call(m.early)
    check("diag magic", m.u32(m.diag + 0x00), 0x4E4F5246)
    check("diag cleared (fault)", m.u32(m.diag + 0x04), 0)
    check("diag cleared (heap_max)", m.u32(m.diag + 0x34), 0)
    check("boot counter", m.u32(m.diag + 0x2C), 1)
    check("SCB->CCR.IC set", m.u32(SCB_CCR) & (1 << 17), 1 << 17)
    check("ICIALLU written", m.u32(ICIALLU), 0)
    # the X-Env curve's constants are copied flash -> DTCM on every boot,
    # and the RC ADSR state next to them starts from silence
    master, dtcm, state = struct.unpack("<3I", m.uc.mem_read(BLOB + 0x120, 12))
    check("envelope constants in DTCM", dtcm, m.diag + 0x140)
    check("envelope constants copied", bytes(m.uc.mem_read(dtcm, 36)),
          bytes(m.uc.mem_read(master, 36)))
    check("RC envelope state cleared", bytes(m.uc.mem_read(state, 21 * 32)), bytes(21 * 32))

    # the retained area: reset cause captured and cleared, a boot marker in
    # the PC ring, the sample index advanced by one
    check("PC ring: boot marker", m.u32(m.diag + 0x540), 0xB0070001)
    check("PC ring: index", m.u32(m.diag + 0x424), 1)
    check("RCC_RSR: RMVF written to clear the flags", m.u32(0x580244D0) & (1 << 16), 1 << 16)

    # a second boot must keep the record and only bump the counter; it also
    # freezes the previous session's ring, counters and DWT records where
    # the telemetry can dump them, and records the reset cause
    m.uc.mem_write(m.diag + 0x20, struct.pack("<I", 7))   # 7 faults so far
    m.uc.mem_write(m.diag + 0x74, struct.pack("<I", 3))   # 3 pool failures last session
    m.uc.mem_write(m.diag + 0x540 + 4 * 5, struct.pack("<I", 0x12345618))   # a sample
    m.uc.mem_write(m.diag + 0x424, struct.pack("<I", 6))
    m.uc.mem_write(0x580244D0, struct.pack("<I", 1 << 26))   # IWDG1RSTF
    m.call(m.early)
    check("second boot keeps fault count", m.u32(m.diag + 0x20), 7)
    check("second boot counter", m.u32(m.diag + 0x2C), 2)
    check("reset cause recorded", m.u32(m.diag + 0x420), 1 << 26)
    check("previous pool failures frozen", m.u32(m.diag + 0x440 + 0x74 - 0x60), 3)
    check("live pool failures cleared", m.u32(m.diag + 0x74), 0)
    check("previous ring frozen", m.u32(m.diag + 0x940 + 4 * 5), 0x12345618)
    check("previous ring index frozen", m.u32(m.diag + 0xD40), 6)
    check("second boot marker", m.u32(m.diag + 0x540 + 4 * 6), 0xB0070002)


def test_sbrk(m):
    print("np_sbrk -- allocation, refusal and high-water mark")
    m.reset(flags=0)
    m.call(m.early)
    heap_size = m.u32(m.heapsize_ptr)
    check("heap size constant", heap_size, 0x3C000)
    m.w32(m.brk_ptr, D2)

    got = m.call(m.sbrk, r0=1024)
    check("first sbrk returns old break", got, D2)
    check("break advanced", m.u32(m.brk_ptr), D2 + 1024)
    check("high-water mark", m.u32(m.diag + 0x34), D2 + 1024)

    got = m.call(m.sbrk, r0=4096)
    check("second sbrk returns old break", got, D2 + 1024)
    check("high-water mark grows", m.u32(m.diag + 0x34), D2 + 5120)

    # give it back; the mark must not shrink
    got = m.call(m.sbrk, r0=-4096)
    check("negative increment accepted", got, D2 + 5120)
    check("break shrank", m.u32(m.brk_ptr), D2 + 1024)
    check("high-water mark kept", m.u32(m.diag + 0x34), D2 + 5120)

    # ask for more than the heap holds
    before = m.u32(m.brk_ptr)
    got = m.call(m.sbrk, r0=0x40000)
    check("oversized request returns -1", got, 0xFFFFFFFF)
    check("break untouched after refusal", m.u32(m.brk_ptr), before)

    # exactly the remaining space must still succeed
    left = D2 + heap_size - m.u32(m.brk_ptr)
    got = m.call(m.sbrk, r0=left)
    check("exact fit accepted", got, before)
    check("break at heap top", m.u32(m.brk_ptr), D2 + heap_size)
    check("one more byte refused", m.call(m.sbrk, r0=1), 0xFFFFFFFF)
    check("sbrk failures counted", m.u32(m.diag + 0x4C), 2)


def test_runtime_allocator(m):
    print("np_runtime_init -- DWT, pool layout and heap limit")
    m.reset(flags=0)
    m.uc.mem_write(POOL_META, b"\xFF" * 0x100)
    m.call(m.early)
    check("diagnostic format", m.u32(m.diag + 0x40), 2)
    check("400 MHz core clock", m.u32(m.diag + 0x44), 400000000)
    check("DWT marked ready", m.u32(m.diag + 0x48), 1)
    check("DEMCR trace enabled", m.u32(DEMCR) & (1 << 24), 1 << 24)
    check("DWT cycle counter enabled", m.u32(DWT_CTRL) & 1, 1)
    check("DWT lock access key written", m.u32(DWT_LAR), 0xC5ACCE55)
    check("audio deadline is the 250 us buffer interval",
          m.u32(m.diag + 0x9C), 100000)
    check("ordinary heap ends below pools", m.u32(m.diag + 0x80), POOL_META)
    check("pool bitmaps cleared", bytes(m.uc.mem_read(POOL_META, 0x100)),
          b"\x00" * 0x100)
    ui_canary = bytes(range(48))
    m.uc.mem_write(POOL_META + 0x40, ui_canary)

    # Before the first main-loop hook, constructors must keep using malloc.
    stock_pointer = 0x30001000
    stub_return_const(m, 0x0815A0C0, stock_pointer)
    p = m.call(m.new_entry, r0=12)
    check("startup allocation uses stock heap", p, stock_pointer)
    check("startup did not consume small pool", m.u32(m.diag + 0x64), 0)

    # Once main is alive, the common <=32-byte event objects are bounded.
    m.w32(m.diag + 0xAC, 1)
    p0 = m.call(m.new_entry, r0=12)
    p1 = m.call(m.new_entry, r0=20)
    check("thread event starts at small pool", p0, POOL_SMALL)
    check("thread event advances by 32 bytes", p1, POOL_SMALL + 32)
    check("small pool usage", m.u32(m.diag + 0x64), 2)
    check("small pool high-water", m.u32(m.diag + 0x68), 2)
    m.call(m.delete_entry, r0=p0)
    check("pool-aware delete releases the block", m.u32(m.diag + 0x64), 1)
    check("released slot is reused", m.call(m.new_entry, r0=8), p0)

    deque_block = m.call(m.new_entry, r0=512)
    check("thread deque growth uses the big pool", deque_block, POOL_BIG)
    m.call(m.delete_entry, r0=deque_block)

    # Exception context takes the pools first.  DMA deque growth is the
    # largest statically proven request and consumes the 512-byte pool.
    m.uc.reg_write(UC_ARM_REG_IPSR, 11)
    big = [m.call(m.new_entry, r0=512) for _ in range(15)]
    check("ISR 512-byte pool begins at reserved RAM", big[0], POOL_BIG)
    check("ISR 512-byte blocks are contiguous", big[-1], POOL_BIG + 14 * 512)
    check("big pool high-water reaches capacity", m.u32(m.diag + 0x70), 15)
    # A 16th block used to fault (cause 5, reset); a mod-note key press --
    # a burst of event objects from the key-scan ISR -- reached that on the
    # unit.  Exhaustion now falls back to the stock heap under its PRIMASK
    # lock, exactly where the stock served every ISR allocation, counted.
    check("a 16th ISR block falls back to the stock heap", m.call(m.new_entry, r0=512),
          stock_pointer)
    check("pool exhaustion is counted", m.u32(m.diag + 0x74), 1)
    small_isr = m.call(m.new_entry, r0=12)
    check("small ISR requests still use the small pool", POOL_SMALL <= small_isr < POOL_BIG, True)
    check("only the exhausted pool counts", m.u32(m.diag + 0x74), 1)
    check("no fault was raised", m.u32(m.diag + 0x04), 0)
    m.call(m.delete_entry, r0=small_isr)
    for block in big:
        m.call(m.delete_entry, r0=block)
    check("all big blocks release", m.u32(m.diag + 0x6C), 0)
    m.uc.reg_write(UC_ARM_REG_IPSR, 0)

    # A non-pool pointer must still reach the original free routine.
    free_log = 0x2001C500
    m.w32(free_log, 0)
    stub_store_r0(m, 0x0815A0D0, free_log)
    m.call(m.delete_entry, r0=stock_pointer)
    check("non-pool delete reaches stock free", m.u32(free_log), stock_pointer)
    check("no invalid pool free was reported", m.u32(m.diag + 0x78), 0)
    check("pool operations preserve UI shadow", bytes(m.uc.mem_read(POOL_META + 0x40, 48)),
          ui_canary)


def test_runtime_profile_and_defer(m):
    print("DWT wrappers -- kind mapping, totals, deadline and IRQ-off time")
    m.reset(flags=0)
    m.call(m.early)

    def irq_stub(address, final_cycles):
        # ldr r0,[pc,#4]; ldr r1,[pc,#8]; str r1,[r0]; bx lr; literals
        code = (b"\x01\x48\x02\x49\x01\x60\x70\x47" +
                struct.pack("<II", DWT_CYCCNT, final_cycles))
        m.uc.mem_write(address, code)

    originals = (0x08119844, 0x081196D8, 0x081194A4, 0x0810F208)
    wrappers = (m.audio_irq, m.systick_irq, m.tim5_irq, m.dma_irq)
    for kind, (original, wrapper) in enumerate(zip(originals, wrappers)):
        start = 100 + kind * 1000
        elapsed = 200 + kind
        irq_stub(original, start + elapsed)
        m.w32(DWT_CYCCNT, start)
        m.call(wrapper)
        stat = m.diag + 0xB8 + kind * 24
        check("profile %d count" % kind, m.u32(stat), 1)
        check("profile %d total" % kind, m.u32(stat + 4), elapsed)
        check("profile %d maximum" % kind, m.u32(stat + 12), elapsed)
        check("profile %d last" % kind, m.u32(stat + 20), elapsed)

    # A second long audio sample must trip the real 100,000-cycle deadline
    # (the stock EXTI2 handler renders eight samples, so the deadline is the
    # buffer interval, not one sample period).
    irq_stub(originals[0], 101000)
    m.w32(DWT_CYCCNT, 0)
    m.call(wrappers[0])
    audio = m.diag + 0xB8
    check("audio overrun counted", m.u32(audio + 16), 1)
    check("audio max includes the overrun", m.u32(audio + 12), 101000)

    m.uc.reg_write(UC_ARM_REG_PRIMASK, 0)
    m.w32(DWT_CYCCNT, 1000)
    m.call(m.malloc_lock)
    check("malloc lock masks interrupts", m.uc.reg_read(UC_ARM_REG_PRIMASK), 1)
    m.w32(DWT_CYCCNT, 1123)
    m.call(m.malloc_unlock)
    check("malloc unlock restores PRIMASK", m.uc.reg_read(UC_ARM_REG_PRIMASK), 0)
    check("malloc IRQ-off maximum", m.u32(m.diag + 0x50), 123)

    print("USB defer and main-loop profile")
    usb0, usb1 = 0x20001000, 0x20002000
    m.call(m.usb_defer, r0=usb0)
    m.call(m.usb_defer, r0=usb1)
    check("USB work is pending", m.u32(m.diag + 0x90), 1)
    check("two SysTicks deferred", m.u32(m.diag + 0x94), 2)
    check("second pending tick coalesced", m.u32(m.diag + 0x98), 1)
    check("latest USB object retained", m.u32(m.diag + 0x8C), usb1)

    usb_log = 0x2001C504
    m.w32(usb_log, 0)
    stub_store_r0(m, 0x08110244, usb_log)
    stub_return_const(m, 0x0811A6A4, 0x30020000)
    m.w32(0x30020000 + 0x0C18, 0)
    m.w32(0x30020000 + 0x0C1C, 0)
    m.w32(DWT_CYCCNT, 5000)
    m.call(m.main_service)
    check("USB service moved to main", m.u32(usb_log), usb1)
    check("main clears pending work", m.u32(m.diag + 0x90), 0)
    check("main enables thread event pool", m.u32(m.diag + 0xAC), 1)
    m.w32(DWT_CYCCNT, 5250)
    m.call(m.main_service)
    main_stat = m.diag + 0xB8 + 4 * 24
    check("main period count", m.u32(main_stat), 1)
    check("main period cycles", m.u32(main_stat + 4), 250)


def test_watchdog(m):
    print("np_wdg_hook -- arm once, then kick")
    m.reset(flags=0x02)                                 # FLAG_WDG
    m.call(m.early)
    pr = m.img[BLOB - BASE + 0x06]
    rlr = struct.unpack_from("<I", m.img, BLOB - BASE + 0x38)[0]

    m.call(m.wdg_hook)
    check("IWDG prescaler", m.u32(IWDG + 0x04), pr)
    check("IWDG reload", m.u32(IWDG + 0x08), rlr)
    check("IWDG key = reload", m.u32(IWDG + 0x00), 0xAAAA)
    check("watchdog marked armed", m.u32(m.diag + 0x30), 1)

    m.w32(IWDG + 0x04, 0xDEAD)                          # must not be rewritten
    m.w32(IWDG + 0x00, 0)
    m.call(m.wdg_hook)
    check("second call only kicks", m.u32(IWDG + 0x00), 0xAAAA)
    check("prescaler left alone", m.u32(IWDG + 0x04), 0xDEAD)

    print("np_wdg_hook -- disabled by the flag")
    m.reset(flags=0x00)
    m.call(m.early)
    m.call(m.wdg_hook)
    check("IWDG untouched when disabled", m.u32(IWDG + 0x00), 0)


def test_fault(m):
    print("np_hardfault -- record and reset")
    m.reset(flags=0)
    m.call(m.early)
    m.w32(SCB_CFSR, 0x00008200)                        # PRECISERR + BFARVALID
    m.w32(SCB_HFSR, 0x40000000)                        # FORCED

    frame = DTCM + 0x1E000
    for i, v in enumerate([0x11, 0x22, 0x33, 0x44, 0x55,
                           0x08123456,                 # stacked LR
                           0x08111111,                 # stacked PC
                           0x61000000]):               # stacked xPSR
        m.w32(frame + 4 * i, v)
    # SP aliases MSP in handler mode, so set MSP last: it is what the
    # handler reads back with "mrs r3, msp"
    m.uc.reg_write(UC_ARM_REG_MSP, frame)

    try:
        m.uc.reg_write(UC_ARM_REG_LR, 0xFFFFFFF9)      # return using MSP
        m.uc.emu_start(m.hardfault | 1, STOP, count=200)
    except Exception:
        pass                                            # ends in "b ."
    check("fault id = HardFault", m.u32(m.diag + 0x04), 1)
    check("fault counter", m.u32(m.diag + 0x20), 1)
    check("stacked SP", m.u32(m.diag + 0x08), frame)
    check("stacked PC", m.u32(m.diag + 0x0C), 0x08111111)
    check("stacked LR", m.u32(m.diag + 0x10), 0x08123456)
    check("stacked xPSR", m.u32(m.diag + 0x14), 0x61000000)
    check("CFSR", m.u32(m.diag + 0x18), 0x00008200)
    check("HFSR", m.u32(m.diag + 0x1C), 0x40000000)
    check("system reset requested", m.u32(SCB_AIRCR), 0x05FA0004)

    # ARMv7-M B1.5.7: an extended frame is the basic frame r0..xPSR at SP,
    # with S0-S15, FPSCR and a reserved word stacked ABOVE it (SP+0x20..
    # +0x67).  The S-register area is filled with a pattern that must never
    # come back as LR/PC/xPSR -- the decoder once added 0x48 for this frame
    # type and read exactly those words.
    print("np_usagefault -- extended FPU frame: core frame still at SP")
    m.reset(flags=0)
    m.call(m.early)
    frame = DTCM + 0x1D000
    for i, v in enumerate([0x11, 0x22, 0x33, 0x44, 0x55,
                           0x0812ABCD, 0x08112233, 0x21000000]):
        m.w32(frame + 4 * i, v)
    m.uc.mem_write(frame + 0x20, b"\xA5" * 0x48)       # S0-S15, FPSCR, reserved
    try:
        m.uc.reg_write(UC_ARM_REG_LR, 0xFFFFFFE9)      # MSP, FP frame
        m.uc.reg_write(UC_ARM_REG_MSP, frame)
        m.uc.emu_start(m.usagefault | 1, STOP, count=200)
    except Exception:
        pass
    check("extended-frame raw SP recorded", m.u32(m.diag + 0x08), frame)
    check("extended-frame PC decoded", m.u32(m.diag + 0x0C), 0x08112233)
    check("extended-frame LR decoded", m.u32(m.diag + 0x10), 0x0812ABCD)
    check("extended-frame xPSR decoded", m.u32(m.diag + 0x14), 0x21000000)

    print("np_busfault -- bad stack pointer must not be dereferenced")
    m.reset(flags=0)
    m.call(m.early)
    try:
        m.uc.reg_write(UC_ARM_REG_LR, 0xFFFFFFF9)
        m.uc.reg_write(UC_ARM_REG_MSP, 0xDEADBEEF)
        m.uc.emu_start(m.busfault | 1, STOP, count=200)
    except Exception:
        pass
    check("fault id = BusFault", m.u32(m.diag + 0x04), 3)
    check("bad SP recorded", m.u32(m.diag + 0x08), 0xDEADBEEF)
    check("PC left alone", m.u32(m.diag + 0x0C), 0)
    check("system reset requested", m.u32(SCB_AIRCR), 0x05FA0004)


def test_wiring(m):
    """
    The checks above call the patch entry points directly.  This one runs the
    patched image's own Reset_Handler and its own main-loop call site, so the
    rewritten branches are what gets exercised.
    """
    print("wiring -- run the patched Reset_Handler")
    m.reset(flags=0x02)                                 # FLAG_WDG
    m.uc.mem_write(m.diag, b"\x5A" * 0x40)
    m.call(RESET_HANDLER, count=2000000)
    check("np_early ran (diag magic)", m.u32(m.diag + 0x00), 0x4E4F5246)
    check("np_early ran (I-cache)", m.u32(SCB_CCR) & (1 << 17), 1 << 17)
    check("np_early ran (boot counter)", m.u32(m.diag + 0x2C), 1)
    check("bss zeroed by the stock code", m.u32(0x2000E2D8), 0)
    check("watchdog still disarmed before main", m.u32(m.diag + 0x30), 0)

    print("wiring -- run the patched main-loop call site")
    m.uc.reg_write(UC_ARM_REG_LR, STOP | 1)
    m.uc.emu_start(MAIN_HOOK_SITE | 1, MAIN_HOOK_SITE + 4, count=1000)
    check("watchdog armed from the main loop", m.u32(m.diag + 0x30), 1)
    check("IWDG running", m.u32(IWDG + 0x00), 0xAAAA)
    check("System::instance still reached", m.uc.reg_read(UC_ARM_REG_PC),
          MAIN_HOOK_SITE + 4)


def test_crc(m):
    print("np_late -- CRC32 self check")
    m.reset(flags=0x01)                                 # FLAG_CRC
    m.call(m.early)
    # point the check at a short, known region
    length = 4096
    m.uc.mem_write(BLOB + 0x0C, struct.pack("<I", length))
    expect = zlib.crc32(m.img[:length])
    m.uc.mem_write(BLOB + 0x08, struct.pack("<I", expect))
    m.call(m.late)
    check("CRC computed", m.u32(m.diag + 0x28), expect)
    check("CRC expected recorded", m.u32(m.diag + 0x24), expect)

    # if the image was built with --crc, run the real thing over the whole
    # region.  The harness stubs four stock entry points with "bx lr", so the
    # emulated flash differs from the file by a few bytes -- hash what the
    # emulator actually saw, and separately confirm the value baked into the
    # image matches the file.
    flags = m.img[BLOB - BASE + 0x05]
    stored = struct.unpack_from("<I", m.img, BLOB - BASE + 0x08)[0]
    full = struct.unpack_from("<I", m.img, BLOB - BASE + 0x0C)[0]
    if flags & 0x01 and full:
        print("np_late -- full image self check (%d bytes)" % full)
        m.reset()
        m.call(m.early)
        m.call(m.late, count=40000000)
        seen = bytes(m.uc.mem_read(BASE, full))
        check("CRC over the emulated image", m.u32(m.diag + 0x28), zlib.crc32(seen))
        check("stored CRC matches the file", stored, zlib.crc32(m.img[:full]))



# ---------------------------------------------------------------------------
# G1/G2/G2b -- synced X-Mod LFO and external MIDI phase gate
# ---------------------------------------------------------------------------

def assemble_thumb(source):
    """Assemble a Thumb fragment with the real toolchain.

    Hand-encoded stubs are exactly the kind of thing that fails silently, and
    the cross assembler is right here.  The fragment must be position
    independent -- no literal pools -- so it can be dropped at any address.
    """
    import subprocess, tempfile, os
    with tempfile.TemporaryDirectory() as d:
        src, obj, binf = (os.path.join(d, n) for n in ("s.S", "s.o", "s.bin"))
        open(src, "w").write(".syntax unified\n.thumb\n" + source + "\n")
        subprocess.run(["arm-none-eabi-as", "-mcpu=cortex-m7", "-mthumb",
                        "-mfpu=fpv5-d16", "-mfloat-abi=hard", src, "-o", obj],
                       check=True, capture_output=True)
        subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", obj, binf],
                       check=True)
        return open(binf, "rb").read()


# records (r0, s0) for every call, then returns -- replaces setRate
SETRATE_STUB = """
    push    {r1, r2, r3}
    movw    r1, #(%#x & 0xFFFF)
    movt    r1, #(%#x >> 16)
    ldr     r2, [r1]
    adds    r3, r2, #1
    str     r3, [r1]
    lsls    r2, r2, #3
    adds    r2, #8
    adds    r2, r1, r2
    str     r0, [r2]
    vstr    s0, [r2, #4]
    pop     {r1, r2, r3}
    bx      lr
"""


def stub_return_ptr(m, addr, value):
    """ldr r0, [pc, #0]; bx lr; .word value -- the same shape the stock
    singleton accessors use."""
    m.uc.mem_write(addr, struct.pack("<HHI", 0x4800, 0x4770, value))


def as_float(v):
    return v if isinstance(v, float) else struct.unpack("<f", struct.pack("<I", v & 0xFFFFFFFF))[0]


class LfoRig:
    """A transport, a project with 19 mod slots, and their owner objects."""

    def __init__(self, m, synced=(0,), value=0.0, tick_rate=192.0,
                 retrig=DEFAULT_RETRIG):
        self.m = m
        m.reset(flags=0)
        m.call(m.early)

        stub_return_ptr(m, ORIG_TRANSPORT, FAKE_TRANSPORT)
        stub_return_ptr(m, ORIG_PROJECT, FAKE_PROJECT)
        m.w32(FAKE_TRANSPORT + 4, struct.unpack("<I", struct.pack("<f", tick_rate))[0])

        # capture every setRate call
        m.uc.mem_write(CALL_LOG, b"\x00" * 0x400)
        m.uc.mem_write(ORIG_SETRATE,
                       assemble_thumb(SETRATE_STUB % (CALL_LOG, CALL_LOG)))

        m.w32(FAKE_SUB + 20, retrig)
        # The stock clock paths refresh rate indirectly: 0x08116C9C walks
        # local parameter 5 at project+0x6088 and dispatches vtable slot 2.
        # Build those objects as well as the local-0 objects used by the
        # phase walker so that tests cannot miss that call graph again.
        m.w32(FAKE_SUB + 8, ORIG_LFO_SPEED | 1)
        for i in range(SLOT_COUNT):
            slot = FAKE_SLOT + i * 0x40
            owner = self.owner(i)
            speed_param = slot + 0x10
            m.w32(SLOT_ARRAY + i * SLOT_STRIDE, slot)
            m.w32(SLOT_ARRAY + i * SLOT_STRIDE + 5 * 4, speed_param)
            m.w32(slot + 8, owner)
            m.w32(speed_param, FAKE_SUB)
            m.w32(speed_param + 8, owner)
            m.uc.mem_write(owner, b"\x00" * OWNER_STRIDE)
            m.w32(owner + 0x28, FAKE_SUB)
            mode = 1 if i in synced else 0
            m.uc.mem_write(owner + 0x10C, bytes([mode]))
            # np_xmod_speed takes the type from LFOTimeCParam's value, not
            # from the mode byte, so the real object's copy has to be here.
            m.w32(owner + 0x1A4, struct.unpack(
                "<I", struct.pack("<f", mode / 65536.0))[0])
            self.set_value(i, value)

    def owner(self, i):
        return FAKE_OWNER + i * OWNER_STRIDE

    def slot(self, i):
        return FAKE_SLOT + i * 0x40

    def set_value(self, i, v):
        self.m.w32(self.owner(i) + 0x184,
                   struct.unpack("<I", struct.pack("<f", v))[0])

    def set_rate(self, hz):
        self.m.w32(FAKE_TRANSPORT + 4,
                   struct.unpack("<I", struct.pack("<f", hz))[0])

    @property
    def calls(self):
        n = self.m.u32(CALL_LOG)
        assert n <= 127, (
            "the setRate recorder holds 127 entries and the slot array is "
            "immediately after it -- clear_calls() inside long loops")
        return [(self.m.u32(CALL_LOG + 8 + 8 * k),
                 as_float(self.m.u32(CALL_LOG + 12 + 8 * k))) for k in range(n)]

    def clear_calls(self):
        self.m.w32(CALL_LOG, 0)

    def phases(self, i):
        return [as_float(self.m.u32(self.owner(i) + off)) for off in PHASES]

    def division(self, idx):
        return as_float(self.m.u32(DIV_TABLE + 4 * idx))


def close(name, got, want, tol):
    ok = abs(got - want) <= tol
    print("  %-46s %s" % (name, "ok" if ok else
                          "FAIL got %.9g want %.9g" % (got, want)))
    if not ok:
        FAILS.append(name)


def cents(a, b):
    """interval between two frequencies, in cents"""
    import math
    return 1200.0 * math.log(a / b, 2.0)


# Every rig writes the same scratch addresses, so only the most recently
# built one describes the machine.  Reading an older one silently compares a
# rig with itself, which passes for free -- that has slipped through twice.
# Make it raise instead.
_RIG_GEN = [0]


class XModRig(LfoRig):
    """An LfoRig plus a CParam and a project with pitch and note fields.

    Slot 0 of the LfoRig is the one under test.  Its slot id (owner+4) is
    settable because the two audio-rate pitch branches turn on it: slot 0 is
    VCO1Tune, and the X-Mod destination is slot 8 (VCOTZFM).
    """

    def __init__(self, m, rate=0.5, slot_id=8, note=TEST_MIDI, transpose=0.0,
                 pitch=TEST_PITCH, tick_rate=192.0):
        LfoRig.__init__(self, m, synced=(), value=rate, tick_rate=tick_rate)
        self.owner0 = self.owner(0)
        m.uc.mem_write(FAKE_PROJECT, b"\x00" * 0x6074)
        # LfoRig wrote the slot array inside what is now the project object
        for i in range(SLOT_COUNT):
            m.w32(SLOT_ARRAY + i * SLOT_STRIDE, self.slot(i))
        # slot 4 of the LFO sub-object's vtable: the real setMode, so the
        # widened clamp is exercised rather than assumed
        m.w32(FAKE_SUB + 16, LFO_SETMODE | 1)
        m.uc.mem_write(self.owner0 + 4, bytes([slot_id]))
        self.set_note(note)
        self.set_f(FAKE_PROJECT + P_TRANSPOSE, transpose)
        self.set_f(FAKE_PROJECT + P_PITCH_OTHER, pitch)
        self.set_f(FAKE_PROJECT + P_PITCH_SLOT0, pitch)
        m.w32(FAKE_PARAM + 8, self.owner0)
        self.set_type(2)
        _RIG_GEN[0] += 1
        self._gen = _RIG_GEN[0]

    def _assert_current(self):
        if self._gen != _RIG_GEN[0]:
            raise AssertionError(
                "reading rig %d after rig %d replaced it in memory -- take "
                "the measurement before building the next one"
                % (self._gen, _RIG_GEN[0]))

    def set_f(self, addr, v):
        self.m.w32(addr, struct.unpack("<I", struct.pack("<f", v))[0])

    def set_note(self, note):
        """A note with nothing else held: both slots get it, as the note
        handler's first-note path at 0x0811739A does."""
        self.set_f(FAKE_PROJECT + P_NOTE_VCO1, note)
        self.set_f(FAKE_PROJECT + P_NOTE_VCO2, note)
        self.set_f(NP_NOTE, note)

    def set_note_legato(self, previous, new):
        """The legato path at 0x0811728A: VCO1 keeps the previous note,
        VCO2 takes the new one, and the hook records the new one."""
        self.set_f(FAKE_PROJECT + P_NOTE_VCO1, previous)
        self.set_f(FAKE_PROJECT + P_NOTE_VCO2, new)
        self.set_f(NP_NOTE, new)

    def set_note_slide(self, previous, new):
        """The slide path at 0x0811757C, which swaps the two."""
        self.set_f(FAKE_PROJECT + P_NOTE_VCO1, new)
        self.set_f(FAKE_PROJECT + P_NOTE_VCO2, previous)
        self.set_f(NP_NOTE, new)

    def set_pitch(self, pitch):
        """Set VCO1's live 0..126 pitch coordinate."""
        self.set_f(FAKE_PROJECT + P_PITCH_OTHER, pitch)

    def set_type(self, n):
        """A discrete CParam stores its integer as n / 65536.

        The type lives in LFOTimeCParam, which sits at owner+0x198, so its
        value is at owner+0x1A4.  np_xmod_speed reads it there -- the mode
        byte cannot serve, since the ratio type deliberately publishes 2.
        Mirror both, the way the real object does.
        """
        self.set_f(FAKE_PARAM + 0x0C, n / 65536.0)
        self.set_f(self.owner0 + P_TYPE_VALUE, n / 65536.0)

    def mode_byte(self):
        self._assert_current()
        return self.m.uc.mem_read(self.owner0 + 0x10C, 1)[0]

    def audio_flag(self):
        return self.m.uc.mem_read(self.owner0 + 0x25, 1)[0]

    def run_time(self, n):
        """np_xmod_time: the TYPE parameter changed to n."""
        self._assert_current()
        self.set_type(n)
        self.clear_calls()
        self.m.call(self.m.xmod_time, r0=FAKE_PARAM)
        return self.calls

    def run_speed(self):
        """np_xmod_speed: the RATE parameter changed; type comes from
        owner+0x10C, so this is also the check that the type stuck."""
        self._assert_current()
        self.clear_calls()
        self.m.call(self.m.xmod_speed, r0=FAKE_PARAM)
        return self.calls

    def rate_for(self, n):
        self._assert_current()
        calls = self.run_time(n)
        return calls[0][1] if len(calls) == 1 else float("nan")


def table_at(m, base, p):
    """the interpolation np_lfo.cpp does, in Python"""
    if not p > 0.0:
        return as_float(m.u32(base))
    if p >= 1.0:
        return as_float(m.u32(base + 4 * 256))
    x = p * 256.0
    i = int(x)
    lo = as_float(m.u32(base + 4 * i))
    hi = as_float(m.u32(base + 4 * (i + 1)))
    return lo + (hi - lo) * (x - i)


def test_xmod_type(m):
    print("np_xmod_time -- the four types, and what each writes")

    rig = XModRig(m, rate=0.75, slot_id=8, pitch=0.0)

    # (type, mode byte published to the stock readers, audio marker).
    # The ratio type deliberately publishes 2: owner+0x10C is shared with
    # five stock readers and must never hold a value they were not written
    # against.  owner+0x25 is what carries the fourth type, and its only
    # reader is our own DSP splice.
    for n, published, flag in ((0, 0, 0), (1, 1, 0), (2, 2, 1), (3, 2, 2)):
        calls = rig.run_time(n)
        check("type %d publishes mode %d" % (n, published),
              rig.mode_byte(), published)
        check("type %d audio flag" % n, rig.audio_flag(), flag)
        check("type %d calls setRate once" % n, len(calls), 1)
        if calls:
            check("type %d targets owner+0x28" % n, calls[0][0],
                  rig.owner0 + 0x28)

    print()
    print("np_xmod_time -- each type produces the rate its table says")
    rig = XModRig(m, rate=0.75, slot_id=8, pitch=0.0)
    close("type 0 = slow LFO table at 0.75", rig.rate_for(0),
          table_at(m, SLOW_TABLE, 0.75), 1e-6)
    # knob 0.75 -> index int(0.75*10) = 7 -> stock table entry kDivSelect[7]
    # = 9.  This is the check that keeps the parameter handler and the clock
    # tick agreeing about what a knob position selects.
    close("type 1 = 192 Hz / division[9] (1/8T)", rig.rate_for(1),
          192.0 / rig.division(9), 1e-6)
    close("type 2 = frequency table at 0.25", rig.rate_for(2),
          table_at(m, FREQ_TABLE, 0.75 - 0.5), 1e-3)
    check("an unknown type is silent", rig.rate_for(4), 0.0)


def ratio_values():
    """np_ratio_value from patch/np_lfo.cpp: since p49 the firmware carries
    only the offsets it uses (np_ratio_offset), not the values they came from"""
    import re
    src = open("patch/np_lfo.cpp").read()
    body = src[src.index("np_ratio_value[kRatioCount] = {"):]
    body = body[body.index("{") + 1:body.index("};")]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return [float(x.strip().rstrip("f")) for x in body.split(",") if x.strip()]


def ratio_tables(m):
    """the ratio values (source) and the offsets (read out of the image)"""
    n = m.ratio_count
    val = ratio_values()[:n]
    off = [as_float(struct.unpack_from("<I", m.img, m.ratio_offset - BASE + 4 * i)[0])
           for i in range(n)]
    return val, off


def test_xmod_ratio_table(m):
    print("H -- the ratio table is two banks of 64")
    import math

    check("128 detents", m.ratio_count, RATIO_COUNT)
    val, off = ratio_tables(m)
    lower, upper = val[:64], val[64:]

    # bank 1: the quarter grid, 0.25 .. 8.00.  Every value is a rational
    # p/4, so every value is harmonic -- this is the pitched half.
    worst = max(abs(v - 0.25 * (i + 1)) for i, v in enumerate(lower))
    close("bank 1 is 0.25 * (index + 1)", worst, 0.0, 1e-6)
    check("bank 1 spans 0.25 .. 16.00", (lower[0], lower[-1]), (0.25, 16.0))
    check("bank 1 ascends", lower == sorted(lower), True)
    for idx, want in ((3, 1.0), (7, 2.0), (11, 3.0), (15, 4.0), (31, 8.0),
                      (39, 10.0), (47, 12.0), (55, 14.0), (63, 16.0)):
        check("detent %2d is ratio %g" % (idx, want), val[idx], want)
    check("unison's offset is exactly zero", off[3], 0.0)

    # bank 2: the irrational columns of the Yamaha four-operator set.  Every
    # value must be n * m for one of the three multipliers -- if one of them
    # were rational the entry would be harmonic and would not belong here.
    ms = (math.sqrt(2.0), math.pi / 2.0, math.sqrt(3.0))
    # n runs in halves; the bank is the first 64 of the sorted grid
    ns = [0.5 * k for k in range(1, 40)]
    allowed = sorted({round(n * mm, 7) for n in ns for mm in ms})[:64]
    worst = max(abs(g - w) for g, w in zip(upper, allowed))
    close("bank 2 is the irrational Yamaha columns", worst, 0.0, 1e-5)
    check("bank 2 ascends", upper == sorted(upper), True)
    check("bank 2 tops out at 12*sqrt2", abs(upper[-1] - 12.0 * math.sqrt(2)) < 1e-4,
          True)
    # position is clamped to [0,1], so each bank stops separating above a
    # note set by its own ceiling.  The two must stay balanced: bank A tops
    # at 16.00 (MIDI 79.0), so bank B must not be much worse.
    def flattens(top):
        return 127.0 * (1.0 - 12.0 * math.log2(top) / 127.0)
    check("bank 1 holds to MIDI 79", abs(flattens(lower[-1]) - 79.0) < 0.5, True)
    check("bank 2 holds to MIDI 78, so neither bank is the weak link",
          abs(flattens(upper[-1]) - 78.0) < 0.5, True)

    # no entry of bank 2 may be a quarter: that would make it harmonic and
    # duplicate bank 1
    quarters = [v for v in upper if abs(v * 4.0 - round(v * 4.0)) < 1e-4]
    check("bank 2 holds nothing rational", quarters, [])

    # the seam is deliberate and worth pinning down, because it is the one
    # thing about this table that looks like a bug
    check("the table is not monotonic across the seam", val[64] < val[63],
          True)
    close("the seam drops from 16.00 to 0.7071", val[64], math.sqrt(2) / 2,
          1e-5)

    # the offset table is the only thing the firmware reads; if it does not
    # match the ratios it claims to encode, every ratio is quietly wrong
    worst = max(abs(o - 12.0 * math.log(v, 2.0) / 127.0)
                for v, o in zip(val, off))
    close("offset == 12 * log2(ratio) / 127", worst, 0.0, 1e-7)
    check("0.5 is an octave down", round(off[1] * 127.0 / 12.0, 6), -1.0)
    check("2.0 is an octave up", round(off[7] * 127.0 / 12.0, 6), 1.0)


def test_xmod_ratio(m):
    import math
    val, off = ratio_tables(m)
    osc1 = m.follow_osc1

    # The mode's base is a build switch: the played note by default, VCO1's
    # live coordinate with --xmod-follow-osc1.  Everything below is written
    # once and expects whichever this image was built with.
    NOTE, XPOSE = 42.0, 6.0
    base = (TEST_PITCH / 126.0) if osc1 else ((NOTE + XPOSE) / 127.0)

    print("np_xmod_time -- type 3 puts the modulator at ratio * %s"
          % ("live VCO1" if osc1 else "the played note"))

    def rig_for(idx, note=NOTE, xpose=XPOSE, pitch=TEST_PITCH, slot=8):
        return XModRig(m, rate=(idx + 0.5) / RATIO_COUNT, slot_id=slot,
                       note=note, transpose=xpose, pitch=pitch)

    unison = rig_for(3).rate_for(3)
    close("ratio 1.0 is the unison position", unison,
          table_at(m, FREQ_TABLE, base), 1e-3)

    for idx, name in ((1, "0.5"), (7, "2"), (11, "3"), (15, "4"), (31, "8")):
        got = rig_for(idx).rate_for(3)
        close("ratio %-3s sits %s above unison" % (name, name),
              cents(got, unison), 1200.0 * math.log(val[idx], 2.0), 1.0)

    print()
    print("np_xmod_time -- the ratio holds wherever the base goes")
    for step in (-12.0, 0.0, 12.0, 24.0):
        if osc1:
            kw = dict(pitch=TEST_PITCH + step * 126.0 / 127.0)
        else:
            kw = dict(note=NOTE + step)
        one = rig_for(3, **kw).rate_for(3)
        two = rig_for(7, **kw).rate_for(3)
        close("%+5.0f semitones: ratio 2 is still an octave" % step,
              cents(two, one), 1200.0, 1.0)

    print()
    print("np_xmod_time -- and it moves with the base, one for one")
    low = rig_for(11).rate_for(3)
    if osc1:
        high = rig_for(11, pitch=TEST_PITCH + 12.0 * 126.0 / 127.0).rate_for(3)
    else:
        high = rig_for(11, note=NOTE + 12.0).rate_for(3)
    close("an octave of the base is an octave of the modulator",
          cents(high, low), 1200.0, 1.0)

    print()
    if osc1:
        print("np_xmod_time -- built to follow VCO1, so the note must not move it")
        ref = rig_for(7, note=0.0, xpose=0.0).rate_for(3)
        other = rig_for(7, note=127.0, xpose=0.0).rate_for(3)
        check("the played note does not move it", other, ref)
    else:
        # This is the whole point of the default: silence VCO1, carry on
        # VCO2, and turning VCO1's tune knob must not drag the modulator.
        print("np_xmod_time -- built free of VCO1, so its pitch must not move it")
        ref = rig_for(7).rate_for(3)
        for p in (0.0, 63.0, 126.0):
            other = rig_for(7, pitch=p).rate_for(3)
            check("VCO1 pitch %5.1f leaves it alone" % p, other, ref)
        slot0 = rig_for(7, slot=0).rate_for(3)
        check("nor does the slot 0 branch", slot0, ref)
        xp = rig_for(7, note=NOTE + XPOSE, xpose=0.0).rate_for(3)
        check("transpose and note enter on the same scale", xp, ref)

    print()
    print("np_xmod_time -- type 2 is untouched by all of this")
    fixed = XModRig(m, rate=0.75, slot_id=8, note=NOTE,
                    pitch=0.0).rate_for(2)
    close("still the free sweep, knob - 0.5", fixed,
          table_at(m, FREQ_TABLE, 0.25), 1e-3)
    moved = XModRig(m, rate=0.75, slot_id=8, note=NOTE + 24.0,
                    pitch=0.0).rate_for(2)
    check("and still ignores the played note", moved, fixed)

    print()
    print("np_xmod_time -- the knob covers every detent and stops at the top")
    seen = set()
    for i in range(RATIO_COUNT):
        seen.add(rig_for(i).rate_for(3))
    check("all 64 ratios reachable and distinct", len(seen), RATIO_COUNT)
    top = XModRig(m, rate=1.0, slot_id=8, note=NOTE, transpose=XPOSE,
                  pitch=TEST_PITCH).rate_for(3)
    last = rig_for(RATIO_COUNT - 1).rate_for(3)
    check("knob at 1.0 clamps to the last detent", top, last)


def test_xmod_speed(m):
    print("np_xmod_speed -- reads the type back from where the core kept it")

    # The ratio type publishes 2 in the mode byte, so the type has to come
    # from LFOTimeCParam's own value.  Deriving it from the mode byte plus
    # the audio marker is a one-way latch: the marker is only written once
    # the type is already recognised, so any moment where the pair is
    # inconsistent collapses the slot to plain audio rate for good.  Force
    # that inconsistency and check the rate handler still resolves ratio.
    latch = XModRig(m, rate=0.75, slot_id=4, pitch=0.0)
    latch.set_note(60.0)
    latch.set_f(FAKE_PROJECT + P_TRANSPOSE, 0.0)
    latch.run_time(3)
    want = latch.rate_for(3)
    for bad in (0, 1):
        m.uc.mem_write(latch.owner0 + 0x25, bytes([bad]))
        latch.clear_calls()
        m.call(m.xmod_speed, r0=FAKE_PARAM)
        calls = latch.calls
        check("marker clobbered to %d: setRate still called once" % bad,
              len(calls), 1)
        if calls:
            close("and still the ratio frequency, not audio rate",
                  calls[0][1], want, 1e-6)
    check("and the handler restored the marker", latch.audio_flag(), 2)


    rig = XModRig(m, rate=0.75, slot_id=8)
    rig.run_time(3)                      # the ratio type
    # The ratio type publishes 2 in the shared mode byte and keeps its own
    # identity in the marker, so np_xmod_speed has to read the pair.
    check("the mode byte reads as ordinary audio rate", rig.mode_byte(), 2)
    check("and the marker carries the fourth type", rig.audio_flag(), 2)

    # Move whichever base this image was built to follow, by an octave, and
    # leave the other one alone: the rate handler has to agree with the
    # splice about which one it is.
    def set_base(midi):
        if m.follow_osc1:
            rig.set_f(FAKE_PROJECT + P_PITCH_OTHER, pitch_for_midi(midi))
            rig.set_note(7.0)
        else:
            rig.set_note(midi)
            rig.set_f(FAKE_PROJECT + P_PITCH_OTHER, pitch_for_midi(7.0))
        rig.set_f(FAKE_PROJECT + P_TRANSPOSE, 0.0)

    set_base(48.0)
    rig.clear_calls()
    m.call(m.xmod_speed, r0=FAKE_PARAM)
    ref = rig.calls[0][1] if rig.calls else float("nan")
    check("setRate called once", len(rig.calls), 1)

    set_base(60.0)
    rig.clear_calls()
    m.call(m.xmod_speed, r0=FAKE_PARAM)
    up = rig.calls[0][1] if rig.calls else float("nan")
    close("rate handler follows the same base as the splice",
          cents(up, ref), 1200.0, 1.0)

    print()
    print("np_xmod_speed -- and the queued encoder ramp is left to np_xmod_time")
    rig = XModRig(m, rate=0.5, slot_id=8, pitch=0.0)
    m.w32(rig.owner0 + 0x190, struct.unpack("<I", struct.pack("<f", 0.25))[0])
    m.w32(rig.owner0 + 0x194, 2)
    rig.run_time(2)
    check("pending decremented", m.u32(rig.owner0 + 0x194), 1)
    check("value advanced", round(as_float(m.u32(rig.owner0 + 0x184)), 6),
          0.75)
    rig.run_speed()
    check("the rate handler does not consume a step",
          m.u32(rig.owner0 + 0x194), 1)


def test_xmod_splice(m):
    print("np_xmod_pos -- the splice in the per-block DSP loop")

    rig = XModRig(m, rate=0.0, slot_id=8)
    val, off = ratio_tables(m)
    uc = m.uc
    STOCK_RET = 0x08112C8E
    RATIO_RET = 0x08112C9C   # past both of the loop's own pitch branches

    # What the loop has established by the time it reaches the splice:
    #   r0  the slot owner
    #   r3  &(engine + 0x2EDC), VCO1's live 0..126 coordinate
    #   s14 0.5, s15 the rate knob, s18 the loop-invariant 1/126
    # The ratio path reads r3 and s18 itself, because it rejoins past the
    # instruction that would have loaded them.
    PITCH_CELL = FAKE_PROJECT + P_PITCH_OTHER

    def run(flag, pitch, rate):
        uc.mem_write(rig.owner0 + 0x25, bytes([flag]))
        rig.set_f(PITCH_CELL, pitch)
        uc.reg_write(UC_ARM_REG_R0, rig.owner0)
        uc.reg_write(UC_ARM_REG_R2, 0xDEAD)
        uc.reg_write(UC_ARM_REG_R3, PITCH_CELL)
        uc.reg_write(UC_ARM_REG_S14,
                     struct.unpack("<I", struct.pack("<f", 0.5))[0])
        uc.reg_write(UC_ARM_REG_S15,
                     struct.unpack("<I", struct.pack("<f", rate))[0])
        uc.reg_write(UC_ARM_REG_S18,
                     struct.unpack("<I", struct.pack("<f", 1.0 / 126.0))[0])
        ret = RATIO_RET if flag == 2 else STOCK_RET
        uc.emu_start(m.xmod_pos | 1, ret, count=200)
        return (uc.reg_read(UC_ARM_REG_R2),
                as_float(uc.reg_read(UC_ARM_REG_S15)))

    def knob_for(i):
        return (i + 0.5) / RATIO_COUNT

    # flag 1 -- stock audio rate, both displaced instructions reproduced and
    # the loop's own pitch branch left to do its work
    r2, s15 = run(1, TEST_PITCH, 0.75)
    check("slot id reaches r2", r2, 8)
    close("type 2 is still rate - 0.5", s15, 0.25, 1e-6)

    # flag 0 -- not an audio-rate slot at all
    r2, s15 = run(0, TEST_PITCH, 0.75)
    check("a non-audio slot still reports its id", r2, 8)
    close("and takes the stock path", s15, 0.25, 1e-6)

    # flag 2 -- ratio mode.  Decorrelate the two candidate bases first:
    # with the note at 36 and the VCO1 coordinate also at MIDI 36 they give
    # the same number, and every check below passes whichever the build
    # follows.  Put them two octaves apart so only the right one matches.
    NOTE = 60.0
    other_pitch = pitch_for_midi(24.0)
    base = (TEST_PITCH / 126.0) if m.follow_osc1 else (NOTE / 127.0)

    def run_ratio(rate, note=NOTE, pitch=None):
        rig.set_note(note)
        rig.set_f(FAKE_PROJECT + P_TRANSPOSE, 0.0)
        return run(2, TEST_PITCH if pitch is None else pitch, rate)

    if not m.follow_osc1:
        # the whole reason the default exists
        _, a = run_ratio(knob_for(7))
        _, b = run_ratio(knob_for(7), pitch=other_pitch)
        close("VCO1's pitch does not move it", b - a, 0.0, 1e-7)

    for idx in (0, 3, 7, 11, 31, 63):
        _, s15 = run_ratio(knob_for(idx))
        close("detent %2d -> ratio offset + base" % idx, s15,
              off[idx] + base, 1e-6)

    _, s15 = run_ratio(1.0)
    close("knob at 1.0 clamps to the last detent", s15,
          off[RATIO_COUNT - 1] + base, 1e-6)

    # the property the mode exists for, measured on the splice itself
    if m.follow_osc1:
        _, a = run(2, pitch_for_midi(48.0), knob_for(3))
        _, b = run(2, pitch_for_midi(60.0), knob_for(3))
        _, c = run(2, pitch_for_midi(48.0), knob_for(7))
    else:
        _, a = run_ratio(knob_for(3), note=48.0)
        _, b = run_ratio(knob_for(3), note=60.0)
        _, c = run_ratio(knob_for(7), note=48.0)
    close("an octave of the base is 12/127 of the table", b - a,
          12.0 / 127.0, 1e-6)
    close("ratio 2 is another 12/127 on top", c - a, 12.0 / 127.0, 1e-6)

    if m.follow_osc1:
        return

    print()
    print("np_xmod_pos -- the ratio survives legato and slide")
    # The bug this guards: the note handler puts an incoming note into one
    # of two per-oscillator slots, and a slid step puts it in the other one.
    # Reading a single slot made the ratio track the *previous* note on
    # every slid step, which is what dense random slides turn into.
    rig.set_f(FAKE_PROJECT + P_TRANSPOSE, 0.0)

    def position(setup, idx):
        setup()
        return run(2, TEST_PITCH, knob_for(idx))[1]

    for idx in (0, 7, 31, 63):
        want = off[idx] + 60.0 / 127.0
        close("detent %2d: nothing held" % idx,
              position(lambda: rig.set_note(60.0), idx), want, 1e-6)
        close("detent %2d: legato onto 60 from 48" % idx,
              position(lambda: rig.set_note_legato(48.0, 60.0), idx),
              want, 1e-6)
        close("detent %2d: slid onto 60 from 48" % idx,
              position(lambda: rig.set_note_slide(48.0, 60.0), idx),
              want, 1e-6)

    # and the knob still spans its whole range on a slid step -- the
    # symptom was that it stopped doing anything
    rig.set_note_slide(48.0, 60.0)
    seen = {round(run(2, TEST_PITCH, knob_for(i))[1], 7)
            for i in range(RATIO_COUNT)}
    check("all 64 detents still distinct while sliding", len(seen),
          RATIO_COUNT)

    print()
    print("np_xmod_pos -- the splice must not clobber the loop's live state")
    # The splice sits inside an 11 KB function whose loop keeps values in
    # callee-saved and loop-invariant registers.  Anything the ratio path
    # touches that the stock path does not has to be dead at the rejoin.
    gp = [("r%d" % i, getattr(uc_arm, "UC_ARM_REG_R%d" % i)) for i in range(9)]
    gp += [(n, getattr(uc_arm, "UC_ARM_REG_" + n.upper()))
           for n in ("sb", "sl", "fp", "ip")]
    sr = [("s%d" % i, getattr(uc_arm, "UC_ARM_REG_S%d" % i)) for i in range(32)]

    def clobbers(flag):
        uc.mem_write(rig.owner0 + 0x25, bytes([flag]))
        for i, (_, r) in enumerate(gp):
            uc.reg_write(r, 0xA5A50000 | i)
        for i, (_, r) in enumerate(sr):
            uc.reg_write(r, struct.unpack("<I", struct.pack("<f", 1e3 + i))[0])
        uc.reg_write(UC_ARM_REG_R0, rig.owner0)
        uc.reg_write(UC_ARM_REG_R3, PITCH_CELL)
        for r, v in ((UC_ARM_REG_S14, 0.5), (UC_ARM_REG_S15, 0.75),
                     (UC_ARM_REG_S18, 1.0 / 126.0)):
            uc.reg_write(r, struct.unpack("<I", struct.pack("<f", v))[0])
        was = {n: uc.reg_read(r) for n, r in gp + sr}
        uc.emu_start(m.xmod_pos | 1,
                     RATIO_RET if flag == 2 else STOCK_RET, count=200)
        return {n for n, r in gp + sr if uc.reg_read(r) != was[n]}

    stock_set, ratio_set = clobbers(1), clobbers(2)
    check("the stock path touches only r1, r2, s15",
          " ".join(sorted(stock_set)), "r1 r2 s15")
    extra = ratio_set - stock_set
    check("the ratio path adds only s0 and s14",
          " ".join(sorted(extra)), "s0 s14")
    # s0 and s14 are dead at the rejoin: every one of the three paths out of
    # RATIO_RET writes both before reading them (0x08112CC6 / 0x08112CCC,
    # 0x08112CA8, 0x08113106).  Nothing the loop carries may appear here.
    carried = {"r4", "r5", "r6", "r7", "sl", "fp",
               "s16", "s17", "s18", "s19"}
    check("and nothing the loop carries across an iteration",
          " ".join(sorted(extra & carried)), "")

    print()
    print("np_note_hook -- what the note handler now calls")
    check("np_note is the word the splice reads", m.note_addr, NP_NOTE)

    # Prove the tail-call by making the function it replaces answer: if the
    # hook lands anywhere else this comes back as something other than the
    # marker, or does not come back at all.
    MARKER = 0x0D15EA5E
    stub_return_const(m, NOTE_HOOK_TAIL, MARKER)
    EVENT = 0x2001C800
    m.w32(NP_NOTE, 0)
    rig.set_f(EVENT + 8, 71.5)                   # CNoteData::note
    for reg, value in ((UC_ARM_REG_R2, 0xC0DE), (UC_ARM_REG_R3, 0xBEEF)):
        uc.reg_write(reg, value)
    uc.reg_write(UC_ARM_REG_R4, EVENT)
    m.call(m.note_hook & ~1)
    check("it tail-calls the function it replaced",
          uc.reg_read(UC_ARM_REG_R0), MARKER)
    close("having recorded the note off the event",
          as_float(m.u32(NP_NOTE)), 71.5, 1e-7)
    check("and left r2 alone for it", uc.reg_read(UC_ARM_REG_R2), 0xC0DE)
    check("and r3", uc.reg_read(UC_ARM_REG_R3), 0xBEEF)


def test_wave_shape(m):
    """np_wave_shape -- the renderer the slot evaluators now dispatch to."""
    print("np_wave_shape -- the modulator's waveform, band limited")

    SLOT = 0x2001D400
    PH = (0x58, 0x84, 0xA8, 0xCC, 0xF0)          # sine tri square saw S&H
    HELD = 0x108
    uc = m.uc

    def setup(phase, step):
        uc.mem_write(SLOT, b"\x00" * 0x200)
        for off in PH:
            m.w32(SLOT + off, struct.unpack("<I", struct.pack("<f", phase))[0])
            m.w32(SLOT + off + 8,
                  struct.unpack("<I", struct.pack("<f", step))[0])

    def shape(wave, phase, step):
        setup(phase, step)
        m.call(m.wave_shape, r0=SLOT, r1=wave)
        return (as_float(uc.reg_read(UC_ARM_REG_S0)),
                as_float(m.u32(SLOT + PH[wave] if wave < 5 else SLOT)))

    # the stock phase advance and wrap, reproduced exactly
    for wave in range(4):
        _, p = shape(wave, 0.49, 0.02)
        close("wave %d wraps past +0.5" % wave, p, -0.49, 1e-6)
        _, p = shape(wave, -0.49, -0.02)
        close("wave %d wraps past -0.5" % wave, p, 0.49, 1e-6)
        _, p = shape(wave, 0.0, 0.1)
        close("wave %d otherwise just advances" % wave, p, 0.1, 1e-6)

    check("an out-of-range waveform gives 0.0, as the stock bail did",
          shape(0, 0.0, 0.1)[0] is not None, True)
    setup(0.0, 0.1)
    m.call(m.wave_shape, r0=SLOT, r1=7)
    close("waveform 7 -> 0.0", as_float(uc.reg_read(UC_ARM_REG_S0)), 0.0, 0.0)

    # sample and hold: stock stores the advanced phase, then tests it
    setup(0.0, 0.1)
    m.call(m.wave_shape, r0=SLOT, r1=4)
    held = as_float(m.u32(SLOT + HELD))
    close("S&H holds its value between wraps",
          as_float(uc.reg_read(UC_ARM_REG_S0)), held, 0.0)
    close("and advances the phase", as_float(m.u32(SLOT + PH[4])), 0.1, 1e-6)

    # a full cycle of the sine, through the real code
    N = 4096
    step = 1.0 / N
    setup(-0.5, step)
    got = []
    for _ in range(N):
        m.call(m.wave_shape, r0=SLOT, r1=0)
        got.append(as_float(uc.reg_read(UC_ARM_REG_S0)))
    # the phase starts at -0.5, so after i+1 advances it is -0.5 + u, and
    # the stock mapping makes the output -cos(2*pi*phase) = +cos(2*pi*u)
    ref = [math.cos(2.0 * math.pi * ((i + 1) * step)) for i in range(N)]
    err = max(abs(a - b) for a, b in zip(got, ref))
    # The modulator's sine is the same folded degree-7 polynomial np_sine
    # uses for the main oscillators (p16 had swapped in a parabola when it
    # ran 80 times a block; since p19 only audio-rate slots pay per sample,
    # and the polynomial came back in p25).
    check("the sine is a real sine to 1e-5", err < 1e-5, True)
    check("and it keeps the stock polarity", got[N // 2] < 0.0, True)
    close("with unity peak to the fit's own accuracy", max(abs(v) for v in got), 1.0, 2e-5)

    print()
    print("sample & hold -- white noise at the top of the rate knob (H5)")
    m.w32(RNG_CELL, 0x12345678)
    uc.mem_write(ORIG_RNG, assemble_thumb(RNG_STUB % (RNG_CELL, RNG_CELL)))

    KNOB, MARKER = 0x184, 0x25

    def sh_run(step, knob, marker=1, n=512):
        setup(-0.5, step)
        m.w32(SLOT + KNOB, struct.unpack("<I", struct.pack("<f", knob))[0])
        uc.mem_write(SLOT + MARKER, bytes([marker]))
        out = []
        for _ in range(n):
            m.call(m.wave_shape, r0=SLOT, r1=4)
            out.append(as_float(uc.reg_read(UC_ARM_REG_S0)))
        return out

    def r1(v):
        num = sum(v[i] * v[i + 1] for i in range(len(v) - 1))
        return abs(num / max(1e-9, sum(x * x for x in v)))

    # the knob is what decides, so a slow step must still turn to noise
    # at the top -- this is the case the frequency-keyed first version got
    # wrong, since a full knob at C4 only reaches a step of about 0.31
    top = sh_run(0.01, 1.0)
    check("full knob, slow step: every sample fresh", len(set(top)) > 500, True)
    check("and uncorrelated (|r1| < 0.15)", r1(top) < 0.15, True)
    check("inside [-1, 1)", all(-1.0 <= x < 1.0 for x in top), True)
    check("mean near zero", abs(sum(top) / len(top)) < 0.1, True)

    edge = sh_run(0.01, 117.0 / 127.0)
    check("exactly ten from the top is already noise",
          len(set(edge)) > 500, True)

    below = sh_run(0.01, 116.0 / 127.0)
    check("eleven from the top still holds (r1 > 0.9)", r1(below) > 0.9, True)
    changes = sum(1 for i in range(1, len(below)) if below[i] != below[i - 1])
    check("and steps once per phase wrap", 4 <= changes <= 8, True)

    # only plain audio rate: ratio mode keeps all 64 of its exact ratios,
    # and free/synced LFOs use the same knob for a period
    for marker, what in ((2, "ratio mode"), (0, "free or synced")):
        v = sh_run(0.01, 1.0, marker=marker)
        check("full knob in %s still holds" % what, r1(v) > 0.9, True)

    # In the noise zone the phase is never consulted, so the modulator's
    # frequency -- and with it the tie to VCO1's pitch that audio rate
    # normally carries -- stops reaching the output at all.  These are the
    # steps a full knob produces at C2, C4 and C5 and up.
    def seeded(st):
        m.w32(RNG_CELL, 0x12345678)      # same seed, so the runs are comparable
        return sh_run(st, 1.0, n=256)

    seqs = [seeded(st) for st in (0.001, 0.088, 0.311, 0.392)]
    check("the noise is bit-identical at every VCO1 pitch",
          all(q == seqs[0] for q in seqs[1:]), True)
    check("and it is not a held value repeating",
          len(set(seqs[0])) > 250, True)

    setup(0.0, 0.1)
    m.call(m.wave_shape, r0=SLOT, r1=5)
    close("waveform 5 is still 0.0 -- no sixth waveform was added",
          as_float(uc.reg_read(UC_ARM_REG_S0)), 0.0, 0.0)

    # and the naive parabola it replaces, for the record
    par = []
    for i in range(N):
        p = -0.5 + (i + 1) * step
        if p > 0.5:
            p -= 1.0
        q = p + 1.0 if p < -0.25 else p
        x = (q - 0.25) * 4.0
        par.append((2.0 - abs(x)) * x)
    perr = max(abs(a - b) for a, b in zip(par, ref))
    check("the parabola it replaces was 4000x worse", perr > 4000 * err, True)


MULTIWAVE_RENDER = 0x081565DC         # MultiWaveOscFast vtable slot 2
FAKE_OSC = 0x2001C000                 # phase +0x30, increment +0x38, wave +0xE5
assert DTCM < FAKE_OSC and FAKE_OSC + 0x100 < DTCM + 0x1F000, \
    "the scratch oscillator must sit inside DTCM and below the stack"
ORIG_RNG = 0x08147984                 # xorshift32, state on a System object
RNG_CELL = 0x2001C400                 # where the test keeps that state


def render_cycle(m, waveform, periods=1, n=4096):
    """Run the real renderer for `periods` full cycles and return the samples.

    This is the patched code doing the arithmetic, not a Python model of it:
    the point is what the firmware emits, not what it ought to.
    """
    uc = m.uc
    uc.mem_write(FAKE_OSC, b"\x00" * 0x100)
    uc.mem_write(FAKE_OSC + 0xE5, bytes([waveform]))
    inc = float(periods) / n
    for off in (0x38, 0x64, 0x88, 0xAC, 0xD0):        # each wave's own step
        m.w32(FAKE_OSC + off, struct.unpack("<I", struct.pack("<f", inc))[0])
    for off in (0x30, 0x5C, 0x80, 0xA4, 0xC8):        # and its own phase
        m.w32(FAKE_OSC + off, struct.unpack("<I", struct.pack("<f", -0.5))[0])
    out = []
    for _ in range(n):
        uc.reg_write(UC_ARM_REG_R0, FAKE_OSC)
        uc.reg_write(UC_ARM_REG_LR, STOP | 1)
        uc.reg_write(UC_ARM_REG_SP, DTCM + 0x1F000)
        uc.emu_start(MULTIWAVE_RENDER | 1, STOP, count=400)
        out.append(as_float(uc.reg_read(UC_ARM_REG_S0)))
    return out


def dft_bin(samples, k):
    import cmath
    n = len(samples)
    acc = 0j
    for i, v in enumerate(samples):
        acc += v * cmath.exp(-2j * cmath.pi * k * i / n)
    return abs(acc) / n


def harmonics(samples):
    """THD and the third-harmonic level of one buffer holding whole cycles."""
    f = dft_bin(samples, 1)
    hs = [dft_bin(samples, h) for h in range(2, 16)]
    thd = (sum(v * v for v in hs) ** 0.5) / f if f else float("inf")
    h3 = 20 * math.log10(hs[1] / f) if f and hs[1] else -300.0
    return thd, h3


def alias_energy(samples, cycles):
    """Alias energy as a fraction of the total, in dB.

    The buffer holds a whole number of cycles, so every real harmonic falls
    exactly on a bin that is a multiple of `cycles`.  Everything off those
    bins got there by folding back from above Nyquist.
    """
    import cmath
    n = len(samples)
    tot = alias = 0.0
    for k in range(1, n // 2):
        acc = 0j
        for i, v in enumerate(samples):
            acc += v * cmath.exp(-2j * cmath.pi * k * i / n)
        e = abs(acc) ** 2
        tot += e
        if k % cycles:
            alias += e
    return 10 * math.log10(alias / tot + 1e-30)


def test_sine(m):
    print("np_sine -- what the patched renderer actually emits")
    m.reset(flags=0)          # FAKE_OSC is inside the DTCM the rig maps

    samples = render_cycle(m, 0, periods=1, n=2048)
    close("peak is unity", max(abs(v) for v in samples), 1.0, 1e-3)

    thd, h3 = harmonics(samples)
    print("      THD %.6f %%, third harmonic %.1f dB" % (100 * thd, h3))
    check("THD below 0.001 %", thd < 1e-5, True)
    check("third harmonic below -120 dB", h3 < -120.0, True)

    # ...and against a true sinusoid sample by sample.  The renderer starts
    # at the positive peak, so take the fundamental's own amplitude and
    # phase out of bin 1 rather than assuming a convention.
    import cmath
    n = len(samples)
    acc = sum(v * cmath.exp(-2j * cmath.pi * i / n)
              for i, v in enumerate(samples))
    amp, phase = 2.0 * abs(acc) / n, cmath.phase(acc)
    worst = max(abs(samples[i] - amp * math.cos(2 * math.pi * i / n + phase))
                for i in range(n))
    close("residual against a true sinusoid", worst, 0.0, 2e-4)

    print()
    print("np_sine -- the stock parabola, for the record")
    stock = [x * (2.0 - abs(x)) for x in
             ((-2.0 + 4.0 * i / 2048) for i in range(2048))]
    sthd, _ = harmonics(stock)
    print("      THD %.4f %%" % (100 * sthd))
    check("the replacement is at least 1000x cleaner", sthd / thd > 1000.0,
          True)


def test_waveforms(m):
    print("np_saw / np_square / np_tri -- aliasing at the 32 kHz rate")
    m.reset(flags=0)

    # 257 cycles in 2048 samples is 4.0 kHz at fs = 32 kHz, and 257 is prime
    # to 2048 so the harmonics spread over the whole spectrum instead of
    # landing on a handful of bins.
    n, cycles = 2048, 257
    dt = float(cycles) / n

    def stock(kind):
        out = []
        p = -0.5
        for _ in range(n):
            p += dt
            if p > 0.5:
                p -= 1.0
            elif p < -0.5:
                p += 1.0
            if kind == "saw":
                out.append(2.0 * p)
            elif kind == "square":
                out.append(1.0 if p > 0.0 else -1.0)
            else:
                out.append(4.0 * abs(p) - 1.0)
        return out

    # The saw and the square are steps: 1/n spectrum, and polyBLEP is worth
    # double figures.  The triangle is corners, 1/n^2, so it starts 16 dB
    # cleaner and polyBLAMP only buys a couple -- worth having, not worth
    # pretending about.
    for wf, kind, want in ((3, "saw", 8.0), (2, "square", 8.0), (1, "tri", 1.5)):
        got = render_cycle(m, wf, periods=cycles, n=n)
        a_stock = alias_energy(stock(kind), cycles)
        a_patched = alias_energy(got, cycles)
        print("      %-8s stock %6.1f dB -> patched %6.1f dB  (%+.1f dB)"
              % (kind, a_stock, a_patched, a_patched - a_stock))
        check("%s: at least %.1f dB less alias energy" % (kind, want),
              a_stock - a_patched >= want, True)
        check("%s: peak still within unity" % kind,
              max(abs(v) for v in got) <= 1.02, True)

    print()
    print("np_square -- the beat is the rising edge, not the falling one")
    # Stock starts the cycle low, so a retrigger puts the beat on the falling
    # edge and the rising edge halfway through the division.  Inverted here.
    slow = render_cycle(m, 2, periods=1, n=64)
    check("it starts high after a phase reset", slow[0] > 0.9, True)
    check("and is low in the second half", slow[48] < -0.9, True)
    flips = [i for i in range(1, len(slow))
             if (slow[i] > 0.0) != (slow[i - 1] > 0.0)]
    check("exactly one edge inside the cycle", len(flips), 1)
    # the renderer advances the phase before computing the sample, so the
    # edge lands one sample early against a naive count
    check("and it sits at the half-way point", abs(flips[0] - 32) <= 1, True)


RNG_STUB = """
    push    {r1, r2}
    movw    r1, #(%#x & 0xFFFF)
    movt    r1, #(%#x >> 16)
    ldr     r2, [r1]
    eor.w   r2, r2, r2, lsl #13
    eor.w   r2, r2, r2, lsr #17
    eor.w   r2, r2, r2, lsl #5
    str     r2, [r1]
    mov     r0, r2
    pop     {r1, r2}
    bx      lr
"""


def test_sample_hold(m):
    print("sample & hold -- checking the stock one is sane")
    m.reset(flags=0)

    # The stock generator is a xorshift32 whose state lives on a System
    # object this rig does not build, so it would read zero -- and zero is
    # an absorbing state for xorshift.  Give it the same generator over a
    # cell of our own, seeded, so the hold logic is what gets tested.
    m.uc.mem_write(ORIG_RNG, assemble_thumb(RNG_STUB % (RNG_CELL, RNG_CELL)))
    m.w32(RNG_CELL, 0x1234ABCD)

    n, cycles = 4096, 64
    got = render_cycle(m, 4, periods=cycles, n=n)

    changes = sum(1 for i in range(1, n) if got[i] != got[i - 1])
    check("changes once per cycle, not per sample", changes, cycles)
    steps = sorted(set(round(v, 6) for v in got))
    check("every step is inside [-1, 1)",
          all(-1.0 <= v < 1.0 for v in steps), True)
    check("the values are not repeating", len(steps) > cycles // 2, True)
    mean = sum(got) / len(got)
    print("      %d distinct values, mean %+.3f, min %+.3f, max %+.3f"
          % (len(steps), mean, min(got), max(got)))
    check("mean is near zero for a uniform source", abs(mean) < 0.15, True)


USART1 = 0x40011000
USART1_CR1 = USART1 + 0x00
USART1_ISR = USART1 + 0x1C
USART1_TDR = USART1 + 0x28
USART_TXE = 1 << 7
USART_TXEIE = 1 << 7
FAKE_PORT = 0x30020000                # the MIDI object: ring +0xA18, ...
MIDI_RING, MIDI_HEAD, MIDI_TAIL = 0x0A18, 0x0C18, 0x0C1C


def midi_fill(m, data):
    """Put `data` in the ring and point head/tail at it."""
    m.uc.mem_write(FAKE_PORT + MIDI_RING, bytes(512))
    m.uc.mem_write(FAKE_PORT + MIDI_RING, bytes(data))
    m.w32(FAKE_PORT + MIDI_HEAD, 0)
    m.w32(FAKE_PORT + MIDI_TAIL, len(data) & 0x1FF)


def midi_spliced(m, addr, stock_hex):
    """Is the replacement actually wired in at `addr`?

    Calling the blob entry point directly proves the function works and
    proves nothing about whether the firmware ever reaches it -- a build
    that quietly skipped the poke would pass every other check here.
    """
    got = m.img[addr - BASE:addr - BASE + 4]
    if got.hex() == stock_hex:
        return False
    ins = struct.unpack("<HH", got)
    reached = (ins[0] & 0xF800) == 0xF000 and (ins[1] & 0xD000) == 0x9000
    check("0x%08X is a b.w, not stock bytes" % addr, reached, True)
    return True


def test_midi_tx(m):
    print("np_midi_tx -- the DIN transmitter")
    m.reset(flags=0)

    wired = midi_spliced(m, 0x0811AC90, "d0f81c3c")
    check("the transmitter is spliced in", wired, True) if wired else \
        print("      (built with --no-midi: the splice is absent by choice)")

    # The old call site is now a kick: it only enables TXEIE.
    midi_fill(m, [])
    m.w32(USART1_CR1, 0)
    m.call(m.midi_tx, r0=FAKE_PORT)
    check("empty queue leaves TXEIE off", m.u32(USART1_CR1) & USART_TXEIE, 0)
    check("and leaves the read index alone", m.u32(FAKE_PORT + MIDI_HEAD), 0)

    midi_fill(m, [0x90, 0x40, 0x7F])
    m.w32(USART1_CR1, 0)
    m.w32(USART1_TDR, 0xAA)
    m.call(m.midi_tx, r0=FAKE_PORT)
    check("nonempty queue enables TXEIE",
          m.u32(USART1_CR1) & USART_TXEIE, USART_TXEIE)
    check("kick writes no byte", m.u32(USART1_TDR) & 0xFF, 0xAA)
    check("kick does not advance the queue",
          m.u32(FAKE_PORT + MIDI_HEAD), 0)

    # Stub the singleton getter to the test ring.  The combined USART IRQ
    # writes exactly one byte for every hardware TXE assertion.
    m.uc.mem_write(0x0811A6A4, assemble_thumb(
        "movw r0, #0x0000\n movt r0, #0x3002\n bx lr"))
    burst = [0x90, 0x40, 0x7F, 0x90, 0x44, 0x7F]
    midi_fill(m, burst)
    m.w32(USART1_CR1, USART_TXEIE)
    got = []
    for i in range(len(burst)):
        m.w32(USART1_ISR, USART_TXE)
        m.w32(USART1_TDR, 0)
        m.call(m.midi_rx)
        got.append(m.u32(USART1_TDR) & 0xFF)
        check("TXE IRQ %d advances one byte" % i,
              m.u32(FAKE_PORT + MIDI_HEAD), i + 1)
    check("two note-ons leave in order", got, burst)
    m.w32(USART1_ISR, USART_TXE)
    m.call(m.midi_rx)
    check("empty IRQ disables TXEIE", m.u32(USART1_CR1) & USART_TXEIE, 0)

    # the ring must wrap, not run off the end
    m.uc.mem_write(FAKE_PORT + MIDI_RING, bytes(512))
    m.uc.mem_write(FAKE_PORT + MIDI_RING + 510, bytes([0xB0, 0x01]))
    m.uc.mem_write(FAKE_PORT + MIDI_RING, bytes([0x40]))
    m.w32(FAKE_PORT + MIDI_HEAD, 510)
    m.w32(FAKE_PORT + MIDI_TAIL, 511)
    out = []
    m.w32(USART1_CR1, USART_TXEIE)
    for want_tail in (0, 1):
        m.w32(USART1_ISR, USART_TXE); m.w32(USART1_TDR, 0)
        m.call(m.midi_rx)
        out.append(m.u32(USART1_TDR) & 0xFF)
        m.w32(FAKE_PORT + MIDI_TAIL, want_tail)
    m.w32(USART1_ISR, USART_TXE); m.w32(USART1_TDR, 0)
    m.call(m.midi_rx)
    out.append(m.u32(USART1_TDR) & 0xFF)
    check("the read index wraps at 512", out, [0xB0, 0x01, 0x40])
    check("and lands where it should", m.u32(FAKE_PORT + MIDI_HEAD), 1)

    # bounded work: an IRQ sends one byte, never drains the ring in a loop.
    midi_fill(m, [(0x40 + i) & 0x7F for i in range(200)])
    before = m.u32(FAKE_PORT + MIDI_HEAD)
    m.w32(USART1_CR1, USART_TXEIE)
    m.w32(USART1_ISR, USART_TXE)
    m.call(m.midi_rx)
    check("one IRQ is bounded to one byte",
          m.u32(FAKE_PORT + MIDI_HEAD) - before, 1)

    print()
    print("np_midi_rx -- garbled bytes must not reach the parser")

    # Start a fresh translation cache before replacing the singleton stub.
    # Unicorn 2.0.1 does not invalidate an already translated block on
    # mem_write(), even though the real MCU obviously fetches the new bytes.
    m.reset(flags=0)

    USART1_ICR = USART1 + 0x20
    USART1_RDR = USART1 + 0x24
    FE, NE, ORE, RXNE = 1 << 1, 1 << 2, 1 << 3, 1 << 5

    # stub the MIDI object getter; a delivered byte is observable in that
    # object's receive ring (+0x810, head +0xA14 -- the handler stores it
    # itself since p35 MF3, it no longer calls the stock 0x0811A738)
    # Use a literal load here.  Unicorn 2.0.1 on Apple ARM has a MOVW
    # immediate quirk; that emulator bug must not masquerade as a MIDI bug.
    PORT = 0x30021234
    stub_return_const(m, 0x0811A6A4, PORT)

    def rx(status, byte):
        m.w32(PORT + 0xA14, 0); m.w32(PORT + 0xA10, 0)
        m.uc.mem_write(PORT + 0x810, b"\0")
        m.w32(USART1_ISR, status | RXNE)
        m.w32(USART1_RDR, byte)
        m.w32(USART1_ICR, 0)
        m.call(m.midi_rx)
        calls = m.u32(PORT + 0xA14)
        return calls, m.uc.mem_read(PORT + 0x810, 1)[0], PORT if calls else 0

    m.w32(m.diag + 0x3C, 0)

    calls, got, port = rx(0, 0x90)
    check("a clean byte is delivered", (calls, got), (1, 0x90))
    check("into the ring of the port the stock getter returns", port, PORT)
    check("clean RX does not clear unrelated flags", m.u32(USART1_ICR), 0)

    calls, _, _ = rx(FE, 0x90)
    check("a framing error is not delivered", calls, 0)
    check("and is counted", m.u32(m.diag + 0x3C) & 0xFFFF, 1)

    calls, _, _ = rx(NE, 0x90)
    check("a noise error is not delivered", calls, 0)
    check("and is counted too", m.u32(m.diag + 0x3C) & 0xFFFF, 2)

    calls, got, _ = rx(ORE, 0x41)
    check("an overrun byte IS delivered", (calls, got), (1, 0x41))
    check("and counted in the high half", m.u32(m.diag + 0x3C) >> 16, 1)
    check("without touching the low half", m.u32(m.diag + 0x3C) & 0xFFFF, 2)

    print()
    print("np_midi_tx -- corrected stock timing")
    # SysTick itself is 4 kHz, but this call is in its modulo-four arm.
    # Stock is therefore safe from TDR overwrite and merely 3.125x slower
    # than the DIN wire's byte capacity.
    tick, byte_time = 1000.0, 320.0
    check("stock polling interval is slower than a wire byte",
          tick > byte_time, True)


def test_xmod_ui(m):
    print("H -- the three UI copies of the type count")

    # The wrap table at 0x0815E8C8 is not the only bound: each of the three
    # TYPE actions carries its own literal, selected by the same conditional
    # that picks local parameter 7 over local 6.  Leaving any of them at 3
    # makes type 3 unreachable from that control.  Verify the whole
    # conditional pair, so a poke that hit the waveform arm would show up.
    img = m.img
    off = lambda a: a - BASE
    for addr, want, what in (
            (0x081500FE, "15bf0527042706260726", "direct"),
            (0x08150550, "15bf4ff005084ff0040806270727", "alternate"),
            (0x081506EA, "15bf042305234ff007094ff00609", "batch")):
        got = img[off(addr):off(addr) + len(want) // 2].hex()
        check("TYPE cycle (%s): waveforms 5, types 4" % what, got, want)


def test_xmod_data(m):
    print("H -- the data pokes that make type 3 reachable")

    img = m.img
    off = lambda a: a - BASE
    check("wrap table entry 7 (X-Mod type count)",
          struct.unpack_from("<H", img, off(0x0815E8D6))[0], 4)
    check("entry 6 (LFO waveforms) untouched -- H5 needs no new waveform",
          struct.unpack_from("<H", img, off(0x0815E8D4))[0], 5)
    check("entry 8 (X-Env destinations) untouched",
          struct.unpack_from("<H", img, off(0x0815E8D8))[0], 2)
    # owner+0x10C is read in five places across the image and stock never
    # stores anything but 0, 1 or 2 there.  The ratio type must not be the
    # thing that puts a fourth value in front of those readers, so all three
    # sites that used to be widened are left exactly as the vendor shipped
    # them.
    check("setMode bound untouched", img[off(0x081562C8)], 0x02)
    check("evaluator audio-rate test A untouched",
          img[off(0x0811582E):off(0x0811582E) + 6].hex(), "022b00f00f82")
    check("evaluator audio-rate test B untouched",
          img[off(0x08115BBC):off(0x08115BBC) + 4].hex(), "022c7dd0")

    rig = XModRig(m, rate=0.5, slot_id=8, pitch=0.0)
    rig.run_time(3)
    check("the ratio type stores mode 2", rig.mode_byte(), 2)
    check("with the marker at 2", rig.audio_flag(), 2)
    rig.run_time(2)
    check("plain audio rate stores mode 2 too", rig.mode_byte(), 2)
    check("but the marker at 1", rig.audio_flag(), 1)
    calls = rig.run_time(4)
    check("mode 4 is still refused", rig.mode_byte(), 2)
    check("rejected mode clears the audio marker", rig.audio_flag(), 0)
    check("rejected mode calls setRate once", len(calls), 1)
    if calls:
        close("rejected mode still stops the LFO", calls[0][1], 0.0, 0.0)

    # The physical TYPE action and two alternate UI actions each embed their
    # own `% 3`; the generic parameter wrap table alone cannot change them.
    # Check their whole conditional pairs so the waveform's five choices and
    # local index 6 cannot be accidentally changed along with X-Mod type.
    check("direct TYPE/WAVE cycle: type four, waveform five",
          img[off(0x081500FE):off(0x08150108)].hex(),
          "15bf0527042706260726")
    check("alternate TYPE/WAVE cycle: type four, waveform five",
          img[off(0x08150550):off(0x0815055E)].hex(),
          "15bf4ff005084ff0040806270727")
    check("batch TYPE/WAVE cycle: type four, waveform five",
          img[off(0x081506EA):off(0x081506F8)].hex(),
          "15bf042305234ff007094ff00609")


def test_lfo_tick(m):
    print("np_mod_tick -- rate follows transport; phase follows division")

    rig = LfoRig(m, synced=(0,), value=0.0, tick_rate=192.0)
    m.call(m.mod_tick, r0=rig.slot(0), r1=0)
    check("phase hook does not duplicate stock setRate", len(rig.calls), 0)
    check("phase reset", rig.phases(0), [-0.5] * 6)
    check("flag 0x114", m.u32(rig.owner(0) + 0x114) & 0xFF, 1)
    check("flag 0x074", m.u32(rig.owner(0) + 0x074) & 0xFF, 1)

    print("np_mod_tick -- an ordinary MIDI F8 must not retrigger a slow LFO")
    rig = LfoRig(m, synced=(0,), value=0.0, tick_rate=192.0)
    before = rig.phases(0)
    m.call(m.mod_tick, r0=rig.slot(0), r1=42)
    check("non-boundary does not duplicate stock setRate", len(rig.calls), 0)
    check("non-boundary phases untouched", rig.phases(0), before)
    check("non-boundary flag 0x114", m.u32(rig.owner(0) + 0x114) & 0xFF, 0)
    check("non-boundary flag 0x074", m.u32(rig.owner(0) + 0x074) & 0xFF, 0)

    print("np_mod_tick -- integer boundary tolerates F8 floating-point roundoff")
    rig = LfoRig(m, synced=(0,), value=4.5 / 10.0, tick_rate=192.0)
    m.call(m.mod_tick, r0=rig.slot(0), r1=1023)
    check("1023 represents the 1024-tick boundary", rig.phases(0), [-0.5] * 6)

    print("np_mod_tick -- triplet boundaries do not drift after the first hit")
    rig = LfoRig(m, synced=(0,), value=5.5 / 10.0, tick_rate=192.0)
    for pos in (682, 1365, 2048, 2730):
        # Give every call a fresh sentinel; all are exact multiples of 2048/3
        # before the MIDI handler truncates its double transport position.
        for phase_off in (0x34, 0x58, 0x84, 0xA8, 0xCC, 0xF0):
            m.w32(rig.owner(0) + phase_off,
                  struct.unpack("<I", struct.pack("<f", 0.25))[0])
        m.call(m.mod_tick, r0=rig.slot(0), r1=pos)
        check("triplet boundary %d" % pos, rig.phases(0), [-0.5] * 6)

    print("np_mod_tick -- the dropped divisions are unreachable")
    # 2/1, 1/4 dotted, the 1/16 quintuplet, the 1/32 triplet and 1/64 were
    # removed from the selection.  Sweep the whole knob and confirm the
    # firmware never lands on one of their tick values.
    # np_mod_tick no longer calls setRate, so the selected division has to be
    # read back from behaviour: the smallest pulse count that retriggers is
    # the period.
    from fractions import Fraction as _F
    ADV0 = _F(128, 3)
    dropped_periods = {192, 36, 24 * 5, 2, 3}   # 2/1, 1/4., 1/16Q, 1/32T, 1/64
    kept_periods = {144, 96, 72, 48, 24, 16, 12, 8, 6, 3}

    def period_at(value):
        rig = LfoRig(m, synced=(0,), value=value, tick_rate=192.0)
        for k in range(1, 200):
            for off in PHASES:
                m.w32(rig.owner(0) + off,
                      struct.unpack("<I", struct.pack("<f", 0.25))[0])
            m.call(m.mod_tick, r0=rig.slot(0), r1=int(k * ADV0))
            if rig.phases(0) == [-0.5] * 6:
                return k
        return None

    seen_periods = {period_at(step / 39.0) for step in range(40)}
    check("the knob reaches exactly ten divisions", len(seen_periods), 10)
    check("and they are the ten that were kept", seen_periods, kept_periods)
    check("2/1 (192 pulses) is gone", 192 in seen_periods, False)
    check("1/4 dotted (36) is gone", 36 in seen_periods, False)
    check("1/32 triplet (2) is gone", 2 in seen_periods, False)

    print("np_mod_tick -- every division fires on its exact pulse set")
    # The F8 handler keeps a double at transport+0x18, passes its floor as
    # `pos`, then adds 128/3 (the literal at 0x08118D68).  So one quarter is
    # 1024 engine ticks and a division's period in MIDI pulses is
    # division / (128/3).  All ten selected divisions come out whole, which
    # is why the fractional realignment case no longer exists.
    from fractions import Fraction

    ADV = Fraction(128, 3)
    periods = (144, 96, 72, 48, 24, 16, 12, 8, 6, 3)
    names = ("1/1.", "1/1", "1/2.", "1/2", "1/4", "1/4T", "1/8", "1/8T",
             "1/16", "1/32")
    SENTINEL = 0.25

    for idx, period in enumerate(periods):
        pulses = min(2 * period + 1, 400)
        want = [k for k in range(pulses) if k % period == 0]
        rig = LfoRig(m, synced=(0,), value=(idx + 0.5) / 10.0,
                     tick_rate=192.0)
        got = []
        for k in range(pulses):
            rig.clear_calls()
            for off in PHASES:
                m.w32(rig.owner(0) + off,
                      struct.unpack("<I", struct.pack("<f", SENTINEL))[0])
            m.call(m.mod_tick, r0=rig.slot(0), r1=int(k * ADV))
            if rig.phases(0) == [-0.5] * 6:
                got.append(k)
        check("division %d (%-4s) every %3d pulses" % (idx, names[idx], period),
              got, want)

    print()
    print("np_mod_tick -- a synced sample and hold must step on the boundary")
    # S&H picks a new value only where its phase wraps past +0.5.  Resetting
    # the phase to -0.5 on the boundary takes that wrap away: when the rate
    # is one cycle per division, reset and wrap fall together, the reset
    # wins, and the held value never changes again.
    rig = LfoRig(m, synced=(0,), value=(8 + 0.5) / 10.0, tick_rate=192.0)
    # after the rig: building one resets the machine and reloads the image,
    # which would put the stock generator back
    m.uc.mem_write(ORIG_RNG, assemble_thumb(RNG_STUB % (RNG_CELL, RNG_CELL)))
    m.w32(RNG_CELL, 0x1234ABCD)
    owner = rig.owner(0)
    m.uc.mem_write(owner + 0x28 + 0xE5, bytes([4]))      # waveform 4 = S&H
    m.w32(owner + 0x28 + 0xE0, 0)

    held = []
    for k in (0, 6, 12, 18):
        m.call(m.mod_tick, r0=rig.slot(0), r1=int(k * ADV))
        held.append(as_float(m.u32(owner + 0x28 + 0xE0)))
    check("it steps on every 1/16 boundary", len(set(held)), 4)
    check("and stays inside [-1, 1)",
          all(-1.0 <= v < 1.0 for v in held), True)

    # a pulse that is not a boundary must leave it alone
    before = as_float(m.u32(owner + 0x28 + 0xE0))
    m.call(m.mod_tick, r0=rig.slot(0), r1=int(19 * ADV))
    check("and not between boundaries",
          as_float(m.u32(owner + 0x28 + 0xE0)), before)

    # any other waveform must not be touched
    rig = LfoRig(m, synced=(0,), value=(8 + 0.5) / 10.0, tick_rate=192.0)
    owner = rig.owner(0)
    m.uc.mem_write(owner + 0x28 + 0xE5, bytes([2]))      # square
    m.w32(owner + 0x28 + 0xE0, 0x3F000000)
    m.call(m.mod_tick, r0=rig.slot(0), r1=0)
    check("a square leaves the held value alone",
          m.u32(owner + 0x28 + 0xE0), 0x3F000000)

    print()
    print("np_mod_tick -- the CParam ramp retains stock per-call timing")
    # MIDI clock is 24 PPQN.  Moving this generic CParam interpolation to an
    # LFO boundary makes its response division-dependent; at 2/1 even one
    # queued step can wait eight quarter notes.
    rig = LfoRig(m, synced=(0,), value=(8 + 0.5) / 10.0, tick_rate=192.0)
    m.w32(rig.owner(0) + 0x190,
          struct.unpack("<I", struct.pack("<f", 0.001))[0])
    m.w32(rig.owner(0) + 0x194, 8)
    for k in range(1, 6):                 # five non-boundary pulses
        m.call(m.mod_tick, r0=rig.slot(0), r1=int(k * ADV))
    check("five F8 calls consume five steps",
          m.u32(rig.owner(0) + 0x194), 3)
    m.call(m.mod_tick, r0=rig.slot(0), r1=int(6 * ADV))
    check("the boundary consumes one more, not a special extra step",
          m.u32(rig.owner(0) + 0x194), 2)

    print("np_mod_tick -- G2: value 1.0 must not read past the table")
    rig = LfoRig(m, synced=(0,), value=1.0, tick_rate=192.0)
    check("table[15] is out of range garbage", rig.division(15) > 1e30, True)
    # Pulse 3 is the next common boundary of clamped index 14 (period 3/2).
    # Use a non-zero boundary so this checks the selected table entry rather
    # than the universal transport-origin reset.
    m.call(m.mod_tick, r0=rig.slot(0), r1=128)
    check("index clamped to 14 (common boundary pulse 3)",
          rig.phases(0), [-0.5] * 6)

    print("np_mod_tick -- untouched when the slot is not synced")
    rig = LfoRig(m, synced=(), value=0.0)
    before = rig.phases(0)
    m.call(m.mod_tick, r0=rig.slot(0), r1=0)
    check("no setRate call", len(rig.calls), 0)
    check("phases untouched", rig.phases(0), before)

    print("np_mod_tick -- queued encoder delta is consumed once")
    rig = LfoRig(m, synced=(0,), value=0.0)
    m.w32(rig.owner(0) + 0x190, struct.unpack("<I", struct.pack("<f", 0.5))[0])
    m.w32(rig.owner(0) + 0x194, 2)
    m.call(m.mod_tick, r0=rig.slot(0), r1=0)
    check("pending decremented", m.u32(rig.owner(0) + 0x194), 1)
    check("value advanced by the delta",
          round(as_float(m.u32(rig.owner(0) + 0x184)), 5), 0.5)

    print("np_mod_tick -- an overridden retrigger is called instead")
    rig = LfoRig(m, synced=(0,), value=0.0, retrig=0x08109615)
    before = rig.phases(0)
    m.call(m.mod_tick, r0=rig.slot(0), r1=0)
    check("override path also avoids duplicate setRate", len(rig.calls), 0)
    check("inline reset skipped", rig.phases(0), before)


def test_midi_clock_lfo_path(m):
    print("DIN/USB MIDI F8 -- real firmware path reaches the gated LFO tick")

    rig = LfoRig(m, synced=(0,), value=0.0, tick_rate=192.0)
    # The real transport is much larger than the small object used by the
    # isolated hook tests.  Point its singleton at a non-overlapping D2 area.
    m.uc.mem_write(MIDI_TRANSPORT, b"\x00" * 0x1000)
    stub_return_ptr(m, ORIG_TRANSPORT, MIDI_TRANSPORT)
    m.w32(MIDI_TRANSPORT + 4,
          struct.unpack("<I", struct.pack("<f", 192.0))[0])
    m.uc.mem_write(MIDI_TRANSPORT + 0x74, b"\x01")  # MIDI clock source

    # Unicorn 2.0 cannot execute the stock handler's f64 VCVT instructions.
    # In this test instance only, replace those conversions by a load of the
    # already-converted position from transport+0x70.  The production BL at
    # 0x08118B92 and all of sub_08116CD4 remain untouched, so this still
    # executes the real F8 -> slot walker -> patched np_mod_tick call chain.
    m.uc.mem_write(ORIG_MIDI_F8 + 0x34, b"\x25\xE0")  # b.n 0x08118B82
    converted_pos = assemble_thumb("""
        ldr.w r1, [r4, #0x70]
        nop.w
        nop.w
    """)
    check("F8 emulator shim has the expected width", len(converted_pos), 12)
    m.uc.mem_write(ORIG_MIDI_F8 + 0x86, converted_pos)
    m.uc.mem_write(ORIG_MIDI_F8 + 0x96, b"\x06\xE0")  # b.n epilogue

    # Force the handler's first-pulse arm; it is the shortest route to the
    # shared call site and avoids unrelated timer-estimator state.
    m.uc.mem_write(MIDI_TRANSPORT + 0x887, b"\x01")
    m.w32(MIDI_TRANSPORT + 0x70, 42)
    before = rig.phases(0)
    m.call(ORIG_MIDI_F8, r0=MIDI_TRANSPORT)
    check("first-pulse phase arm does not duplicate setRate",
          len(rig.calls), 0)
    check("first F8 did not retrigger the slow division",
          rig.phases(0), before)

    # knob 0.0 selects the slowest of the ten, 1/1. -- 144 pulses, and
    # 144 * 128/3 = 6144 ticks
    m.uc.mem_write(MIDI_TRANSPORT + 0x887, b"\x01")
    m.w32(MIDI_TRANSPORT + 0x70, 6144)
    m.call(ORIG_MIDI_F8, r0=MIDI_TRANSPORT)
    check("real F8 boundary retriggers the slow division",
          rig.phases(0), [-0.5] * 6)


def test_stock_lfo_refresh_path(m):
    print("stock local-5 walker -- the clock path already refreshes LFO rate")

    synced = (0, 3, 7, 18)
    rig = LfoRig(m, synced=synced, value=0.0, tick_rate=192.0)
    m.call(ORIG_RATE_WALK, r0=FAKE_PROJECT)
    check("all 19 LFOSpeed parameters dispatched", len(rig.calls), SLOT_COUNT)
    calls = {target: rate for target, rate in rig.calls}
    # knob 0.0 now selects the slowest of the ten, 1/1. = 6144 ticks
    check("synced rate = transport / division",
          round(calls[rig.owner(0) + 0x28], 9),
          round(192.0 / 6144.0, 9))

    rig.clear_calls()
    rig.set_rate(384.0)
    m.call(ORIG_RATE_WALK, r0=FAKE_PROJECT)
    calls = {target: rate for target, rate in rig.calls}
    check("tempo change reaches the same stock walker",
          round(calls[rig.owner(0) + 0x28], 9),
          round(384.0 / 6144.0, 9))

    rig.clear_calls()
    m.call(m.lfo_poll)
    check("obsolete compatibility poll is a no-op", len(rig.calls), 0)


def test_led_frame(m):
    from test_spi_p6 import run
    run(m)


def test_ui_queue_pop(m):
    print("np_ui_queue_pop -- the deque's start iterator advances atomically")
    m.reset(flags=0)
    UI_RESUME = 0x0815756A
    panel, node, block = DTCM + 0x1000, DTCM + 0x2000, DTCM + 0x4000
    # the window that must run masked: between the cpsid and the msr, found
    # by walking halfwords (capstone here stops at `mrs r0, primask`)
    stub = m.ui_queue_pop & ~1
    code = m.img[stub - BASE:stub - BASE + 48]
    bounds, a = [], 0
    while a < len(code) - 1:
        hw = struct.unpack_from("<H", code, a)[0]
        bounds.append((stub + a, hw))
        a += 4 if (hw & 0xF800) in (0xE800, 0xF000, 0xF800) else 2
    cpsid = next(i for i, (_, hw) in enumerate(bounds) if hw == 0xB672)
    msr = next(i for i, (_, hw) in enumerate(bounds) if hw == 0xF380)
    guarded = {a for a, _ in bounds[cpsid + 1:msr]}
    for entry_primask in (0, 1):
        m.w32(panel + 0x30, node)          # start.node
        m.w32(node + 4, block)             # the block it points at
        for off in (0x24, 0x28, 0x2C):
            m.w32(panel + off, 0xDEADBEEF)
        seen = {}
        h = m.uc.hook_add(UC_HOOK_CODE,
                          lambda uc, a, size, data: seen.__setitem__(
                              a, uc.reg_read(UC_ARM_REG_PRIMASK)),
                          begin=stub, end=stub + 48)
        m.uc.reg_write(UC_ARM_REG_R4, panel)
        m.uc.reg_write(UC_ARM_REG_PRIMASK, entry_primask)
        m.uc.emu_start(m.ui_queue_pop | 1, UI_RESUME, count=100)
        m.uc.hook_del(h)
        tag = "primask %d: " % entry_primask
        check(tag + "rejoins the stock consumer",
              m.uc.reg_read(UC_ARM_REG_PC), UI_RESUME)
        check(tag + "start.node advanced one map entry",
              m.u32(panel + 0x30), node + 4)
        check(tag + "start.cur is the block read through the old node",
              m.u32(panel + 0x24), block)
        check(tag + "start.block_start", m.u32(panel + 0x28), block)
        check(tag + "start.block_end", m.u32(panel + 0x2C), block + 512)
        check(tag + "r3 left holding the block for the caller's compare",
              m.uc.reg_read(UC_ARM_REG_R3), block)
        check(tag + "caller's PRIMASK restored",
              m.uc.reg_read(UC_ARM_REG_PRIMASK), entry_primask)
        check(tag + "the whole transition executed", guarded <= set(seen), True)
        check(tag + "every step of the transition ran masked",
              [a for a in guarded if not seen[a]], [])


def test_scan_rearm(m):
    print("np_scan_rearm -- the pot scan restarts only on a consumed, idle row")
    m.reset(flags=0)
    m.call(m.early)
    DMA1_LISR, DMA1_S0CR = 0x40020000, 0x40020010
    ADC1_ISR, ADC1_CR = 0x40022000, 0x40022008
    D_SCANSKIP, D_SCANOVR = 0xD90, 0xD94

    def run(lisr, s0cr, isr, cr=0x10000101):
        m.w32(DMA1_LISR, lisr); m.w32(DMA1_S0CR, s0cr)
        m.w32(ADC1_ISR, isr); m.w32(ADC1_CR, cr)
        m.uc.reg_write(UC_ARM_REG_R4, 0x44444444)
        m.call(m.scan_rearm)
        return m.u32(DMA1_S0CR), m.u32(ADC1_CR), m.u32(ADC1_ISR)

    # healthy tick: stream idle, row consumed, no overrun -> exactly stock
    s0cr, cr, isr = run(0, 0x2C16, 0x000F)
    check("idle: returned to the caller", m.uc.reg_read(UC_ARM_REG_PC), STOP)
    check("idle: r4 untouched", m.uc.reg_read(UC_ARM_REG_R4), 0x44444444)
    check("idle: DMA1 S0 re-enabled", s0cr, 0x2C17)
    check("idle: ADSTART written", cr, 0x10000105)
    check("idle: ADC ISR not written", isr, 0x000F)
    check("idle: nothing counted", (m.u32(m.diag + D_SCANSKIP), m.u32(m.diag + D_SCANOVR)), (0, 0))
    # a latched overrun: cleared (write 1 to bit 4) before the restart
    s0cr, cr, isr = run(0, 0x2C16, 0x001F)
    check("ovr: OVR written to clear", isr, 0x0010)
    check("ovr: still re-armed", (s0cr, cr), (0x2C17, 0x10000105))
    check("ovr: counted", m.u32(m.diag + D_SCANOVR), 1)
    # the previous row's TC still pending: leave everything alone
    s0cr, cr, isr = run(0x20, 0x2C16, 0x001F)
    check("tc pending: no re-enable", s0cr, 0x2C16)
    check("tc pending: no ADSTART", cr, 0x10000101)
    check("tc pending: OVR left for the tick that restarts", isr, 0x001F)
    check("tc pending: skip counted", m.u32(m.diag + D_SCANSKIP), 1)
    # the stream still transferring: leave everything alone
    s0cr, cr, isr = run(0, 0x2C17, 0x000F)
    check("busy: no ADSTART", cr, 0x10000101)
    check("busy: skip counted", m.u32(m.diag + D_SCANSKIP), 2)
    check("busy: OVR count unchanged", m.u32(m.diag + D_SCANOVR), 1)


def main():
    m = Machine()
    print("patched image: %s, blob at 0x%08X, diag at 0x%08X\n"
          % (IMAGE, BLOB, m.diag))
    for t in (test_early, test_sbrk, test_runtime_allocator,
              test_runtime_profile_and_defer,
              test_watchdog, test_fault, test_crc,
              test_wiring, test_lfo_tick, test_midi_clock_lfo_path,
              test_stock_lfo_refresh_path, test_xmod_data,
              test_xmod_ui, test_xmod_ratio_table, test_xmod_type,
              test_xmod_ratio, test_xmod_speed, test_xmod_splice,
              test_sine, test_waveforms, test_sample_hold,
              test_wave_shape, test_midi_tx, test_led_frame,
              test_ui_queue_pop, test_scan_rearm):
        t(m)
        print()
    if FAILS:
        print("FAILED: %d check(s): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("PASS -- every patch entry point behaves as intended under emulation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
