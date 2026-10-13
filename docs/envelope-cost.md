# What the envelope curves cost per audio frame, and whether the sequencer can feel it — 2026-09-14

Question: does computing the exponentials steal cycles, and could that make
the sequencer less tight?

## Where the time goes

Everything below runs inside the EXTI2 handler, once per 32 kHz frame
(31.25 µs, 12,500 cycles at 400 MHz).  Numbers are dynamic counts of the
whole per-frame DSP update `0x08112488` executed under emulation
(`scratchpad/cost4.py`: real engine constructor, all 14 X-envelopes and the
main ADSR triggered, waveform-to-CV mapper live, run on Unicorn's
FP64-capable model so nothing is stubbed).  Emulation counts instructions,
not cycles — the cycle column is an estimate, see below.

| build | instr/frame avg | max | flash data reads avg | max |
|---|---|---|---|---|
| stock | 2,491 | 2,657 | 109 | 121 |
| + ratio X-Mod, band-limited modulator (H, H2–H5) | 2,830 | 2,988 | 159 | 171 |
| + X-Env curve ×14 and RC ADSR (X) | 3,326 | 3,799 | 240 | 381 |

"max" for the envelope build is not a corner case: every note trig fires
all 14 X-envelopes whatever their amount knobs say, so for the whole
attack+decay of every note all 14 curve calls take the polynomial path.
The average is diluted by frames after the decays have finished.

Per frame the envelopes add ~800 instructions and ~210 flash data reads
during notes.  Of that, the main ADSR (`np_env_main` + `np_env_rc`) is
about 75 instructions and 8 reads; the rest is the X-Env curve: 14 calls ×
(15 literal loads + 14 dependent FMAs + save/restore of registers and
flags).

## Cycles

> **CORRECTION (2026-09-18).** The premise below is wrong: the stock firmware
> enables **both** caches. `SCB_CCR` is written three times during boot --
> `np_early+0xce` sets IC (0x00020000), then the stock's own init at
> `sub_0810FDF0+0x24` sets IC and at `+0x72` sets IC|DC, leaving
> `SCB_CCR = 0x00030000`. So a `vldr sN,[pc,#imm]` is a D-cache hit of a few
> cycles, not an uncached AXI read, and the per-frame "uncached read" counts
> below overstate their cost by roughly an order of magnitude. Moving the
> constants into DTCM is still right -- zero wait states, no cache line spent
> on them -- but it bought a cycle each, not ten. The measured instruction
> counts in this document are unaffected; only the cycle attribution is.

The Cortex-M7 runs with the I-cache on (patch C) and the **D-cache off**
(stock never enabled it, the patch did not either).  So every `vldr
sN,[pc,#imm]` literal is an uncached read through the AXI matrix (200 MHz)
into flash at 2 wait states: of the order of 8–15 CPU cycles each rather
than 1.  The dependent FMA chain costs ~3 cycles per link.

| | cycles/frame (estimate) | share of 12,500 |
|---|---|---|
| stock | ~3,600–4,300 | 29–34 % |
| + earlier patches | ~4,300–5,300 | 34–42 % |
| + envelopes, during notes | ~6,300–8,600 | 50–69 % |
| of which the X-Env curve | ~1,600–2,900 | 13–23 % |
| of which the RC ADSR | ~200–250 | ~2 % |

So: no frame overrun is expected, but the margin is no longer generous,
and the uncertainty is dominated by one number nobody has measured on
this unit — the cost of an uncached flash read.  The device already
records the answer: the DWT `audio` row (average, max, overruns) in the
diagnostic block.  Reading it needs an SWD probe or the SysEx dump that
was proposed earlier and not built.

## What the sequencer sees

Internal clock: TIM5 (IRQ 50, NVIC preemption 2 — the row mislabelled
"SPI3" in the profiler) reloads itself from `transport+0x78`, counts
`transport+0x70` down and, at zero, sets `transport+0x88C`.  The step
itself is **not** taken in the interrupt: the main loop (`0x081199F4`)
polls that flag and calls `0x08119270`.  External clock: the F8 byte is
also dispatched from the main loop (`0x0811B700` from `0x08119938`).

