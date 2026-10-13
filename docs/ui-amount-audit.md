# UI freeze during ratio-FM Amount editing — 2026-09-15

> Later clarification: the keys/pots still work and there is no reset; only
> the LEDs freeze. The deque race below remains a separate finding (fix N in
> docs/patches.md), not the established cause of that symptom; the LED fixes
> are L and M there. Image identities below describe the earlier snapshot.

## Result and limits

**Fixed 2026-09-16 as patch N** — see [patches.md](patches.md).  The unit's
telemetry showed the second outcome of this same corruption: not the `_exit`
branch reproduced below, but `start` overshooting `finish` so the queue looks
empty for ever.  The main loop then keeps iterating at 6,000/s, SysTick at
4 kHz and the LED writer at 1,282 frames/s while the UI consumes nothing,
every pushed event leaks (the small pool pinned at its 192-block thread limit
with 356 failures) and the panel is frozen.  The fix replays the whole
start-iterator transition with interrupts masked.

**A real UI deque producer/consumer race is reproduced in both the factory
image and the shipped heap-fallback patch.** It reaches the stock infinite
`_exit` loop, not just a stuck TYPE/WAVE modifier. The latest pool fallback
does not fix it. The fault is common to key and pot events; ratio FM need
not be enabled.

**This is not yet proof of the exact failure seen on the unit.** The reproduction
deliberately accumulates 128 pending events and injects a producer operation
at a specific interruptible instruction. We do not know whether a real
project/peripherals produce that backlog. A serial-IRQ full-firmware test
of oscillator 1 / fourth TYPE / full-range Amount, with a held note, remains
responsive. Real cycle cost and nested IRQ preemption are not simulated.

This turn changed diagnostic tests and documentation only. No firmware
source, flashable image, cache policy, envelope sound, or device was changed.

## Image actually inspected

`build/mono_V2.2.0r-patched.syx` is identical to
`build/mono_V2.2.0r-modnote-heap-fallback.syx`:

* SYX MD5: `b147c3ad54d4a0e73f81bfe70787c31b`
* SYX SHA-256: `7ea9b1cef14beea2657bc1681bcfa0005d1aa344faf30f5062cadda761f7b5b3`
* Raw SHA-256: `addf20c8c1886bf90e2c88b39f119d1656e697a4dc3e36a174e4aea399918e87`
* Stock SHA-256: `85af6a14b31cd3d3aa8ebdb3a8891cf2054320f262e4ca2147d8acb1248017c8`

The sources contain later MIDI telemetry work, but **this binary does not**:
its NPAT diagnostic size is `0x420`; current `np_patch.S` requests `0xD80`.
The current static test separately expects `0x940`, another mismatch. Do not
tell someone to collect the new MIDI heartbeat from the above SYX, or claim
that a build of current sources has the same identity/coverage. No rebuild
was done as part of this read-only firmware investigation.

## Actual paths

The panel object is `0x2000DE54`. Its input queue is a `std::deque` of
event pointers, **not** the time-budgeted project list:

| Panel field | Meaning |
| --- | --- |
| `+0x1C`, `+0x20` | block-pointer map, map capacity |
| `+0x24`, `+0x28`, `+0x2C`, `+0x30` | start cursor, block start/end, map-node pointer |
| `+0x34`, `+0x38`, `+0x3C`, `+0x40` | finish cursor, block start/end, map-node pointer |

Each block holds 128 four-byte pointers (512 bytes).

* Pot ADC scanner constructs a 12-byte `UiEvent` and enqueues it through
  `0x08157324` at **`0x0810F1EA`**, from DMA1_Stream0.
* The key scanner uses the same queue and producer.
* Main calls the consumer `0x081574F8` at **`0x08119A60`**.
* A full final block can make producer `0x08157324` recenter the map
  (`0x08157422..`) or allocate a replacement (`0x081573BE..`). It writes
  **both iterators' map-node pointers** at `0x081573FE` and `0x0815740A`.

The first/last metadata shared by these operations has no encompassing
critical section. Making individual `new`/`delete` calls safe does not make
these multi-instruction deque operations safe.

## Reproduced failure

The consumer finishes a block:

```asm
08157558  ldr   r3, [r4, #0x30]   ; old start.node
0815755A  adds  r2, r3, #4
0815755C  str   r2, [r4, #0x30]   ; publish next node
0815755E  ldr   r3, [r3, #4]      ; read block through old map location
08157560  add.w r2, r3, #512
08157564  str   r3, [r4, #0x24]
08157566  strd  r3, r2, [r4, #0x28]
```

If the scanner's next push recenters the map **after the load at 0x08157558
but before the store at 0x0815755C**, it updates `start.node` in memory.
Main resumes with the *old* value in `r3` and overwrites the corrected
pointer with `old + 4`. Its cursor is now outside the live map entries.

Concrete full-boot run (real allocator, dispatcher and callbacks):

1. Normal push/pop operations position the empty deque near the end of its
   map; 128 more pushes leave the consumer at the last element of the
   penultimate block and the producer at the last element of the last one.
