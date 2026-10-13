"""Small, explicit SPI4 TX / LED wire model for the audit, NOT an MCU simulator.

RM0433: 8-bit simplex master, finite TSIZE, preloaded FIFO, CSTART/EOT/TXTF.
Holtek HT16D35A/B rev 1.51 pp.9,52-53: CS setup/hold/high and byte gaps.
Only mode 0 and mode 3 are implemented. No STM32 erratum is injected.
Pin events are ideal (no pad delay, ringing or bus bridge latency).
"""
import heapq
from collections import Counter, deque

SPI = 0x40013400
CS_BSRR = 0x58021018


class LedWire:
    def __init__(self):
        self.high = True
        self.last_high = None
        self.last_low = None
        self.last_sample = None
        self.samples = []
        self.frames = []
        self.gaps = []
        self.high_widths = []
        self.violations = Counter()

    def cs(self, now, high):
        if high == self.high:
            return
        if high:
            if self.last_sample is not None and now - self.last_sample < 2000:
                self.violations['CS hold < 2 us'] += 1
            if self.samples:
                if len(self.samples) % 8:
                    self.violations['CS split byte'] += 1
                payload = bytes(sum(self.samples[i + j][1] << (7 - j)
                                    for j in range(8))
                                for i in range(0, len(self.samples) - 7, 8))
                self.frames.append((len(self.samples), payload))
            self.last_high = now
        else:
            if self.last_high is not None:
                width = now - self.last_high
                self.high_widths.append(width)
                if width < 100:
                    self.violations['CS high < 100 ns'] += 1
            self.last_low = now
            self.samples = []
            self.last_sample = None
        self.high = high

    def sample(self, now, bit):
        if self.high:
            self.violations['clock while deselected'] += 1
            return
        if not self.samples and now - self.last_low < 50:
            self.violations['CS setup < 50 ns'] += 1
        if self.samples and len(self.samples) % 8 == 0:
            gap = now - self.last_sample
            self.gaps.append(gap)
            # Deliberately RELAXED necessary condition: the datasheet's
            # 2 us interval ends at the next byte's falling/launch edge,
            # BEFORE this next rising/sample edge. Failing even this
            # looser bound proves a violation. Passing is not sufficient.
            if gap < 2000:
                self.violations['byte gap < 2 us (relaxed bound)'] += 1
        self.samples.append((now, bit))
        self.last_sample = now


