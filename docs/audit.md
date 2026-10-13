# Norand Mono V2.2.0 — firmware audit

The engine internals every finding below refers to — parameter layout,
the modulation matrix, the audio-rate path, and the open threads left — are
in [findings.md](findings.md).

Everything below is read off `build/firmware.bin` (479704 bytes, loads at
`0x08100000`) and `build/bootloader.bin`. Each finding names the address it
was read from so it can be checked independently:

```bash
arm-none-eabi-objdump -d build/firmware.elf | less   # symbolised
python3 tools/xref.py fn 0x081591D4
```

Findings are ordered by how badly they can bite a player on stage.

**Status:** A1, A2, A4, B1 (instrumentation), C and G are fixed in
`build/mono_V2.2.0r-patched.syx`; E1 is available as an option. See
[patches.md](patches.md). B3, the rest of B1, D and E2/E3/E4 need the
original source or a bootloader change and are still open.

---

## A. Nothing recovers from a fault

**A1 — every fault handler is an infinite loop.**

```
081093cc <HardFault_Handler>:   e7fe   b.n 81093cc
081093d0 <MemManage_Handler>:   e7fe   b.n 81093d0
081093d4 <BusFault_Handler>:    e7fe   b.n 81093d4
081093d8 <UsageFault_Handler>:  e7fe   b.n 81093d8
```

`NMI_Handler`, `SVC_Handler`, `PendSV_Handler` and `DebugMon_Handler` are
bare `bx lr`.

**A2 — there is no watchdog.** No reference to IWDG1 (`0x58004800`) or
WWDG1 (`0x50003000`) exists anywhere in the image. Nothing will ever pull
the device out of A1.

**A3 — 129 `udf #255` trap sites.** GCC compiled every unrecoverable
library path into an undefined instruction, which raises UsageFault and
lands in A1. The reachable ones include:

| trap | reached from |
|------|--------------|
| `cannot create std::vector larger than max_size()` | `0x0810DD82`, `0x0810E3C8`, `0x0810E5C8`, `0x08145294` |
| `cannot create std::deque larger than max_size()` | `0x0810C1A8`, `0x08157324` |
| `Out of range` | `0x0810E0B4`, `0x0810E0EC`, `0x0810E1D8`, `0x0810E5C8`, `0x0810ED8C` |
| `Borrowable already borrowed` | `0x0810DD82`, `0x0810E5C8` |

**A4 — heap exhaustion executes a breakpoint.**

```
08119678 <_sbrk>:
 8119690:  ...  heap_top - brk  <  requested ?
 811968c:  be00   bkpt  #0
```

Instead of returning `(void *)-1` so `malloc` can fail cleanly, `_sbrk`
executes `bkpt #0`. With no debugger attached that is a HardFault, i.e. A1.

**Consequence.** Any of A3/A4, or any genuine bus/alignment fault, freezes
the device permanently: the sequencer stops, and whatever the CV/gate DAC
and the LED drivers were last given stays on the outputs. Only a power
cycle recovers. There is no way for a user to distinguish this from a
hardware failure, and no post-mortem information is kept.

**Fix — done, see [patches.md](patches.md).** The four fault handlers now
latch the fault id, a counter, CFSR, HFSR and the stacked PC/LR/xPSR into
DTCM at `0x2000E400`, then `NVIC_SystemReset()`. DTCM rather than backup
SRAM: it survives a warm reset just as well and needs no clock or
write-protection dance inside a fault handler. IWDG1 runs at 16 s, armed on
the first iteration of the main loop and kicked from `0x08119A02` — not from
SysTick, which keeps running through an application deadlock. `_sbrk` is
redirected to a replacement with the same arithmetic and the same signed
comparison that returns `-1` on a full heap.

Not fixed: the CV/gate outputs are still left wherever they were. Putting
them into a safe state means driving the DAC from a fault handler, and the
protocol is not understood well enough to do that blind.

---

## B. Dynamic allocation on the real-time path

