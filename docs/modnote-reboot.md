# Mod-note reboot investigation / ISR reserve — 2026-09-14

## Status: one reproduced failure fixed, the exact crash on the unit not reproduced

Report: some saved mod-note patches cause a hard reboot after the RC ADSR /
X-Env changes. No failing project export or hardware fault record was supplied
at inspection time. **Do not call this a proven fix for that particular
project or a guarantee against all reboots.** The candidate fixes a genuine
allocator failure found during the investigation; saved-parameter DSP tests
did not reproduce a numeric/format incompatibility.

Baseline: `build/mono_V2.2.0r-env-rc5.syx`, MD5
`d3aaffeccd731da09345f4f0940ff750`. Preserved decoded copy:
`build/firmware-env-rc5.bin`, SHA-256
`68072482c95485ec9936c85a6c1efbbed37ecca1404a98b14be9d11bcc386065`.

## Proven failure and change

`patch/np_runtime.cpp::np_new` sends all thread allocations <=32 bytes and
exactly 512 bytes into the same pools used by allocating interrupts after
the first main-loop iteration. This is classification by **size, not
lifetime**. The main thread could consume all 256 small and 15 large blocks.
The next interrupt `new(12)` then calls `pool_panic` (`udf #0`) even with
ordinary heap space remaining. The patched fault handler records cause 5
and resets the device. Returning null is not a safe fix: stock callers
dereference the returned allocation without checking it.

The saved-mod-note application path does allocate: `0x081209F4` walks the
linked records at project-manager `+0x1BC`, converts each matching stored
uint16 to float, allocates a 12-byte controller event at `0x08120A42`, and
dispatches it at `0x08120A5E`. That alone does **not** prove pool exhaustion:
the dispatch is immediate and events can be released before the next record.
The amount of outstanding memory in a real project remains unknown.

Fix: limit **thread** pool admission to the first 192 small blocks and 7
large blocks. The final 64 small (2 KiB) and 8 large (4 KiB) blocks cannot
be consumed by threads. Interrupts retain access to the entire pools;
threads retain their existing ordinary-heap fallback. This is enforced by
the bitmap search range, not a racy usage-counter check. ISR allocation,
free logic, memory addresses, total pool footprint, and heap limit are
unchanged. No extra masking, null-return path, or heap use in ISR was added.

Trade-off: heavy thread allocation falls back to the ordinary heap earlier.
This can increase heap churn / malloc IRQ-off time; existing DWT counters
measure it. The reserve is bounded, **not infinite buffering**. If interrupts
themselves fill the remaining reserve, or the ordinary heap genuinely fills,
the existing fatal-allocation path remains. No watchdog or fault protection
was disabled.

## Tests and limits

`tests/test_pool_reserve.py` executes the installed `operator new` splice,
real pool bitmap allocation, and both installed delete entry points.
Ordinary malloc alone is isolated by a non-overlapping, bounded host bump
allocator; the test asserts it is never reached with IPSR != 0.

1. Hold 512 small (8/12/16/20-byte) and 32 large thread allocations.
2. RC5: the first ISR `new(12)` executes the fault path, failure count = 1,
   while ordinary heap remains available.
3. Candidate: 64 further small ISR allocations plus 8 large succeed with
   disjoint addresses, no ISR heap call, and failure count = 0.
4. Free alternating ISR blocks via one delete entry, reallocate them, then
   free all via the other. Bitmaps and used counters recover correctly.

```sh
build/venv/bin/python tests/test_pool_reserve.py --image build/firmware-env-rc5.bin --expect-old-fault
build/venv/bin/python tests/test_pool_reserve.py
```

`tests/test_modnote_dsp.py` creates the real DSP object at `0x200047F0+0x44`,
installs its 171 real CParams through the stock constructors, fills decoded
mod-note banks at project `+0x4268/+0x5A74`, sets the active-bank flags,
and calls the real evaluator `0x081156C4`. Then it triggers main ADSR and
all 14 X-Envs and executes `0x08112488` through its **real return**. The
waveform-to-CV mapper / double-precision instructions are **not stubbed**:
Unicorn ARM MAX supports the FP64/VRINTA instructions missing in its M7
model. Only GPIO output is isolated; RNG uses the stock xorshift with a
synthetic System registration and nonzero seed.

100 sequential changes x 100 frames cover parameter endpoints, all four
LFO modes, five waves, both envelope destinations and morph 0/0.5/1.
An identical-seed A/B run compares the entire DSP object and all 21 RC
state entries after each frame: **bit-identical to RC5**.

```sh
build/venv/bin/python tests/test_modnote_dsp.py --compare build/firmware-env-rc5.bin
```

This is **not** a project deserializer test, panel/sequencer replay,
cycle-accurate timing test, or emulation of interrupt interleavings.
The synthetic pool-pressure test does not prove that a real mod note
creates that pressure. Those distinctions are important.

