# Runtime performance and allocator telemetry

The default patch now unlocks and enables the Cortex-M7 DWT cycle counter and records a
1056-byte block at `0x2000E400`.  The first 64 bytes retain the old fault and
watchdog layout; the extension is reset on every boot.

## What is measured

| counter | meaning |
|---|---|
| audio | body of the 32 kHz EXTI2 handler; deadline is 12,500 cycles / 31.25 us |
| SysTick | complete stock SysTick body after USB was replaced by a defer flag |
| SPI3 (misnamed) | complete IRQ50 handler — IRQ 50 on the STM32H743 is **TIM5**, the transport/sequencer tick timer (`0x081194A4` clears `TIM5_SR`, counts `transport+0x70` down and raises the `+0x88C` step flag the main loop polls); SPI3 is IRQ 51, whose vector is the empty stub. The row and symbol keep the old name for tool compatibility |
| DMA1_Stream0 | complete IRQ11 handler |
| main period | cycles between consecutive main-loop hooks; includes time preempted by IRQs |
| malloc IRQ-off max | longest `__malloc_lock`→`__malloc_unlock` PRIMASK interval |

Each DWT row has a 64-bit cycle total, 32-bit count, last, maximum and
overrun count.  Exception entry/exit and the profiler's own bookkeeping are
not in the handler-body number.  Higher-priority preemption *is* included,
which is the relevant part for deadline misses.

The same block contains MIDI ring high-water/full observations, bytes sent,
USB defer/coalescing counts, fixed-pool current/high-water/failures and
thread-event fallbacks, malloc
and `_sbrk` failures, heap tail remaining, and the largest allocatable heap
payload seen by a validated free-list snapshot.  `midi_queue_full` counts
times the consumer observed a 511-byte ring; the closed producer can move
the read index itself, so this is an episode counter rather than an exact
number of overwritten bytes.

## Reading it

Nothing in the stock UI can display arbitrary RAM.  With an SWD probe, halt
the running unit without resetting it and ask GDB to dump the range:

```gdb
dump binary memory build/diag.bin 0x2000e400 0x2000e530
```

Decode it with:

```bash
python3 tools/patch.py diag build/diag.bin
```

Do not issue `load`, `monitor reset`, or a reset command before the dump;
those destroy the per-boot timing sample.  A warm reset preserves the old
fault fields but intentionally clears the profiler extension.

Fault code 5 means that a fixed pool ran out.  Unlike its per-boot counter,
this cause is retained across the reset triggered by the failure.

As of 2026-09-14, threads may consume only 192 of 256 small blocks and 7
of 15 large blocks. The last 64 small / 8 large blocks are reserved for
interrupts; threads use the existing heap fallback once their partition is
full. This fixes a reproduced fault in RC5 where thread allocations exhausted
both pools and the next ISR `new(12)` trapped despite unused heap. See
[modnote-reboot.md](modnote-reboot.md) for evidence and limitations.

Healthy invariants before further optimisation are:

- `audio overruns = 0`;
- `pool_failures = pool_bad_free = 0` and
  `malloc_failures = sbrk_failures = 0`;
- small-pool high-water below 256 and big-pool high-water below 15;
- `largest_free` comfortably above the largest observed application request;
- MIDI high-water below 511.

`event_pool_fallback` need not be zero: with the ISR reservation it is an
intentional, safe thread-mode fallback, not a discarded event. Its frequency
still matters for heap fragmentation and IRQ-off time.

## `-Os` versus `-O3`

Build both flashable images with:

```bash
make patch-dsp-ab
```

This produces the default `build/mono_V2.2.0r-patched.syx` (`-O3`) and
`build/mono_V2.2.0r-patched-Os.syx`.  Flash each, load the same preset, play
the same MIDI sequence for the same duration, then compare the audio average,
maximum and overrun count.  Static size is not a speed result: on the current
toolchain the `np_lfo.o` payload is 2,140 bytes at `-O3` versus 1,760 bytes at
`-Os` (excluding ELF metadata and including the two 256-byte ratio tables).
Only the hardware DWT result decides which image is faster.

Control/audio-rate separation, dirty-parameter caching and reciprocal
replacement remain intentionally after this A/B measurement.  The current
binary does not expose enough attribution to justify changing numerical DSP
from an aggregate timing guess.