Therefore the sequencer's step jitter is the main-loop iteration time,
not anything inside the audio frame.  The audio handler affects it only
through its CPU share: a main loop that got 60 % of the CPU and now gets
45 % runs its iterations a third slower, so whatever its latency was
stretches by that factor — a proportional stretch, not a new source of
jitter.  The one way the envelopes could break sequencer timing
qualitatively is a frame overrun (back-to-back EXTI2 at priority 1 starves
TIM5, SysTick and the main loop), and the estimate says that does not
happen.

Note that patch K put EXTI2 above TIM5 (priority 1 vs 2).  Before it, the
transport tick preempted the audio frame; now a tick that lands during a
frame waits for the frame to finish, up to ~15–20 µs.  Since the step is
dispatched by the main loop hundreds of microseconds later anyway, this is
invisible.

## Cheaper ways to get the same curve, if the margin is wanted back

1. **Fewer literals per call.**  `e^{3.6x} = (e^{0.9x})^4` with a degree-7
   Taylor on `[0, 0.9]` then two squarings: 7 loads instead of 15, 12 FP
   ops instead of 15, chain ~36 instead of ~45 cycles; max error 8.1e-6,
   inside the existing 1.1e-5 test tolerance.  About −1,000 cycles/frame.
2. **Constants in DTCM.**  Copy the coefficients into the diagnostic block
   at boot; loads become 1–2 cycles.  With (1): the curve drops to roughly
   40–50 cycles per call, ~600–700 per frame — a third of today's cost.
3. **A 513-entry table + linear interpolation** (the stock's own idiom for
   its curves): 2 adjacent reads and ~10 FP ops per call, max error
   6.4e-6.  In flash it costs 2 KB and 2 uncached reads per call; in DTCM
   there is no room.
4. **Trim the per-site save/restore** (~30 cycles × 14): clamp with
   `vmaxnm/vminnm` instead of `vcmp` so the helper touches no flags, and
   the APSR/FPSCR round trip goes away.
5. **Skip idle envelopes** is already the case (`x <= 0` takes the fast
   path); skipping by amount would need the site to know the amount.

None of these changes the sound.  (1)+(2) is the recommended pair: small,
verifiable by the existing curve/ABI tests, and it removes the dependence
on the unmeasured flash-read cost.

## Done: (1) + (2), same date

Implemented in `patch/np_xenv.S`: `e^{3.6x} = (e^{0.9x})^4` from a
degree-7 series, seven constants in DTCM at `DIAG+0x140` (`np_early`
copies them from the flash master `np_xenv_consts` on every boot; header
`+0x120`/`+0x124` point at both).  The helper now clobbers `r12` as well
as `s13`/`s14` — every caller already saved it.  Max error against the
exact curve 8.05e-6 (was 3.1e-6), monotone, endpoints exact, all suites
green.

Re-measured with the same fixture:

| build | instr/frame avg | max | flash data reads avg | max |
|---|---|---|---|---|
| stock | 2,491 | 2,657 | 109 | 121 |
| + earlier patches | 2,830 | 2,988 | 159 | 171 |
| + envelopes, before | 3,326 | 3,799 | 240 | 381 |
| **+ envelopes, now** | 3,331 | 3,785 | 173 | **185** |