**B1 — 459 call sites of `operator new`** (`0x081591D4`), 447 of them in
application code rather than the C++ runtime. They include the event
pipeline (`0x08116224`), the sequencer command constructors, and the USB/MIDI
layer (`0x0810C894` and neighbours). Every `ControllerEvent`,
`ProjectEvent` and `SequencerEvent` is heap allocated — see
`docs/feature-map.txt`, where each `...Data` command has its own
constructor function.

**B2 — the heap is 256 KiB of D2 SRAM.**

```
_sbrk:  heap_top = 0x30000000 + *(uint32_t *)0x0815EA38   /* 0x00040000 */
        brk      = *(uint32_t *)0x200000F4                /* .data, init 0x30000000 */
```

so `0x30000000 .. 0x30040000`. The QSPI driver's buffer starts right above
it at `0x30040000` (passed in `main`, `0x08119970`), so there is no slack
between the two.

**B3 — every allocation disables all interrupts.**

```
081196a0 <__malloc_lock>:    mrs r2, PRIMASK ; store ; cpsid i
081196b8 <__malloc_unlock>:  msr PRIMASK, r3
```

**Consequence.** Two distinct problems. Long term, fragmentation of a
256 KiB heap under a workload that allocates one object per note event
eventually fails an allocation, and B2 + A4 turn that into a freeze — this
is the most plausible mechanism behind "it locked up after an hour". Short
term, every `new`/`delete` blocks MIDI, USB and the timer interrupts for
the duration of the allocator's work, which is exactly the kind of jitter a
sequencer must not have.

**Fix — partial.** The replacement `_sbrk` records the high-water mark of
`brk` at `0x2000E434`, so the remaining margin is now a measurement rather
than a guess. The real fix — a fixed-size pool allocator for the event types,
which are small and uniformly sized (`operator new(20)` is the common case,
e.g. at `0x081183D4`), with `malloc` kept for start-up only — needs the
source. B3 needs the source too.

---

## C. Both caches are switched off

`SystemInit` (`0x081093F4`) enables the FPU (`SCB->CPACR |= 0xF00000`) and
configures RCC. Nothing in the image touches `CSSELR` (`0xE000ED80`),
`CCSIDR` (`0xE000ED84`), `ICIALLU` (`0xE000EF50`) or any `DCxSW` register,
and `SCB->CCR` is never written with the `IC`/`DC` bits.

So a Cortex-M7 is executing flash-resident code with no instruction cache.
For code that does not fit the ART prefetch window this typically costs a
factor of two or more.

**Fix — done.** The instruction cache is enabled before the static
constructors run, in the hook that replaces `bl SystemInit`. Safe here
because the image is never written while running — the bootloader owns that.

The data cache is deliberately still off: it needs `SCB_CleanDCache_by_Addr`
/ `InvalidateDCache_by_Addr` around every DMA buffer in D2 SRAM, or MPU
regions marking them non-cacheable, and getting that wrong corrupts audio and
MIDI silently rather than failing loudly.

---

## D. SysTick does too much, and the firmware knows it

`SysTick_Handler` (`0x081196D8`) runs, inline, in interrupt context:

* `HAL_IncTick` (`0x08100428`)
* two subsystem ticks (`0x08149918`, `0x0814F934`)
* MIDI service (`0x0811A6A4`, `0x0811AC90`)
* `driver(7)` — `Usb` — polling (`0x08110244`)

It is wrapped in a reentrancy flag, and when the flag is already set it
**increments a missed-tick counter** and returns (`0x081196E8`). In other
words, tick overrun is a designed-for condition, not an impossible one.

**Consequence.** Every millisecond of work added elsewhere with interrupts
disabled (B3) or spent waiting on QSPI comes straight out of this budget,
and a missed tick is a late or dropped step.

**Fix.** Move USB and MIDI servicing out of SysTick into their own
interrupts or a main-loop pump, and expose the missed-tick counter — it is
already being maintained and is the single most useful number for
diagnosing timing complaints.

---

## E. The update path has no integrity check at all

**E1 — the firmware container carries no checksum or CRC.** The bootloader
container protects each message with an 8 bit checksum over its 32 payload
bytes; the *firmware* container has nothing. The packed stream ends with 80
bytes of zero padding and no trailer.