class SpiTx:
    def __init__(self, regs, kernel_hz):
        self.regs = dict(regs)
        self.regs[0] &= ~0x200  # initial transaction completed
        self.period = 1e9 * (2 << ((regs[8] >> 28) & 7)) / kernel_hz
        assert regs[8] & 31 == 7, 'model supports 8-bit frames only'
        assert (regs[12] >> 17) & 3 == 1, 'model supports simplex TX only'
        assert (regs[12] >> 24) & 3 in (0, 3), 'mode 0/3 only'
        assert regs[4] == 3, 'audit fixture uses TSIZE=3'
        self.mode = (regs[12] >> 24) & 3
        self.midi = (regs[12] >> 4) & 15
        self.fifo = deque()
        self.events = []
        self.serial = 0
        self.now = 0.0
        self.active = False
        self.eot = True
        self.txtf = False
        self.uploaded = 0
        self.sent = 0
        self.wire = LedWire()
        self.log = Counter()
        self.completions = []
        self.starts = []
        self.launch_gaps = []
        self.last_sample = None

    def event(self, at, kind, value=0):
        self.serial += 1
        heapq.heappush(self.events, (at, self.serial, kind, value))

    def advance(self, at):
        assert at >= self.now
        while self.events and self.events[0][0] <= at:
            now, _, kind, value = heapq.heappop(self.events)
            self.now = now
            if kind == 'sample':
                self.wire.sample(now, value)
                self.last_sample = now
            elif kind == 'frame_done':
                self.sent += 1
                if self.sent == self.regs[4]:
                    self.active = False
                    self.regs[0] &= ~0x200
                    self.eot = True
                    self.completions.append(now)
                elif self.fifo:
                    self.start_byte(now + self.midi * self.period)
            else:
                raise AssertionError(kind)
        self.now = at

    def start_byte(self, at):
        byte = self.fifo.popleft()
        # Both supported modes sample on rising edges. Mode 3 starts with
        # a falling launch edge at `at`; mode 0 establishes bit 7 at `at`.
        if self.last_sample is not None and self.sent:
            self.launch_gaps.append(at - self.last_sample)
        for i in range(8):
            self.event(at + (i + 0.5) * self.period,
                       'sample', (byte >> (7 - i)) & 1)
        self.event(at + 8 * self.period, 'frame_done')

    def read(self, offset):
        if offset == 0x18:
            return 0  # IFCR is write-only
        if offset == 0x14:
            return ((0x1008 if self.eot else 0) | (0x10 if self.txtf else 0)
                    | (2 if len(self.fifo) < 8 and not self.txtf else 0))
        return self.regs.get(offset, 0)

    def write(self, addr, size, value):
        if addr == CS_BSRR:
            if value & 8:
                if self.active and not self.wire.high:
                    self.log['CS raised during transfer'] += 1
                self.wire.cs(self.now, True)
            if value & (8 << 16):
                self.wire.cs(self.now, False)
            return
        off = addr - SPI
        if off == 0x18:
            if value & 8:
                self.eot = False
            if value & 16:
                self.txtf = False
                self.uploaded = 0
        elif off == 0x20:
            assert size == 1, 'no packed writes in this audit'
            if not self.regs[0] & 1 or self.txtf or len(self.fifo) == 8:
                self.log['TXDR discarded'] += 1
                return
            self.fifo.append(value & 255)
            self.uploaded += 1
            self.txtf = self.uploaded == self.regs[4]
        elif off == 0:
            self.regs[0] = value
            if not value & 1:
                self.fifo.clear()
                self.active = False
                self.events.clear()
            elif value & 0x200:
                if self.active:
                    self.log['CSTART while active'] += 1
                else:
                    self.active = True
                    self.starts.append(self.now)
                    self.eot = False
                    self.sent = 0
                    self.last_sample = None
                    if self.fifo:
                        self.start_byte(self.now)
        else:
            self.regs[off] = value


def replay(spi, trace, start_ns, instruction_ns, cs_high_min_ns=0):
    """Replay ACTUAL executed MMIO writes with explicit hypothetical CPU timing.

    CS delay is a counterfactual scheduling change, never an image edit.
    Reads stay in trace for coverage; IFCR=0 and CR1|CSTART are invariant
    in the tested sender, so write values don't depend on model status.
    """
    shift = 0
    for insn, pc, access, addr, size, value in trace:
        now = start_ns + insn * instruction_ns + shift
        if (access == 'W' and addr == CS_BSRR and value & (8 << 16)
                and spi.wire.last_high is not None):
            extra = max(0, spi.wire.last_high + cs_high_min_ns - now)
            shift += extra
            now += extra
        spi.advance(now)
        if access == 'W':
            spi.write(addr, size, value)
    return spi.now


def self_test():
    regs = {0: 0x1001, 4: 3, 8: 0x30000007, 12: 0x04420000}
    s = SpiTx(regs, 40e6)
    s.wire.cs(0, False)
    for x in (0x80, 0x4D, 42):
        s.write(SPI + 0x20, 1, x)
    assert s.read(0x14) & 16 and not s.events  # filled != transmitted
    s.write(SPI + 0x20, 1, 99)
    assert s.log['TXDR discarded'] == 1
    s.write(SPI, 4, 0x1201)
    s.advance(9599)
    assert s.active and not s.eot
    s.advance(9600)
    assert s.eot and not s.active and not s.regs[0] & 0x200
    s.wire.cs(12000, True)
    assert s.wire.frames == [(24, b'\x80\x4d\x2a')]
    assert s.wire.gaps == [400, 400]
    assert s.wire.violations['byte gap < 2 us (relaxed bound)'] == 2
    d = SpiTx({**regs, 0: 0x1000}, 40e6)
    d.write(SPI + 0x20, 1, 1)
    assert not d.fifo and d.log['TXDR discarded'] == 1


if __name__ == '__main__':
    self_test()
    print('SPI model controls passed (not a hardware-conformance test).')
