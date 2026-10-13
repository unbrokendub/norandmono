#!/usr/bin/env python3
"""Whole-firmware emulation: boot, main loop, interrupts, panel, LEDs.

Runs the real image from Reset_Handler through the static constructors and
main, then slices the main loop between software-injected interrupts
(SysTick 4 kHz, EXTI2 audio 32 kHz, DMA1_Stream0 key scan) at instruction-
count boundaries.  Peripherals are RAM with a few modelled registers (RCC
ready bits, PWR VOSRDY, polled status registers discovered by the boot).
Double precision comes from fp64_shim.  Keys are pressed through the real
matrix scanner's GPIO read; LED frames are captured at the LED SPI data
register.  Nothing in the image is modified except explicit stubs listed in
STUBS.
"""
import struct
import random
import collections
import time
from pathlib import Path
from unicorn import *
from unicorn.arm_const import *
from fp64_shim import Fp64Shim

BASE = 0x08100000
STOP = 0x00001000
RCC = 0x58024400
PWR = 0x58024800
SYSTICK_HZ = 4000
CORE_HZ = 400_000_000
INSTR_PER_TICK = CORE_HZ // SYSTICK_HZ // 2      # ~2 instructions per cycle is optimistic; keep ticks frequent
VEC_SYSTICK = 15
VEC_EXTI2 = 16 + 8
VEC_DMA1_S0 = 16 + 11
VEC_TIM5 = 16 + 50
VEC_USART1 = 16 + 37
UI = 0x2000DE54
DWT_CYCCNT = 0xE0001004

# functions replaced by an immediate return in emulation (address: why)
STUBS = {
    0x081029C0: 'HAL_GPIO_ReadPin when called by the key scanner -> synthesized panel',
    0x08119B08: 'Error_Handler (bkpt) -> recorded and skipped',
    0x08104F00: 'timer clock frequency -> 240 MHz constant',
}


QSPI = 0x52005000
QSPI_MEM = 0x90000000
QSPI_SIZE = 0x1000000


class Qspi:
    """Just enough QUADSPI + NOR flash: indirect read/write/erase against a
    16 MB array that also backs the memory-mapped window at 0x90000000.
    Erased flash reads 0xFF.  JEDEC ID answers as the IS25LP128 the driver checks for."""
    def __init__(self, uc):
        self.uc = uc
        uc.mem_write(QSPI_MEM, b'\xFF' * QSPI_SIZE)
        # The 512 KB the loader reads at boot is an event journal: a byte
        # of event id followed by that event's payload, replayed through the
        # registry (build/event-ids.json).  A fresh unit needs SOF (254)
        # then EOF (9); 0xFF has no factory and the loader aborts, zeros
        # never reach EOF.  Header page zeroed, then the two-event journal.
        uc.mem_write(QSPI_MEM, b'\x00' * 0x100 + bytes([254, 9]) + b'\xFF' * (0x80000 - 0x102))
        self.ccr = 0; self.ar = 0; self.dlr = 0
        self.rx = b''; self.rxpos = 0
        self.wpos = 0
        self.wel = False
        self.log = collections.Counter()

    # erase opcodes and sizes: the driver uses ISSI's 0xD7 for a 4 KB sector
    # (an earlier revision knew only 0x20/0x52/0xD8, so a save programmed
    # over unerased data); 0x21/0x5C/0xDC are the 4-byte-address forms
    ERASE = {0x20: 0x1000, 0xD7: 0x1000, 0x21: 0x1000, 0x52: 0x8000, 0x5C: 0x8000,
             0xD8: 0x10000, 0xDC: 0x10000}

    def fmode(self): return (self.ccr >> 26) & 3
    def instruction(self): return self.ccr & 0xFF

    def start(self):
        ins = self.instruction()
        self.log[ins] += 1
        n = self.dlr + 1
        if self.fmode() == 1:                      # indirect read
            if ins == 0x9F:   self.rx = bytes([0x9D, 0x60, 0x18, 0, 0, 0])   # IS25LP128, what the driver expects
            elif ins == 0x05: self.rx = bytes([0x02 if self.wel else 0x00]) * n
            elif ins == 0x35: self.rx = bytes([0x02]) * n
            elif ins == 0x15: self.rx = bytes([0x00]) * n
            else:             self.rx = bytes(self.uc.mem_read(QSPI_MEM + (self.ar % QSPI_SIZE), n))
            self.rxpos = 0
        elif self.fmode() == 0:                    # indirect write / command
            self.wpos = 0
            if ins == 0x06: self.wel = True
            elif ins == 0x04: self.wel = False
            elif ins in self.ERASE:
                size = self.ERASE[ins]
                a = (self.ar % QSPI_SIZE) & ~(size - 1)
                self.uc.mem_write(QSPI_MEM + a, b'\xFF' * size)
            elif ins in (0xC7, 0x60):
                self.uc.mem_write(QSPI_MEM, b'\xFF' * QSPI_SIZE)

    def write(self, addr, size, value):
        off = addr - QSPI
        if off == 0x14:
            self.ccr = value
            if ((value >> 10) & 3) == 0:
                self.start()                       # no address phase: starts now
        elif off == 0x18:
            self.ar = value
            self.start()
        elif off == 0x10:
            self.dlr = value
        elif off == 0x20 and self.fmode() == 0:
            data = value.to_bytes(size, 'little')
            a = (self.ar + self.wpos) % QSPI_SIZE
            old = bytes(self.uc.mem_read(QSPI_MEM + a, size))
            self.uc.mem_write(QSPI_MEM + a, bytes(o & d for o, d in zip(old, data)))
            self.wpos += size

    def read(self, addr, size):
        off = addr - QSPI
        if off == 0x08:
            return 0x0E | (min(len(self.rx) - self.rxpos, 31) << 8)   # TCF|FTF|SMF, FLEVEL
        if off == 0x20 and self.fmode() == 1:
            chunk = self.rx[self.rxpos:self.rxpos + size]
            self.rxpos += size
            chunk = chunk + b'\xFF' * (size - len(chunk))
            return int.from_bytes(chunk, 'little')
        return None