**E2 — packet order is never verified.** The bootloader reads the message
index (`0x08026BDC`) and only ever compares it to the total, to decide when
the transfer is finished. A dropped or duplicated message shifts every
subsequent byte in the staging buffer, and the update still reports success
and reboots into a corrupt image.

**E3 — a short group leaks into the next message.** On `F7` the receiver
resets the parser state (`0x08026B90`) but not the 8 byte group counter at
`+0x580`. With stock 112 byte payloads (exactly 14 groups) this never
triggers; any tool that chooses a different packet size silently corrupts
the image.

**E4 — nothing is verified before the jump.** After the last flash word the
bootloader disables interrupts, clears the NVIC and jumps to
`*(uint32_t *)0x08100004` (`0x0802663A`). No CRC, no signature, no sanity
check on the vector table.

**E5 — the size estimate is stale.** `0x08026BF8` computes
`total * 7 * 16` (112 bytes per message) while the V2.2.0 updater sends 98.
It only feeds the progress bar and a `>= 0x80000` bound check, and the
decompressor ignores the value, so it is currently harmless — but it is
wrong, and it will bite anyone who changes the packet size.

**Fix — partial.** `tools/patch.py build --crc` embeds a CRC32 of the image
and has the application measure it at boot, recording both values in the
diagnostic block. It records rather than refuses: halting on a false positive
would turn a diagnostic into a brick, and nothing on the device can display
the result anyway. Off by default for that reason.

E2, E3 and E4 all live in the secondary bootloader. Changing it means
flashing a modified `mono_secondary_bootloader.syx`, which the primary
bootloader accepts — but the primary bootloader is not in either shipped
file, so a mistake there is not recoverable from here. Left alone
deliberately.

---

## F. Smaller notes

* `Borrowable already borrowed` (`0x0815B21C`) is a runtime borrow checker
  whose violation is a trap rather than a recoverable error — it turns a
  logic bug into A1.
* The `MemoryQSPI` error paths log a descriptive string but there is no
  visible recovery: `Invalid Chip Descriptor`, `Incorrect block type in
  MemoryQSPI::LL_Erase`, `Chip flash size must be divisible by all block
  sizes`. Worth checking what happens to a project save that hits one.
* The third byte of every firmware message header is `0x16` in all stock
  files. The bootloader stores it (`+0x586`), uses it in the stale size
  estimate, and compares it against the partial-group length — where any
  value above 7 means "never". Its intended meaning is unconfirmed; treat
  it as a constant when generating files.

---

## G. The synced X-Mod LFO is not synced

**G1 — the rate is computed once and never again.** The synced branch of
`LFOTimeCParam::vfn2` (`0x0811BDF2`) computes

```
rate = transport_tick_rate / divisionTable[index]
```

and hands it to `setRate` (`0x08156910`). That function is reachable from
exactly three call sites — the two knob handlers and one patch-apply path
that uses a different table — and there are no function pointers to it. So
nothing in the clock path ever recomputes the rate. Change the tempo and the
LFO keeps the speed it had when the knob was last touched; under external
MIDI clock, that is essentially always the wrong one.

**G2 — the division index can read past the table.** The index is
`(unsigned)(value * 15.0f)` against a 15 entry table. A parameter value of
exactly 1.0 gives 15 and reads `0x0815F35C` = 7.9e34, so the rate collapses
to roughly zero and the LFO stops at the top of its range.

**G2b — the external MIDI phase gate was misidentified, and stock is also
wrong for fractional divisions.** There are two callers of the slot walker
`0x08116CD4`, not one. The normal sequencer caller is at `0x081467C2`, but
the accepted MIDI timing-clock path is:

```
DIN/USB F8 -> 0x0811B2CC case 8 -> 0x08118B00
           -> 0x08116CD4 -> 0x0811CC70 for each modulation slot
```

`0x08118B00` invokes the walker on every F8 pulse (24 PPQN), passing the
integer conversion of a double transport position that advances by 128/3
engine ticks. Therefore the stock `pos % (uint16_t)division` is live. The
first patch incorrectly removed it as dead code and retriggered every synced
LFO on every F8; a slow LFO barely moved before being reset again.