The envelopes' uncached flash reads went from +210 to +14 per frame
(the remaining 14 are `np_env_rc`'s own literals); instructions are
unchanged.  Estimated cost of the envelope patch during notes: ~1,000–1,200
cycles per frame (8–10 % of the deadline) instead of ~2,000–3,300.
Whole-frame estimate for the current image: ~5,400–6,500 cycles, 43–52 %.

Build `build/mono_V2.2.0r-env-rc2.syx`, MD5 `0429554ed5e017445f4c218fcb389bfc`.

## Round two: where else, same date

Asked whether a cruder formula, or "data inside the code" instead of
flash, would win more.  Measured answers:

* **Constants inside the code.**  ARM has no float immediates beyond the
  VFP set (±(16..31)/16·2^n: 0.5, 1.0, 2.0 …); anything else has to be
  `movw/movt` + `vmov` — three instructions and ~3 cycles, no memory, or a
  DTCM load — one instruction, 1–2 cycles.  GCC does the former with
  `-mslow-flash-data`, which is now on for `np_lfo.cpp`: every float
  literal in the ratio/waveform/ADSR code left the literal pool.  Blob
  flash data reads per frame went from 74 to 11 (the 11 are genuine table
  lookups — ratio, division), for +110 mostly dual-issued instructions.
  The image now reads flash data 122 times per frame, of which 121 are
  the stock's own.
* **Cruder formula.**  The polynomial chain is ~13 dependent FP ops,
  ~40 cycles per call, ~560 per frame.  A degree-5 series with three
  squarings saves about 6 cycles a call — 85 a frame, 0.7 % — for ten
  times the error.  A 513-entry DTCM table is ~25 cycles a call, ~200 a
  frame, 1.6 %, for 2 KB.  Neither is the lever.
* **The site glue was.**  Each of the 14 sites pushed r2, r3, r12, lr,
  read and wrote FPSCR and APSR, and pushed s12–s15.  The stock never
  reads FPSCR as a value (475 `vmrs APSR_nzcv`, zero `vmrs rN`), so its
  cumulative bits are unobservable; and the helper is now branch-free
  except a `cbz` on the raw bit pattern for idle envelopes (exactly
  +0.0), clamps with `vmaxnm/vminnm`, and therefore writes no flags at
  all.  A site now saves r3, lr, s13, s14 — 16 bytes, six instructions.
  The C-call site for the ADSR keeps both flag words because the C code
  compares.

| build | instr/frame avg | max | flash data reads avg | max |
|---|---|---|---|---|
| stock | 2,491 | 2,657 | 109 | 121 |
| + earlier patches (as shipped before today) | 2,830 | 2,988 | 159 | 171 |
| + envelopes, first version | 3,326 | 3,799 | 240 | 381 |
| + envelopes, DTCM constants | 3,331 | 3,785 | 173 | 185 |
| **now: `-mslow-flash-data`, flag-free sites** | 3,315 | 3,771 | 110 | **122** |

Estimated whole frame now ~4,900–5,600 cycles (39–45 % of the
deadline), of which everything the patch adds is ~1,200–1,400 cycles —
the envelopes ~800 of that, essentially all of it arithmetic.  The
remaining flash reads are the stock's.

What is left, in order of size: the stock's own 121 uncached reads
(~1,200 cycles; only a D-cache would remove them, and that needs a DMA
coherency review nobody has done), the band-limited modulator (~35
instructions per slot per frame, ~19 slots — a rate-gated polyBLEP would
save maybe 300 cycles), and the ADSR C call's 64-byte FP frame (~40
cycles).  Below that it is noise.  Real numbers still need the DWT
`audio` row read out of the unit.

Build `build/mono_V2.2.0r-env-rc3.syx`, MD5 `dca0864fcf56ecbac4cf5ccae3409dd1`.

## Round three: the X-Envs on the RC model too

The fourteen X-Envs now run the same RC step as the ADSR (`np_env_rc_step`,
assembly, per-envelope state in DTCM, cached one-pole coefficient).  Same
fixture, all fourteen triggered on tick 0 so the "max" frame has every
attack series live at once:

| build | instr/frame avg | max | flash data reads avg | max |
|---|---|---|---|---|
| stock | 2,491 | 2,657 | 109 | 121 |
| + earlier patches | 2,920 | 3,078 | 109 | 121 |
| + polynomial curve X-Envs, RC ADSR | 3,315 | 3,771 | 110 | 122 |
| **+ RC X-Envs (k = 4.32) and RC ADSR** | 3,982 | 4,484 | 109 | **121** |

Flash data reads are exactly the stock's (the stage dispatch is a compare
chain, not a `tbb` whose table would be a flash read).  The RC step is
heavier than the stateless curve — ~50 instructions a site in decay with
the cached coefficient, ~85 in attack with the series — so the worst
frame is ~4,500 instructions: estimated ~5,900–6,200 cycles, 47–50 % of
the deadline, average frames ~44 %.  An envelope settled at zero in idle
costs ~20 instructions (early-out).  No overrun expected; real numbers
still need the DWT `audio` row.

Build `build/mono_V2.2.0r-env-rc4.syx`, MD5 `ba7cd6b6de97afc62322cb3618c10d09`.