class Mono:
    def __init__(self, image='build/firmware-patched.bin', verbose=False):
        self.img = Path(image).read_bytes()
        self.verbose = verbose
        uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        uc.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M7)
        self.uc = uc
        for b, s in ((0, 0x10000), (0x08000000, 0x200000), (0x20000000, 0x20000),
                     (0x24000000, 0x80000), (0x30000000, 0x50000), (0x38000000, 0x10000),
                     (0x40000000, 0x10000000), (0x50000000, 0x10000000),
                     (0x90000000, 0x1000000), (0xE0000000, 0x100000),
                     (0x08200000, 0x100000)):   # past the end of flash: absorbs runaway writes
            uc.mem_map(b, s)
        # erased flash reads 0xFF: everything past the image, and bank 1
        uc.mem_write(0x08000000, b'\xFF' * 0x200000)
        uc.mem_write(BASE, self.img)
        # Flash: the bootloaders (bank 1 below 0x08040000) and the
        # application (bank 2 below the settings sector at 0x081E0000) are
        # read-only -- with blank data some paths compute sector 0 and would
        # overwrite code in a RAM-backed mapping; such writes are dropped.
        # The project journal lives in bank 1 from 0x08040000 (the format
        # routine erases those sectors and programs the event bytes there,
        # then copies them back into the 0x24000000 store) and the settings
        # sector is 0x081E0000: both stay writable, RAM semantics.
        uc.mem_protect(0x08000000, 0x40000, UC_PROT_READ | UC_PROT_EXEC)
        uc.mem_protect(0x08100000, 0xE0000, UC_PROT_READ | UC_PROT_EXEC)
        self.dropped_flash_writes = 0
        def drop(uc, access, addr, size, value, data):
            self.dropped_flash_writes += 1
            return True
        uc.hook_add(UC_HOOK_MEM_WRITE_PROT, drop)
        uc.mem_write(0xE000ED88, struct.pack('<I', 0x00F00000))
        uc.mem_write(STOP, b'\x70\x47')
        self.shim = Fp64Shim(uc, self.img, BASE)
        self.rules = {PWR + 0x18: 0xFFFFFFFF,
                      0x40020004: 0x20 << 6}   # DMA1 HISR: stream 4 complete (harmless)
        # DMA1 stream 0 is the pot scan's ADC transfer: SysTick sets EN, the
        # three conversions land at once here, TCIF0 goes up, and the stream
        # handler takes it down through LIFCR.  np_scan_rearm reads both.
        self.dma1_s0_tc = False
        try:
            import json
            self.rules.update({int(k, 16): v for k, v in
                               json.load(open(Path(__file__).with_name('sysemu_rules.json'))).items()})
        except FileNotFoundError:
            pass
        self.dma_starts = []
        self.toggle_calls = 0
        self.led_bytes = []
        self.led_rows = []
        self.pots = (0x800, 0x800, 0x800)
        # the 23 front-panel pots (index = the scanner's pot number, table
        # 0x0815E0D0 maps (row, column) to it).  The DMA frame carries 16-bit
        # samples (the scanner averages 32 of them, inverts and normalises by
        # 65536), so a pot's position is 0..0xFFFF here; 0x8000 is mid-travel.
        # Earlier revisions of this model masked the sample to 12 bits, which
        # confined every "sweep" to the top 6 % of the parameter's range
        # (docs/ui-amount-audit.md).
        self.pot_values = [0x8000] * 24
        self.POT_ROWS = [list(self.img[0x5E0D0 + r * 3:0x5E0D0 + r * 3 + 3]) for r in range(8)]
        uc.hook_add(UC_HOOK_CODE, self.pot_row, begin=0x0810F0F4, end=0x0810F0F4)
        self.rng = random.Random(0x4E4F5241)
        self.tim5_acc = 0.0            # fractional TIM5 ticks owed
        self.qspi = Qspi(uc)
        self.reads = collections.Counter()
        self.pressed = set()
        self.log = []
        self.vectors = {i: struct.unpack_from('<I', self.img, i * 4)[0] for i in range(16 + 150)}
        uc.hook_add(UC_HOOK_MEM_READ, self.on_read, begin=0x40000000, end=0x60000000)
        uc.hook_add(UC_HOOK_MEM_READ, self.on_read_cyccnt,
                    begin=DWT_CYCCNT, end=DWT_CYCCNT + 3)
        uc.hook_add(UC_HOOK_MEM_WRITE, self.on_write, begin=0x40000000, end=0x60000000)
        uc.hook_add(UC_HOOK_CODE, self.gpio_read, begin=0x081029C0, end=0x081029C0)
        # boot waits for flags that interrupts set on hardware; satisfy them
        for a in (0x0811510A, 0x08115202):
            uc.hook_add(UC_HOOK_CODE, self.adc_first_frame, begin=a, end=a)
        # timer clock query: the RCC is not modelled deeply enough to answer
        def timer_clock(uc, address, size, data):
            uc.reg_write(UC_ARM_REG_R0, 240_000_000)
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
        uc.hook_add(UC_HOOK_CODE, timer_clock, begin=0x08104F00, end=0x08104F00)
        # the project manager's ToggleModNoteMode handler
        uc.hook_add(UC_HOOK_CODE, lambda uc, a, s, d: setattr(self, 'toggle_calls', self.toggle_calls + 1),
                    begin=0x0812CF88, end=0x0812CF88)
        # every project event enqueued (0x08157578: r1 -> pointer to event)
        self.events = []
        def submit(uc, a, s, d):
            try:
                ev = struct.unpack('<I', bytes(uc.mem_read(uc.reg_read(UC_ARM_REG_R1), 4)))[0]
                vt = struct.unpack('<I', bytes(uc.mem_read(ev, 4)))[0]
            except UcError:
                vt = 0
            self.events.append(vt)
        uc.hook_add(UC_HOOK_CODE, submit, begin=0x08157578, end=0x08157578)
        # every UI callback dispatched (0x08155D14: r3 = callback, r1 -> event data)
        self.callbacks = []
        uc.hook_add(UC_HOOK_CODE, lambda uc, a, s, d: self.callbacks.append(uc.reg_read(UC_ARM_REG_R3)),
                    begin=0x08155D14, end=0x08155D14)
        # any construction of a ToggleModNoteMode event (its ctor), with the caller
        self.toggle_ctor = []
        uc.hook_add(UC_HOOK_CODE, lambda uc, a, s, d: self.toggle_ctor.append(uc.reg_read(UC_ARM_REG_LR)),
                    begin=0x0812E2EC, end=0x0812E2EC)
        # Error_Handler is `bkpt; bx lr`: record who called it and return
        self.errors = []
        uc.hook_add(UC_HOOK_CODE, self.error_handler, begin=0x08119B08, end=0x08119B08)
        self.instructions = 0
        self.slices = 0                # step() slices run: 250 us each
        self.din_bytes = []            # bytes written to USART1 TDR (DIN out)
        self.usart_acc = 0.0           # fractional DIN byte times owed
        self.slice_hooks = []          # called after every slice (models)
        self.cyccnt_reads = 0
        self.in_isr = 0

    # ---- peripherals ------------------------------------------------------
    def on_read(self, uc, access, addr, size, value, data):
        self.reads[addr] += 1
        if QSPI <= addr < QSPI + 0x40:
            v = self.qspi.read(addr, size)
            if v is not None:
                uc.mem_write(addr, v.to_bytes(size, 'little'))
            return
        if addr == RCC:
            v = struct.unpack('<I', bytes(uc.mem_read(addr, 4)))[0]
            for on, rdy in ((0, 2), (7, 8), (12, 13), (16, 17), (24, 25), (26, 27), (28, 29)):
                v = (v | (1 << rdy)) if v & (1 << on) else (v & ~(1 << rdy))
            uc.mem_write(addr, struct.pack('<I', v))
        elif addr == RCC + 0x10:
            v = struct.unpack('<I', bytes(uc.mem_read(addr, 4)))[0]
            uc.mem_write(addr, struct.pack('<I', (v & ~0x38) | ((v & 7) << 3)))
        elif addr in self.rules:
            uc.mem_write(addr, struct.pack('<I', self.rules[addr]))
        elif (addr & 0xFFFFFEFF) in (0x40022008, 0x58026008):   # ADC_CR: self-clearing control bits
            v = struct.unpack('<I', bytes(uc.mem_read(addr, 4)))[0]
            uc.mem_write(addr, struct.pack('<I', v & ~((1 << 31) | 0x3E)))
        elif (addr & 0xFFFFFEFF) in (0x40022000, 0x58026000):   # ADC_ISR: ready, conversions done
            uc.mem_write(addr, struct.pack('<I', 0x0F))
        elif addr == 0x48021808:             # RNG_DR: the main loop reseeds xorshift from it every
            # iteration, and sample-and-hold draws on that state -- without
            # entropy here S&H is stuck at one value and never exercises the
            # random paths (a fixed seed keeps runs reproducible)
            uc.mem_write(addr, struct.pack('<I', self.rng.getrandbits(32)))
        elif addr == 0x48021804:             # RNG_SR: data ready, no errors
            uc.mem_write(addr, struct.pack('<I', 1))
        elif addr == 0x40020000:             # DMA1 LISR: TCIF0 as modelled
            uc.mem_write(addr, struct.pack('<I', 0x20 if self.dma1_s0_tc else 0))
        elif addr == 0x40020010:             # DMA1 S0CR: a transfer is instantaneous,
            # so EN never reads back set
            v = struct.unpack('<I', bytes(uc.mem_read(addr, 4)))[0]
            uc.mem_write(addr, struct.pack('<I', v & ~1))
        elif addr == 0x40013400:             # SPI4 CR1: the three-byte LED
            # frame is instantaneous here, so CSTART is always clear by the
            # time the writer reads CR1 back to set it again
            v = struct.unpack('<I', bytes(uc.mem_read(addr, 4)))[0]
            uc.mem_write(addr, struct.pack('<I', v & ~0x200))
        elif addr == 0x40010024:             # TIM1 CNT: the microsecond within the
            # millisecond that 0x081479A4 adds to the tick count -- free-running
            # at one count per 400 emulated instructions so time-budgeted
            # loops (the step-list service 0x0815888C) terminate inside a slice
            uc.mem_write(addr, struct.pack('<I', (self.instructions // 400) % 1000))

    def on_read_cyccnt(self, uc, access, addr, size, value, data):
        """DWT CYCCNT.  The patch times the audio ISR with it and np_led_frame
        spaces the LED SPI frames with it, so it has to advance -- a frozen
        counter would hold every LED frame back forever.  Slice boundaries
        carry the bulk of the time (two cycles per emulated instruction, the
        same ratio as INSTR_PER_TICK); each read adds a few cycles on top so
        that a spin loop reading nothing else still makes progress."""
        self.cyccnt_reads += 1
        v = (self.instructions * 2 + self.cyccnt_reads * 8) & 0xFFFFFFFF
        uc.mem_write(DWT_CYCCNT, struct.pack('<I', v))

    def on_write(self, uc, access, addr, size, value, data):
        if addr == 0x40013420:               # SPI4 TXDR: the LED shift registers
            self.led_bytes.append(value & 0xFF)
            return
        if addr == 0x40011028:               # USART1 TDR: a DIN MIDI byte on the wire
            self.din_bytes.append(value & 0xFF)
            return
        if QSPI <= addr < QSPI + 0x40:
            self.qspi.write(addr, size, value)
            return
        if addr == 0x40020008 and (value & 0x20):   # DMA1 LIFCR: CTCIF0
            self.dma1_s0_tc = False
            return
        if addr == 0x40020010 and (value & 1):        # DMA1 S0CR EN: pot row done
            self.dma1_s0_tc = True
        # DMA stream start: CR write with EN set -> capture the buffer (LED
        # frames and CV frames both travel this way)
        for dma in (0x40020000, 0x40020400):
            off = addr - dma - 0x10
            if 0 <= off < 8 * 0x18 and off % 0x18 == 0 and (value & 1):
                stream = off // 0x18
                base = addr
                ndtr = struct.unpack('<I', bytes(uc.mem_read(base + 4, 4)))[0]
                par = struct.unpack('<I', bytes(uc.mem_read(base + 8, 4)))[0]
                m0ar = struct.unpack('<I', bytes(uc.mem_read(base + 12, 4)))[0]
                psize = (value >> 11) & 3
                width = (1, 2, 4)[psize] if psize < 3 else 1
                try:
                    payload = bytes(uc.mem_read(m0ar, min(ndtr * width, 4096)))
                except UcError:
                    payload = b''
                self.dma_starts.append((dma, stream, par, m0ar, ndtr, payload))

    def pot_row(self, uc, address, size, data):
        # scanner about to read the three pot readings of row r1 from its
        # ADC frame buffer (scan+0x6c): supply the pots' current positions
        row = uc.reg_read(UC_ARM_REG_R1) & 7
        buf = struct.unpack('<I', bytes(uc.mem_read(uc.reg_read(UC_ARM_REG_R0) + 0x6C, 4)))[0]
        uc.mem_write(buf, struct.pack('<3H', *(self.pot_values[i] & 0xFFFF for i in self.POT_ROWS[row])))

    def adc_first_frame(self, uc, address, size, data):
        # 0x08115050 / 0x08115174 wait for the pot ADC's first DMA frame: obj+0x718
        uc.mem_write(uc.reg_read(UC_ARM_REG_R4) + 0x718, b'\x01')

    def error_handler(self, uc, address, size, data):
        self.errors.append(uc.reg_read(UC_ARM_REG_LR))
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def gpio_read(self, uc, address, size, data):
        # HAL_GPIO_ReadPin, only when the matrix scanner calls it (return
        # address 0x0810F620): r5 holds the key index.  Other callers read
        # the zeroed GPIO registers as they would.
        if (uc.reg_read(UC_ARM_REG_LR) & ~1) != 0x0810F620:
            return
        key = uc.reg_read(UC_ARM_REG_R5)
        uc.reg_write(UC_ARM_REG_R0, int(key in self.pressed))
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    # ---- execution --------------------------------------------------------
    def run(self, count, until=0):
        uc = self.uc
        pc = uc.reg_read(UC_ARM_REG_PC) | 1
        uc.emu_start(pc, until, count=count)
        self.instructions += count

    def call(self, address, r0=0, r1=0, count=50_000_000):
        uc = self.uc
        uc.reg_write(UC_ARM_REG_R0, r0); uc.reg_write(UC_ARM_REG_R1, r1)
        uc.reg_write(UC_ARM_REG_LR, STOP | 1)
        uc.emu_start(address | 1, STOP, count=count)
        assert uc.reg_read(UC_ARM_REG_PC) == STOP, 'instruction limit in call %08x at %08x' % (address, uc.reg_read(UC_ARM_REG_PC))
        return uc.reg_read(UC_ARM_REG_R0)

    REGS = ([globals()['UC_ARM_REG_R%d' % i] for i in range(13)] +
            [UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_APSR, UC_ARM_REG_FPSCR,
             UC_ARM_REG_IPSR, UC_ARM_REG_PRIMASK] +
            [globals()['UC_ARM_REG_D%d' % i] for i in range(16)])

    def inject(self, vector, count=20_000_000, target=None, r0=0):
        """Run an exception handler as a software interrupt: the interrupted
        context is snapshotted and restored around a plain call, IPSR carries
        the vector so ISR-context checks see an interrupt.  `target` runs
        another function in that context instead of the vector's handler
        (with r0), e.g. a HAL callback the model completes by hand."""
        uc = self.uc
        if uc.reg_read(UC_ARM_REG_PRIMASK) & 1:
            return False                     # masked: deliver later
        saved = [uc.reg_read(r) for r in self.REGS]
        # the basic exception frame the hardware would stack: r0-r3, r12,
        # lr, the interrupted pc and xPSR (its IPSR = the interrupted
        # context, thread mode here) -- the patch's SysTick sampler reads
        # pc and xPSR from it
        frame = struct.pack('<8I', *[uc.reg_read(r) & 0xFFFFFFFF for r in
                                     (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
                                      UC_ARM_REG_R12, UC_ARM_REG_LR)],
                            uc.reg_read(UC_ARM_REG_PC) & ~1,
                            (uc.reg_read(UC_ARM_REG_APSR) & 0xF8000000) | (1 << 24) | self.in_isr)
        uc.reg_write(UC_ARM_REG_IPSR, vector)
        sp = (uc.reg_read(UC_ARM_REG_SP) & ~7) - 32
        uc.mem_write(sp, frame)
        uc.reg_write(UC_ARM_REG_SP, sp)
        self.in_isr = vector
        try:
            self.call((self.vectors[vector] if target is None else target) & ~1, r0, count=count)
        finally:
            self.in_isr = 0
            for r, v in zip(self.REGS, saved):
                uc.reg_write(r, v)
        return True

    def run_unmasked(self, limit=2_000_000):
        """Step the main thread until it is outside every PRIMASK critical
        section.  Calling firmware from inside one (or snapshotting there)
        corrupts the stock malloc lock's saved-PRIMASK word, after which
        interrupts stay masked for good and the emulated unit looks dead:
        no SysTick, no LEDs, no keys, main loop still spinning."""
        uc = self.uc
        n = 0
        while (uc.reg_read(UC_ARM_REG_PRIMASK) & 1) and n < limit:
            self.run(100)
            n += 100
        assert not (uc.reg_read(UC_ARM_REG_PRIMASK) & 1), 'main thread never left its critical section'

    def call_preserving(self, address, r0=0, r1=0, s0=None, count=20_000_000):
        """Call a firmware function from the harness without disturbing the
        interrupted main-loop context (registers are restored afterwards)."""
        uc = self.uc
        self.run_unmasked()
        saved = [uc.reg_read(r) for r in self.REGS]
        if s0 is not None:
            uc.reg_write(UC_ARM_REG_S0, s0)
        try:
            return self.call(address, r0, r1, count=count)
        finally:
            for r, v in zip(self.REGS, saved):
                uc.reg_write(r, v)

    def boot(self, limit=300_000_000):
        """Reset_Handler through constructors into main until the main loop
        is reached.  HAL_Delay spins on HAL_GetTick, so whenever the spin is
        seen emulation stops, a SysTick is injected and it resumes -- hooks
        cannot start nested emulation themselves."""
        uc = self.uc
        uc.reg_write(UC_ARM_REG_SP, self.vectors[0])
        uc.reg_write(UC_ARM_REG_PC, self.vectors[1])
        state = {'why': None, 'spins': 0}
        UWTICK = 0x20000174
        def delay_loop(uc, address, size, data):
            # HAL_Delay spins on HAL_GetTick: advance uwTick a millisecond per
            # spin (the SysTick handler itself is injected only once the main
            # loop runs; during boot nothing else needs its side effects)
            state['spins'] += 1
            t = struct.unpack('<I', bytes(uc.mem_read(UWTICK, 4)))[0] + 1
            uc.mem_write(UWTICK, struct.pack('<I', t))
        def at_main(uc, address, size, data):
            state['why'] = 'main'; uc.emu_stop()
        h = uc.hook_add(UC_HOOK_CODE, delay_loop, begin=0x08100466, end=0x08100466)
        h2 = uc.hook_add(UC_HOOK_CODE, at_main, begin=0x08119A02, end=0x08119A02)
        t = time.time()
        uc.emu_start(self.vectors[1] | 1, 0, count=limit)
        uc.hook_del(h); uc.hook_del(h2)
        assert state['why'] == 'main', 'main loop not reached, pc=%08x' % uc.reg_read(UC_ARM_REG_PC)
        if self.verbose:
            print('booted to main loop in %.1fs, %d fp64 ops, unhandled %s' % (
                time.time() - t, self.shim.count, self.shim.unhandled[:3]))
        return self

    RAM = ((0x20000000, 0x20000), (0x24000000, 0x80000), (0x30000000, 0x50000),
           (0x38000000, 0x10000), (0x081E0000, 0x20000), (0x90000000, 0x80000))

    # Everything a snapshot carries by name.  Raw Unicorn contexts are NOT
    # used: they hold host pointers, and pushing bytes pickled by another
    # process through uc_context_restore segfaults the interpreter (seen
    # as intermittent SIGSEGV/SIGABRT in the scenario runs).
    SNAP_REGS = ([globals()['UC_ARM_REG_R%d' % i] for i in range(13)] +
                 [UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_APSR, UC_ARM_REG_FPSCR,
                  UC_ARM_REG_IPSR, UC_ARM_REG_PRIMASK, UC_ARM_REG_MSP, UC_ARM_REG_PSP,
                  UC_ARM_REG_CONTROL, UC_ARM_REG_FPEXC] +
                 [globals()['UC_ARM_REG_D%d' % i] for i in range(16)])

    def snapshot(self):
        """Registers plus every RAM region (and the QSPI store): enough to
        rewind to a booted state without booting again."""
        uc = self.uc
        self.run_unmasked()
        qspi = {k: v for k, v in self.qspi.__dict__.items() if k != 'uc'}
        regs = []
        for r in self.SNAP_REGS:
            try:
                regs.append((r, uc.reg_read(r)))
            except UcError:
                pass                       # a register this Unicorn build lacks
        return (regs, [(b, bytes(uc.mem_read(b, n))) for b, n in self.RAM],
                qspi, set(self.pressed))

    def restore(self, snap):
        regs, mem, qspi, pressed = snap
        if isinstance(regs, (bytes, bytearray)):
            raise ValueError('stale snapshot format (raw Unicorn context); delete build/sysemu-boot-*.pkl')
        for b, data in mem:
            self.uc.mem_write(b, data)
        for r, v in regs:
            if r == UC_ARM_REG_PC:
                continue                   # written last, with the Thumb bit
            self.uc.reg_write(r, v)
        pc = dict(regs)[UC_ARM_REG_PC]
        self.uc.reg_write(UC_ARM_REG_PC, pc | 1)
        self.qspi.__dict__.update(qspi)
        self.pressed = set(pressed)
        self.dma_starts = []
        self.toggle_calls = 0
        self.led_bytes = []
        self.led_rows = []
        self.events = []
        self.toggle_ctor = []
        self.callbacks = []

    def step(self, slices=1, main_instr=20_000, audio=0, keyscan=True, transport=True):
        """One SysTick period (250 us): a slice of the main loop, then the
        interrupts due -- SysTick, the key-scan DMA, the transport timer and
        `audio` frames of the CV engine (8 would be the real 32 kHz)."""
        for _ in range(slices):
            self.run(main_instr)
            n0 = len(self.led_bytes)
            self.inject(VEC_SYSTICK)          # SysTick shifts one LED row out over SPI4
            if len(self.led_bytes) > n0:
                self.led_rows.append(bytes(self.led_bytes[n0:]))
            if keyscan:
                # the DMA1_Stream0 frame carries the three pot ADC readings
                # of the current row: pots parked mid-travel, never moving
                self.uc.mem_write(0x30040000, struct.pack('<3H', *self.pots))
                self.inject(VEC_DMA1_S0)
            if transport:
                # TIM5 is the sequencer clock: 240 MHz / ((PSC+1)(ARR+1)), the
                # firmware reprograms ARR from the tempo (2048 Hz at 120 BPM)
                psc = struct.unpack('<I', bytes(self.uc.mem_read(0x40000C28, 4)))[0]
                arr = struct.unpack('<I', bytes(self.uc.mem_read(0x40000C2C, 4)))[0]
                if arr == 0:   # peripheral registers are not part of snapshots: use the
                    arr = struct.unpack('<I', bytes(self.uc.mem_read(0x2000AFF0, 4)))[0]   # firmware's reload value
                rate = 240e6 / ((psc + 1) * (arr + 1)) if arr else 0.0
                self.tim5_acc += 250e-6 * rate
                while self.tim5_acc >= 1.0:
                    self.tim5_acc -= 1.0
                    self.inject(VEC_TIM5)
            for _ in range(audio):
                self.inject(VEC_EXTI2)
            self.usart_tx()
            self.slices += 1
            for hook in self.slice_hooks:
                hook()

    def settle(self, ms=20, **kw):
        self.step(slices=ms * 4, **kw)

    def press(self, *keys):
        self.pressed |= set(keys)

    def release(self, *keys):
        self.pressed -= set(keys)

    def tap(self, *keys, hold_ms=40, gap_ms=40, **kw):
        self.press(*keys); self.settle(hold_ms, **kw)
        self.release(*keys); self.settle(gap_ms, **kw)

    def chord(self, first, second, hold_ms=40, gap_ms=40, **kw):
        self.press(first); self.settle(hold_ms // 2, **kw)
        self.press(second); self.settle(hold_ms, **kw)
        self.release(first); self.settle(hold_ms // 2, **kw)
        self.release(second); self.settle(gap_ms, **kw)

    def usart_tx(self):
        """DIN MIDI out at the wire rate.  With TXEIE set (np_midi_tx sets it
        while its ring holds bytes; the stock never does, it writes TDR from
        SysTick) the USART raises TXE once per byte time, 320 us at 31250
        baud -- 0.78 per 250 us slice.  Without this the DIN ring never
        drained in emulation: the stock fan-out hid it by overwriting the
        ring, p35's waits for room and would stall."""
        uc = self.uc
        if not struct.unpack('<I', bytes(uc.mem_read(0x40011000, 4)))[0] & 0x80:
            self.usart_acc = 0.0
            return
        self.usart_acc += 250.0 / 320.0
        while self.usart_acc >= 1.0:
            self.usart_acc -= 1.0
            uc.mem_write(0x4001101C, struct.pack('<I', 0x80))   # ISR: TXE, no RX
            if not self.inject(VEC_USART1):
                self.usart_acc += 1.0                           # masked: next slice
                break

    # ---- asynchronous QSPI -------------------------------------------------
    VEC_QSPI = 16 + 92
    HQSPI = 0x20003210

    def enable_qspi_async(self, delay_slices=4):
        """Complete the QSPI driver's interrupt-driven operations, which the
        plain model above leaves pending for ever (no QUADSPI or MDMA
        interrupt is ever raised): FUNC+RECORD's save and the project load
        run as coroutines on driver 8's queue, waiting on
        HAL_QSPI_AutoPolling_IT (status match) and on DMA page transfers.

        * HAL_MDMA_Start_IT (0x08102B38) moves the data at once, through the
          QSPI data register model (programs or reads the array), and the
          transfer completes `delay_slices` later: the channel is released
          the way HAL_MDMA_IRQHandler would (State READY, unlocked), its
          XferCpltCallback runs in interrupt context, then the QUADSPI IRQ
          is raised (its TC branch ends the indirect transfer).
        * A handle left in BUSY_AUTO_POLLING (0x42) for `delay_slices` gets
          the QUADSPI IRQ: the status register model always shows SMF, so
          the HAL ends the poll and calls the status-match callback.
        The delay stands for the erase / page-program time; the default is
        1 ms.  Interrupt priorities are not modelled -- the IRQs land
        between main-loop slices like every other injected interrupt."""
        uc = self.uc
        dr = QSPI + 0x20
        self.qspi_pending = []
        self.qspi_poll_since = None
        self.qspi_irqs = 0

        def mdma_start(uc, address, size, data):
            hmdma, src, dst, blen = (uc.reg_read(r) for r in
                                     (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3))
            count = struct.unpack('<I', bytes(uc.mem_read(uc.reg_read(UC_ARM_REG_SP), 4)))[0]
            n = blen * max(count, 1)
            if dst == dr:
                for b in bytes(uc.mem_read(src, n)):
                    self.qspi.write(dr, 1, b)
            elif src == dr:
                uc.mem_write(dst, bytes(self.qspi.read(dr, 1) for _ in range(n)))
            self.qspi_pending.append((self.slices + delay_slices, hmdma))
        uc.hook_add(UC_HOOK_CODE, mdma_start, begin=0x08102B38, end=0x08102B38)

        def service():
            if uc.reg_read(UC_ARM_REG_PRIMASK) & 1:
                return                     # masked: the IRQ would wait too
            for item in [p for p in self.qspi_pending if p[0] <= self.slices]:
                self.qspi_pending.remove(item)
                hmdma = item[1]
                uc.mem_write(hmdma + 0x3C, b'\x00\x01')          # unlocked, READY
                cb = struct.unpack('<I', bytes(uc.mem_read(hmdma + 0x44, 4)))[0]
                if cb:
                    self.inject(self.VEC_QSPI, target=cb, r0=hmdma)
                self.qspi_irqs += 1
                self.inject(self.VEC_QSPI)
            state = uc.mem_read(self.HQSPI + 0x41, 1)[0]
            if state == 0x42:
                if self.qspi_poll_since is None:
                    self.qspi_poll_since = self.slices
                elif self.slices - self.qspi_poll_since >= delay_slices:
                    self.qspi_poll_since = None
                    self.qspi_irqs += 1
                    self.inject(self.VEC_QSPI)
            else:
                self.qspi_poll_since = None
        self.slice_hooks.append(service)
        return self

    def qspi_queue_len(self):
        """Coroutines on the QSPI driver's operation queue (driver 8 + 8,
        std::deque of 8-byte handles at +0x48)."""
        q = self.call_preserving(0x0810977D, self.call_preserving(0x08109615), 8) + 8
        start, finish = struct.unpack('<II', bytes(self.uc.mem_read(q + 0x50, 4)) +
                                      bytes(self.uc.mem_read(q + 0x60, 4)))
        return 0 if start == finish else 1

    # ---- project defaults -------------------------------------------------
    PATTERN_PARAM_VT = 0x0815EE94        # ProjectEvent<PatternParamData>

    def submit_event(self, vtable, payload):
        """Hand a project event to the manager exactly as the UI does:
        heap object {vptr, payload}, pointer on the stack, 0x08157578."""
        uc = self.uc
        if not hasattr(self, '_event_slot'):
            self._event_slot = self.call_preserving(0x081591D4, 16)
        ev = self.call_preserving(0x081591D4, 12)
        uc.mem_write(ev, struct.pack('<I', vtable) + bytes(payload).ljust(8, b'\0'))
        uc.mem_write(self._event_slot, struct.pack('<I', ev))
        mgr = self.call_preserving(0x0811F854)
        self.call_preserving(0x08157578, mgr, self._event_slot)

    def factory_defaults(self):
        """The store is seeded with an empty journal (SOF, EOF), so the
        project has no pattern: both layers have length 0, the sequencer
        never reaches a step and a pattern rotate walks its step wheel off
        the end.  A real unit's default project has 16 steps.  Pattern
        parameter 0 is the length in steps (stored << 8 at layer+0x60)."""
        pattern = self.call_preserving(0x08145C38)
        if struct.unpack('<H', bytes(self.uc.mem_read(pattern + 0x60, 2)))[0]:
            return
        for page in (0, 1):
            self.submit_event(self.PATTERN_PARAM_VT, struct.pack('<HBBB', 0, page, 0, 16))
            self.settle(20)

    def led_frames(self):
        return [d for d in self.dma_starts if d[2] in (0x40013020, 0x40003820, 0x40003C20,
                                                        0x40013420, 0x40015020, 0x58001420)]


if __name__ == '__main__':
    m = Mono(verbose=True).boot()
    print('pc', hex(m.uc.reg_read(UC_ARM_REG_PC)))