Restoring the stock modulo literally is insufficient. It truncates 682.667,
341.333, 204.8 and 85.333, and the accumulated double position can also
turn an exact integer boundary into the preceding integer (1024 arrives as
1023 on one quarter boundary). In the stock calculation, division 682.667
hits at MIDI pulse 16 and then misses for 1007 pulses; 204.8 retriggers every
153 pulses instead of at the 24-pulse common boundary.

The replacement expresses all 15 divisions as exact numerator/denominator
pairs. It refreshes rate on every accepted transport-rate change, but only
retriggers when the integer position contains a true rational boundary,
allowing the one-tick loss caused by conversion. All 15 divisions and the
real `0x08118B92 -> 0x08116CD4` call site are exercised under ARM emulation.
At divisions finer than MIDI clock can represent, the LFO free-runs at the
correct rate and receives phase correction at the first shared boundary:
204.8 every five LFO cycles / 24 F8 pulses, and 64 every two cycles / three
F8 pulses.

**G1 — corrected. The rate did follow the tempo; the retrigger did not.**
The original write-up said `setRate` is reachable from three call sites,
none in the clock path, and concluded that a synced LFO free-runs at a stale
rate after a tempo change. The three call sites are right and the conclusion
is wrong: `sub_08118A14`, the only writer of the transport tick rate at
`transport+0x04`, calls `0x08116C9C` on both of its exits, and that walks all
nineteen slots applying their LFOSpeed parameter -- which reaches `setRate`
through `LFOSpeedCParam::vfn2`. Every change to the tick rate is therefore
already followed by a full refresh. Verified by scanning the whole image for
float stores landing on `<base>+4`, folded bases included: `0x08118A86` is the
only one on the transport.

What was actually broken is the retrigger. The stock handler gates it on
`pos % (uint16_t)division`, and truncating 682.667, 341.333, 204.8 and 85.333
puts the triplet and quintuplet retriggers on the wrong pulses entirely. That
is what the hardware test heard. The rate-refresh half of the fix was
redundant and has been removed; the retrigger half is what matters.

**G3 — withdrawn. There was no divisor bug.** The earlier reading of this
section claimed that audio-rate mode computes its table position as
`(knob - 0.5) + pitch / 126` while the frequency table at `0x0817379C` spans
MIDI 0..127, so the divisor had to be 127, and that the resulting 0.79 % per
semitone was audible. Both halves are wrong, and the four literals that were
retuned (`0x0811BD2C`, `0x0811BD40`, `0x0811BF14`, `0x0811BF20`) have been
put back.

*The value being divided is always zero.* It is `project+0x2F20` in
`LFOSpeedCParam::vfn2` and `project+0x2F60` in `LFOTimeCParam::vfn2`.
Scanning every instruction in the image that can address either field — as a
displacement off a base register folded from an immediate, not just as a
literal — gives **four readers, all inside those two handlers, and one
writer: the zero-initialiser at `0x08147612`**. Nothing in the firmware ever
stores a value there. `0/126` and `0/127` are the same number.

*And these handlers are not where the pitch is decided.* The per-block DSP
update walks the nineteen slots at `0x08112C70` and, for every slot whose
owner has the audio-rate flag at `+0x25` set, recomputes the position and
calls `setRate` again — overriding whatever the handler wrote, every block.
That loop uses `project+0x2EDC`, which is live.

*In the live path the 126 is correct.* `project+0x2EDC` is built at
`0x08112810` as `126 * position + fine tune`, clamped to `[0, 126]`; the
`* (1/126)` in the loop undoes exactly that multiply and hands back a
normalised position, which then indexes the 257-entry table across its full
MIDI 0..127 span. Replacing it with 127 would have introduced the error the
patch claimed to remove.

*On the confirmation.* The ear test that was reported as confirming G3 asked
for the same note five octaves apart, which cannot separate 0.79 % per
semitone from the much larger effect of no note tracking at all. It was not
evidence for the divisor either way. The real complaint behind it — the
audio-rate modulator does not follow the played note — is a separate,
genuine defect; the note itself lives at `project+0x5FD0` and the firmware's
own keytracking term, in the modulation evaluator at `0x081156C4`, is
`(note + project[0x6004] - 57.0) * amount`, referenced to MIDI 57 (A3).