Also passed: `make test`, static final-image / branch / SYX round-trip
checks, `test_patch.py`, UI suites (132 + 264 scenarios), scale-random
regressions, and all RC envelope suites (including 72 x 240 renderer ticks).
The two new regressions are included in `make test-patch`.

## Candidate for hardware confirmation

`build/mono_V2.2.0r-modnote-isr-reserve-test.syx` (same as generic patched):

- MD5: `09b5b6b2c05fe32135cac6986319debc`
- SYX SHA-256: `9d0b3a43f9c17403e9dd21686461c60d9f62d33b2308369b4e4dd2cc13eb30f6`
- BIN SHA-256: `bb8f79f68aa23afc3c94437f79314c4ece844eca7cb624bbedf387728c654731`
- Image: 488704 bytes; 8952-byte blob; 212 differing factory bytes inside
  the existing verified spans. No new stock patch site was introduced.

Only the allocator's thread-admission policy changed. ADSR/X-Env code,
save format, LFO, scale randomization and UI logic are unchanged; link-time
addresses of subsequent blob symbols shifted. D-cache, MPU, DMA,
calibration, bootloader and watchdog policy were not changed. RC5 remains
available for rollback. Back up the project before another flash.

Next decisive evidence: export containing a failing mod note; whether the
reset is immediate or follows ~16 seconds of stalled UI; ideally retained
fault fields `0x2000E400..0x2000E43F` after a warm reset, before power-off.
Cause 5 would support pool exhaustion, a stacked PC elsewhere would point
to a different failure, and a watchdog reset requires a timing/liveness
investigation. Do not patch more unrelated code until that distinction is
known if this candidate does not stop the reported failure.

## Follow-up (same evening): the reserve did not help; the fatal path itself removed

Hardware result for the candidate above: still reboots, and so does a
**cleared** mod-note slot — pressing an empty slot copies the whole current
patch into it (manual: "the current patch will be copied to that mod note
slot"), which is the largest event burst the instrument produces, and it is
raised from the key-scan interrupt path.  The thread reserve cannot help
there: the burst is the interrupt's own allocations, and once the 256 small
/ 15 big blocks are gone `np_new` executed `udf` — fault cause 5, reset.
That path is the only fatal one the patch ever added, and it is the only
explanation on the table that fits every observation: a short sound first
(the note is applied before the copy burst), a reset rather than a hang,
old and empty slots alike, and no dependence on the envelope code.

Change (`patch/np_runtime.cpp::np_new`): pool exhaustion from an interrupt
now falls back to the stock heap under the stock PRIMASK malloc lock — the
exact path the stock firmware used for every ISR allocation before the
pools existed — and counts the episode in `pool_failures`.  `pool_panic` is
gone; fault cause 5 can no longer be produced.  Nothing else changed.

Tests: `tests/test_patch.py` (a 16th ISR block returns the stock heap
pointer, counted, no fault) and `tests/test_pool_reserve.py` (a 100-object
ISR burst of 8..512-byte requests past both pools lands on distinct heap
blocks, `pool_failures` = 100, heap pointers reach stock free).  All suites
and the static checks pass.

Build `build/mono_V2.2.0r-modnote-heap-fallback.syx`,
MD5 `b147c3ad54d4a0e73f81bfe70787c31b`.

If it still reboots, the next step is the fault record: this build cannot
produce cause 5, so whatever it records is the real cause.

## Correction (2026-09-15, from whole-firmware emulation)

Two statements in the follow-up above were inferences, and the emulator
(`docs/sysemu.md`) shows both were wrong:

* Pressing an empty mod-note slot does **not** copy the patch in V2.2.0r —
  it plays the slot's defaults (`0x0811702C` resets the bank table; no
  `CopyPatchToModNoteData` is raised).  The copy is RECORD + note key.
* UI actions run in the **main loop**; the key-scan interrupt allocates two
  8-byte objects per key transition and a 12-byte object per pot movement
  (the pot average must move 0x180 of 0xFFFF, about 0.6 % of travel),
  nothing more.
  In every mod-note flow emulated — full copies (30 records), cleared
  slots, S&H mod notes, transport running, RECORD on — the interrupt-side
  pool never exceeded a handful of blocks and `pool_failures` stayed 0.

The heap fallback stopped the reboots on the unit, so *some* interrupt-side
allocation burst does happen there; the only interrupt handlers that can
reach `operator new` are DMA1_Stream0 (keys/pots) and TIM5 (the transport
tick, only in clock mode 3 via `0x08116E0C`).  The emulated unit runs with
factory settings (clock mode 0); the unit's settings and project are the
unknowns.  `tools/patch.py diag` (`pool_failures`, `small_high`,
`big_high`) read from the unit would settle which handler it is.