2. Main is paused immediately before `0x0815755A` or `0x0815755C`.
3. One more push is executed with `IPSR=27` (DMA1_Stream0), using a separate
   interrupt stack area; the interrupted register context is then restored.
4. The producer changes start-node `0x300046B8 -> 0x300046A8`.
5. Main replaces it with the obsolete `0x300046BC`; the next block pop moves
   it to `0x300046C0`, one word past this eight-entry map.
6. Consumer attempts to dispatch event **zero** after the 129 valid events.
   `0x08157494 -> 0x081574F2 -> 0x0815913A -> 0x0815A224` reaches
   **`0x0815A6E0: b 0x0815A6E0`** (`abort` / `_exit`). Main no longer returns.

This can leave interrupt-driven work alive while main is stopped; with an
armed main-loop watchdog it should ultimately reset. It does **not** prove
that an indefinitely responsive sequencer plus frozen LEDs has this cause.

## Reproduction and controls

```sh
build/venv/bin/python tests/probe_ui_queue_race.py
build/venv/bin/python tests/probe_ui_queue_race.py --live
build/venv/bin/python tests/probe_ratio_amount.py
```

On this macOS ARM host execute Unicorn outside the app sandbox. An emulator
host SIGILL/segfault is not evidence of a target firmware fault.

The two queue fixtures agree:

| Producer injection point | Factory | Heap-fallback patch |
| --- | --- | --- |
| Serial push, then drain | 129 events, normal return | 129 events, normal return |
| Before `0x08157558` | 129 events, normal return | 129 events, normal return |
| Before `0x0815755A` | null 130th event, `_exit` | null 130th event, `_exit` |
| Before `0x0815755C` | null 130th event, `_exit` | null 130th event, `_exit` |
| Before `0x0815755E` | 129 events, normal return | 129 events, normal return |
| Before `0x08157560` | 129 events, normal return | 129 events, normal return |

24 checked queue scenarios total across the two fixtures. Failure is the
**expected diagnostic assertion** at the two hazardous boundaries; a green
probe does not mean firmware has been fixed. Both image hashes are checked
before/after. The isolated fixture uses a non-reusing test allocator and
suppresses UI action callbacks; `--live` boots from Reset_Handler and uses
the actual allocator, pool fallback, RTTI and UI callbacks. The injected
operation is the real queue producer, not a full asynchronous hardware DMA
event. It establishes the interleaving and corrupt state, not likelihood.

The full-range Amount probe independently verifies selected slot 0, raw TYPE
`3/65536`, published mode 2 and ratio marker 2. It feeds the external ADC
buffer directly, records real pot-generated events, and observes the actual
owner's Amount value reach both `0.0` and `0.999985`. At each of 14 moves
main continues iterating. This does not exercise a real saved project,
USB/DIN traffic, or IRQ timing.

## Test weaknesses found during this investigation

* `sysemu.pot_row()` masks the external sample to `0xFFF`, but the scanner
  and parameter path operate on an inverted 16-bit sample normalized by
  65536. Thus a model sweep of 0..4095 covers about 0.9375..1 of Amount,
  not its full range. The new probe overrides only that ADC input hook and
  explicitly checks the resulting parameter endpoints. It does not infer
  effective ADC range merely from a nominal hardware resolution.
* Serial interrupt injection cannot disprove a deque race. The adversarial
  test saves/restores registers while keeping the producer's shared-memory
  changes, exactly the distinction the ordinary scenario test lacks.
* Instruction counts are not DWT cycles. `sysemu.step()` grants a main slice
  plus all ISR work each tick, without subtracting IRQ cycles from the main
  budget. The claim that 30–48k instructions excludes an audio overrun is
  too strong. No new hardware timing measurement was obtained.
* Persisting raw Unicorn context bytes between host processes produced a
  host segfault in these runs. New probes therefore boot fresh, not through
  the disk context cache. Two cache files generated in this investigation
  were renamed `.pkl.quarantined`; pre-existing untagged caches are untouched.
* In the live allocator, a just-delivered event's address may legitimately
  be reused by the injected producer. Address uniqueness across lifetimes
  is not a valid pass criterion there; count, non-null delivery, iterator
  consistency and normal return are checked instead.

## Fix direction / next evidence needed

Protect the **complete start-iterator transition** `0x08157558..0x08157569`
against producer preemption, preserving the incoming interrupt mask and
restoring it before UI dispatch resumes. Allocation, freeing, callbacks,
and the whole queue drain must remain outside that small critical section.
Do not repeat the former project-budget or TYPE/WAVE toggle changes: they
address different structures.

A release patch should test every instruction boundary of the replacement,
both initial PRIMASK states, live allocation/reuse, map *reallocation* as
well as in-place recentering, and ordinary UI/DSP regressions. This document
is a diagnosis, not a claim that such a replacement has been implemented.

To attribute the particular hardware freeze, collect whether it resets
after about 16 seconds, whether notes/steps actually advance while frozen,
the connected USB/DIN configuration, and ideally the affected project dump.
A main PC at `0x0815A6E0` plus corrupt deque iterators would link the cases;
a continuing main heartbeat would point to a different remaining defect.