**Fix — done, in C++.** `patch/np_lfo.cpp` replaces the handler:
`np_mod_tick` recomputes the rate from the live transport rate, clamps the
index, and performs the phase reset (override dispatch included) only on the
exact rational boundary described in G2b.
`np_lfo_poll` runs from the existing main loop hook and refreshes every
synced slot whenever the transport rate changes — one indirect call and a
float compare in the common case. The isolated hook, all 15 divisions, and
the production F8 call site are verified under emulation.

**H — audio-rate X-Mod does not follow the keyboard.** In audio-rate mode
the modulator's pitch comes from the knob and the oscillator's tuning, and
nothing else: the position handed to the frequency table is
`(rate - 0.5) + project[0x2EDC]/126`, with no term for the note being
played. An FM timbre dialled in on one note is therefore a different
timbre on the next, because the carrier moves and the modulator does not.
This is what the [FOLLOW] machinery would fix for an ordinary parameter,
but [FOLLOW] reaches only local parameter indices 0..5 (the guard at the
top of `0x081156C4`), and the X-Mod type is index 7.

**Fix — done, a fourth type.** Not a change to the three that exist: type 3
is audio rate plus one semitone of modulator pitch per key, rooted at C1
(MIDI 24), so it remains in audio-rate in the Mono's low register; type 2
keeps behaving exactly as it always has. The pieces, the addresses and the
reasoning are in [patches.md](patches.md#the-fourth-x-mod-type). The dynamic
keytracking checks exist in `tests/test_patch.py`, but Unicorn currently
raises `SIGILL` in `uc_mem_map` on this macOS ARM host; fresh verification here
is static (exact branches/bytes and SysEx round-trip) until that host-tool
issue is resolved.

The two structural facts behind it are worth stating on their own, because
both contradict what an earlier reading of this firmware assumed:

* The nineteen "modulation slots" are the nineteen CV destinations, in the
  order the descriptor table at `0x0815E8E0` lists them — VCO1Tune,
  VCO1Fine, VCO1Wave, VCO1Vol, VCO2Tune, VCO2Fine, VCO2Wave, VCO2Vol,
  **VCOTZFM**, FiltFreq, FiltReso, FiltColor, FiltEnv, EnvAttack, EnvDecay,
  EnvRel, EnvSust, OutVol, Sync. Each owns nine parameters, so a global
  parameter index is `slot * 9 + local`, and `owner+4` is the slot id.
  X-Mod is VCOTZFM, slot 8.
* The parameter handlers are not where an audio-rate modulator's pitch is
  decided. The per-block DSP update recomputes it for every audio-rate slot
  and calls `setRate` again, every block. Anything written by a handler
  survives at most one block.

---

## What was fixed, and what is left

Fixed in `build/mono_V2.2.0r-patched.syx` — 37 bytes rewritten, 656 bytes
appended, every entry point exercised under emulation. See
[patches.md](patches.md).

1. **A1 + A2** — fault handlers and a watchdog. Every freeze becomes a
   recoverable reset with a record of what happened.
2. **A4** — a full heap fails cleanly instead of hanging.
3. **B1** (instrumentation) — the heap high-water mark is recorded.
4. **C** — the instruction cache is on.
5. **E1** — optional CRC32 self test (`--crc`).
6. **G1 + G2 + G2b** — the synced X-Mod LFO tracks the tempo, the division
   index stays in range, and external MIDI F8 no longer retriggers it on
   every pulse; rational phase boundaries replace the stock drifting modulo.
7. **G3** — the audio-rate modulator now tracks OSC1 at 1:1, so FM ratios
   hold across the keyboard.

Still open, because they need the source or the primary bootloader:

* **B3 → B1** — get allocation off the interrupt path and pool the event
  objects. This is the fix that addresses the cause rather than the symptom;
  everything above only makes the failure survivable.
* **D** — move USB and MIDI servicing out of SysTick, and surface the
  missed-tick counter the firmware already maintains.
* **E2/E3/E4** — packet ordering, the short-group leak and a verified jump,
  all inside the secondary bootloader.
* **A1, remainder** — driving the CV/gate outputs to a safe state before the
  reset.
