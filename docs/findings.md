# Norand Mono — synth engine internals

Working notes on the **application** image (`mono_V2.2.0r.syx`), covering what
was recovered about the sound engine, the modulation matrix and the parameter
system. The container format, the memory map and the peripheral inventory are
in [architecture.md](architecture.md) and [protocol.md](protocol.md); the
defects and the patches are in [audit.md](audit.md) and
[patches.md](patches.md). This file is the reference the other three lean on.

Everything here is recovered from the binary. Where something is **inferred
rather than proven**, it says so — those are the places to be careful.

---

## 0. How to reproduce any of this

```bash
make extract          # .syx -> build/firmware.bin (479704 bytes @ 0x08100000)
make analyse          # RTTI, vtables, function starts -> build/firmware.elf + .asm
make patch            # apply the fixes -> build/mono_V2.2.0r-patched.syx
make test-patch-static # final image, branch and SysEx checks; no Unicorn
make test-patch        # dynamic harness; packaged Unicorn SIGILL caveat below
```

The packaged macOS/ARM Unicorn crashes in its host `init_cache_info`, before
emulating a single firmware instruction: the app sandbox rejects the
cache-line `sysctl` and Unicorn falls back to the privileged AArch64
`MRS CTR_EL0`.  A local Unicorn 2.0.1 build with that Apple fallback disabled
runs the complete suite; the current result is 324 passing checks.

`build/firmware.symbols.json` carries the recovered function starts and the
set of addresses proven to be code; several tools under `tools/` read it.

Ghidra, when a decompiler is needed:

```bash
# Homebrew installs ghidra but does not link analyzeHeadless onto PATH:
GH=/opt/homebrew/opt/ghidra/libexec/support/analyzeHeadless

# first import (slow, full analysis)
$GH build/ghidra norand -import build/firmware.elf \
  -processor ARM:LE:32:Cortex -scriptPath tools/ghidra \
  -preScript NorandRam.java -postScript NorandProbe.java

# afterwards, reuse the analysed project (fast)
$GH build/ghidra norand -process firmware.elf -noanalysis \
  -scriptPath tools/ghidra -postScript NorandSlotEval.java
```

`NorandRam.java` must run as `-preScript`. The ELF carries only flash, so
without RAM blocks mapped *before* analysis every global access is recorded
as a bare constant and "who writes this field" returns "nobody" regardless of
what the code does. That mistake produced one wrong finding already.

For "who touches `object + 0xNNN`", the disassembler beats Ghidra: the
compiler forms `movw r3, #0x2f20 / add r3, r0 / vldr s13, [r3]`, which has no
literal to search for and no reference for Ghidra to build. `tools/findfield.py`
and the folding scanner pattern in it walk every instruction and resolve
base+displacement pairs.

Its limit, learned the hard way: it follows a base register for a bounded
window after the immediate that formed it. When the base is set once at
function entry — `add.w r5, r0, #0x2000` at the top of an 11 KB function —
every use is out of range and the field looks unwritten. Two wrong findings
came out of that. If a scan says "nobody writes this", check what the
enclosing function's base register actually is before believing it.

---

## 1. The parameter system

### 1.1 CParam

Every user-facing value is a `CParam`, 32 bytes, allocated inside its owner:

| off | type | meaning |
|-----|------|---------|
| 0x00 | ptr | vptr — 3 slots: `~D0`, `~D1`, **`apply()`** |
| 0x04 | u8 | local index within the owner (0..8) |
| 0x05 | u8 | flag; 1 on the parameters that accept a ramp |
| 0x08 | ptr | owner (the modulation slot) |
| 0x0C | float | **value** |
| 0x10 | float | ramp target |
| 0x14 | float | ramp start |
| 0x18 | float | ramp step |
| 0x1C | u32 | ramp steps remaining |

`CParam::setValue` is `0x0811CBBC(param, rampSteps, value)`: stores the value
(or sets up the ramp) and then calls vtable slot 2, the `apply()` handler.

Reading a parameter by global index: `0x08116C64(project, idx)`, which is
`project[(idx + 0x181C) * 4 + 4]` — a pointer array of 171 entries at
**`project + 0x6074`** — then `param->value`, advancing the ramp by one step
if one is pending. Note `idx < 0xAB` (171).

### 1.2 Discrete parameters

A parameter with N discrete positions stores its integer as **`n / 65536.0f`**
and is decoded with `(unsigned)(value * 65536.0f) & 0xFF`. Exactly three
instructions in the image use the 65536.0 constant, and all three are
`apply()` handlers:

| handler | class | what it does with n |
|---------|-------|---------------------|
| `0x0811BB50` | `XEnvDestCParam` | `owner+0x26 = (n != 0)` |
| `0x0811BB80` | `LfoWaveformCParam` | `MultiWaveOscFast::setWaveform(owner+0x28, n)` |
| `0x0811BD48` | `LFOTimeCParam` | X-Mod type — see §3 |

**Consequence worth knowing:** nothing else in the firmware turns a discrete
parameter's float back into an integer. There is no LED or display lookup
table indexed by one of these, so widening a discrete parameter's range
cannot cause an out-of-bounds read anywhere. That search — three uses of the
65536.0 constant, one reader of the wrap table — is also what made it easy to
miss the three UI copies below, which never decode the float at all: they
work on the raw byte with their own modulus.

The wrap count lives in a `ushort` table at **`0x0815E8C8`**, indexed by the
*local* parameter index, and is applied as `raw % table[local]` in the
modulation evaluator. It has exactly one reader (literal pool `0x08115804`).

| local | count | parameter |
|-------|-------|-----------|
| 0..5 | 65535 | the continuous ones |
| 6 | **5** | LFO waveform (five waveforms) |
| 7 | **3** | X-Mod type (raised to 4 by the patch) |
| 8 | **2** | X-Env destination |

**This table is not the only bound.** Each of the three UI actions that cycle
a discrete parameter carries its own copy of the count, as a literal in the
conditional arm that picks local 7 over local 6:

| site | shape |
|------|-------|
| `0x081500FE` | `itete ne / movne r7,#5 / moveq r7,#3 / movne r6,#6 / moveq r6,#7` |
| `0x08150550` | same with `r8` and `r7`, feeding a `udiv` |
| `0x081506EA` | same with `r3` and `sb`, feeding an `sdiv`, arms swapped |

Raising only the wrap table leaves the type unreachable from the controls.
All four have to move together.

---

## 2. The modulation matrix

### 2.1 Nineteen destinations, nine parameters each

The "19 modulation slots" are the **19 CV destinations**. Their descriptors
are 16-byte records at **`0x0815E8E0`**: `{const char *name, u32 kind,
u32 max, u32 …}`.

| slot | name | kind | max |
|------|------|------|-----|
| 0 | VCO1Tune | 2 | 127 |
| 1 | VCO1Fine | 0 | 65535 |
| 2 | VCO1Wave | 0 | 65535 |
| 3 | VCO1Vol | 1 | 65535 |
| 4 | VCO2Tune | 2 | 127 |
| 5 | VCO2Fine | 0 | 65535 |
| 6 | VCO2Wave | 0 | 65535 |
| 7 | VCO2Vol | 1 | 65535 |
| 8 | **VCOTZFM** | 1 | 65535 |
| 9 | FiltFreq | 2 | 150 |
| 10 | FiltReso | 0 | 65535 |
| 11 | FiltColor | 0 | 65535 |
| 12 | FiltEnv | 0 | 65535 |
| 13 | EnvAttack | 3 | 65535 |
| 14 | EnvDecay | 3 | 65535 |
| 15 | EnvRel | 3 | 65535 |
| 16 | EnvSust | 3 | 65535 |
| 17 | OutVol | 0 | 65535 |
| 18 | Sync | 5 | 1 |

19 × 9 = 171 = 0xAB, which is the bound in the parameter getter. **Global
parameter index = `slot * 9 + local`.** X-Mod is VCOTZFM, **slot 8**.

`kind == 2` (VCO1Tune, VCO2Tune, FiltFreq) selects a different follow-amount
computation in the evaluator — an octave-style quantiser rather than a linear
scale. The low byte of `max` is used as its divisor.

### 2.2 The slot object

Built by `0x0811CD08(this, slotIndex)`; five subclass constructors call it
(`0x0811CF60`, `0x0811D0DC`, `0x0811D268`, `0x0811D3E4`, `0x0811D404`).

| off | meaning |
|-----|---------|
| 0x04 | **u8 slot index** (0..18) — the branch condition in every audio-rate path |
| 0x24 | u8, gates whether a ramp is honoured |
| 0x25 | u8 — see §3.3; stock writes 0 or 1, the patch adds 2 |
| 0x26 | u8, written by `XEnvDestCParam` |
| 0x28 | the LFO/oscillator sub-object (its first word is the vptr) |
| 0x34, 0x58, 0x84, 0xA8, 0xCC, 0xF0 | six phase accumulators |
| 0x74, 0x114 | u8 flags set by the phase reset |
| 0x10C | **u8 X-Mod type**, = `(owner+0x28)+0xE4` |
| 0x118 | `MedianValueCParam` (local 0) |
| 0x138 | `LfoWaveformCParam` (local 6) |
| 0x158 | `LFOLevelCParam` (local 1) |
| 0x178 | `LFOSpeedCParam` (local 5) — **the X-Mod RATE**; value at 0x184 |
| 0x198 | `LFOTimeCParam` (local 7) — **the X-Mod TYPE**; value at 0x1A4 |
| 0x1B8 | `XEnvDestCParam` (local 8) |

The LFO sub-object at `owner+0x28` is a `synthpp::MultiWaveOscFast`. Its
vtable is at `0x08172F7C`; slot 4 is `setMode` (`0x081562C8`, the **only**
writer of `owner+0x10C`) and slot 5 is the default retrigger
(`0x0811BA3D`). Sibling classes — `SineOscFast`, `TriangleOscNaive`,
`SquareOscNaive`, `SawtoothOscNaive`, `SAndHOsc` — put something else in
slot 4, so **dispatch through the vtable, never call `0x081562C8` directly**.

### 2.0 The MIDI clock estimator, and where sync jitter comes from

`0x08118B00`, the F8 handler, decompiled:

```c
counts = accumulated + TIM3_CNT;   TIM3_CNT = 0;   /* 0x40000C24 */
est    = (timerHz / counts) * (128.0/3.0);         /* engine ticks per second */
buf[(idx++) & 0xF] = (short)est;                   /* 16-entry ring at +0x48 */
sum    = buf[0] + ... + buf[15];                   /* fully unrolled */
transport[0]      = (sum & 0xFFFFF) >> 4;          /* = sum / 16 */
transport[0x1E]   = timerHz / transport[0];        /* timer reload */
```

Two things follow, and they answer "is USB sync as tight as DIN?" better
than guessing did:

* **Tempo is a 16-tap boxcar average of hardware-timed intervals.** The
  interval is measured with TIM3's counter, not in software, and one clock's
  error is divided by sixteen. USB's 1 ms frame quantisation is therefore
  largely rejected from the *rate*: at 120 BPM a clock is 20.8 ms, so +-0.5 ms
  of framing is +-1.4 % per clock and about 0.35 % after averaging.
* **Phase is not averaged.** A synced LFO retriggers when the F8 is
  *processed*, so it inherits the full arrival jitter — and `sub_0811B700`,
  the packet pump that dispatches realtime, is called from **main**
  (`sub_08119938` at `0x08119A4A`), not from an interrupt. So DIN and USB
  alike carry the main loop's scheduling jitter into the retrigger instant;
  USB adds frame quantisation on top of it.

The number that decides how tight sync feels is therefore the main-loop
period, which the DWT profiler already records: `python3 tools/patch.py diag`,
row `main_period`, maximum column. Nothing else in this chain is soft.

### 2.2a The audio rate, and how accurate the pitch tables are

**`fs = 32000 Hz`, read out of a literal** rather than inferred: the slot
factory loads `32000.0` at `0x08147424` (`vldr s18, [pc, #0x2d4]`) and stores
its reciprocal into `osc+0x20`, which is what `MultiWaveOscFast::setRate`
(`0x08156910`) multiplies a frequency by to get a phase increment. The
performance notes' "12,500 cycles / 31.25 us" deadline agrees.

The signal path itself is analog — the manual is explicit, "all analog
signal path… voltage-controlled by powerful Digital-to-Analog converters" —
so there are no digital oscillator waveforms. The only digital audio-rate
thing in the instrument is the modulator, whose samples drive a VCO's
frequency CV through a DAC. That is why band-limiting it (H4) matters.

**The frequency table at `0x0817379C` is accurate.** 257 entries spanning
MIDI 0 to 127, 0.496 semitones apart, read out and measured:

* nodes versus exact equal temperament: max error **0.00017 cents**
* linear interpolation between nodes: worst **0.178 cents**, rms 0.131
* an FM ratio looks both operands up in it, so the ratio error is bounded by
  **0.36 cents** — well under the ~5-cent difference limen

So a ratio being audibly off is not a table problem.

**Nyquist headroom.** The table tops out at 12543.9 Hz, 78 % of Nyquist. A
modulator's usable harmonic count is `16000 / f`:

| modulator | harmonics below Nyquist |
|---|---|
| 130 Hz (C3) | 122 |
| 1.0 kHz | 15 |
| 2.1 kHz | 7 |
| 4.2 kHz | 3 |
| 8.4 kHz | 1 — a sine, whatever waveform is selected |

At C3 even ratio 15.59 lands at 2 kHz with seven harmonics, so the ratio knob
has headroom across its range; it is high *notes* that run out. Band-limiting
does not add headroom — it makes running out sound like a sine fading in
rather than like folded noise.

### 2.2b Per-slot evaluators, and the X-Envelope destination

Each slot's vtable slot 2 is its per-block evaluator. Five distinct ones,
read out of the factory at `0x0814746C` and the five constructors:

| evaluator | slots | reads `slot+0x26` |
|---|---|---|
| `0x0811C464` QuantizedLinAD | **0 VCO1Tune, 4 VCO2Tune**, 9 | yes, `0x0811C6B2` |
| `0x0811108C` LinAD | 1, 2, 5, 6, 10, 11, **12 FiltFreq**, 17 | yes, `0x0811129A` |
| `0x08110894` ExpAD | 3, 7, **8 VCOTZFM (X-Mod)** | yes, `0x08110ABA` |
| `0x0811C200` | 13, 14, 15, 16 | **no** |
| `0x0811BADC` | 18 | **no** |

`slot+0x26` is the X-Envelope destination, written only by
`XEnvDestCParam::vfn2` (`0x0811BB50`) as `(value * 65536) != 0`. Those three
loads are its only readers in the image — established by scanning raw Thumb-2
encodings, since neither disassembly corpus is complete here.

All three share one shape:

```
ldrb.w r2, [r4, #0x26]      ; the flag
...
cmp    r2, #0
beq.w  <envelope goes to the destination>
cmp    r3, #4               ; r3 = slot+0x10D, this slot's own waveform
bhi.w  <bail>
tbb    [pc, r3]             ; render this slot's own LFO
```

So the toggle is **per slot**, and it means "this slot's envelope scales
*this slot's own* LFO depth instead of driving the destination". For slot 8
that LFO is the FM modulator, which is why the toggle reads as "the envelope
controls the X-Mod amount". It does not reach across slots: everything after
the branch is addressed off the same `r4`.

Two facts that follow, both verified rather than argued:

* **None of the three evaluators reads `slot+0x04`**, the slot index. They
  are slot-agnostic, so VCO1Tune and VCO2Tune — which share evaluator
  `0x0811C464` — execute byte-identical code. The firmware cannot behave
  differently between the two oscillators at this point; a difference has to
  be in the flag's value or downstream of the evaluator.
* The stock `LFOTimeCParam::vfn2` **does** call `setMode` through vtable slot
  4 (`ldr r3,[r0,#0x28] / adds r0,#0x28 / ldr r3,[r3,#0x10] / blx r3` at
  `0x0811BD60`–`0x0811BD74`), so `np_xmod_time` doing the same adds nothing.

### 2.2c What the X-Envelope destination flag actually computes

From Ghidra's decompiler on `0x0811C464` (slots 0, 4, 9), and identical in
shape in the other two classes:

```c
if (slot->xenvDest == 0) {                 /* FUNC+TYPE+WAVE off */
    lfo = amount * waveshape;
    out = base + envelope + lfo;           /* env and LFO both added   */
} else {                                   /* FUNC+TYPE+WAVE on  */
    out = base + (envelope + amount) * waveshape;   /* env joins depth */
}
```

Everything is addressed off the same slot pointer: the flag never reaches
across to another slot. So "the envelope controls the X-Mod amount" is the
slot-8 case of a general per-slot feature, and enabling it on VCO2Tune
redirects **VCO2's** envelope into **VCO2's own** LFO depth.

The flag itself is `(raw & 0xFF) % table[local]` with `table[8] == 2`, and
the parameter index is `9 * slot + local` — no slot-4 special case anywhere
for `local >= 6`. The two `param_2 == 4` branches in the evaluator, and the
`(param_2 & 0xFB) == 0` gate, are all inside the `local < 6` path.

**Consequence:** slots 0 (VCO1Tune) and 4 (VCO2Tune) share evaluator
`0x0811C464`, that evaluator never reads `slot+0x04`, and the flag is
computed symmetrically. Nothing in this path can behave differently between
the two oscillators.

**Correction (2026-10-05): the three evaluator loads are not the only readers.**
VCO1Tune, VCO2Tune and the cutoff are evaluated *inline* in the DSP block,
off the engine (`r4`), with their owners at `+0x48`, `+0xAAC`, `+0x17A8`; the
flag of the first two is read at `0x08112504` (`ldrb.w r2,[r4,#0x6E]`) and
`0x0811295E` (`[r4,#0xAD2]`).  The scan above only looked for offset `0x26`.
No reader of `engine+0x17CE` (the cutoff's flag) was found — not followed
up.  Measured on sysemu (p35, VCO1 FREQ, audio-rate X-Mod, note on, VCO1
pitch CV `engine+0x2EDC` per sample, 1 unit ≈ 1 semitone): with the flag
clear the X-Env moves the mean pitch (+2.5 / +9.5 units at 10–50 / 70–120 ms)
and leaves the FM depth alone (p2p 0.2); with FUNC+TYPE+WAVE the mean pitch
stays put (−0.1 / −0.5, bin noise of an audio-rate wave) and the depth follows
the envelope (p2p 2.5 → 9.2 with X-Mod amount 0; 8.8 → 15.9 with it set —
amount + envelope).  The behaviour of §2.2c holds for the inline copies.

### 2.2d The LFO waveforms are inlined per arm, and they are the naive ones

Each of the three evaluators carries **two complete copies** of the five
waveform generators — one for each arm of the X-Env branch — dispatched by
`tbb` on `slot+0x10D`. For `0x0811C464` the tables are at `0x0811C6EA`
(flag set) and `0x0811C89C` (flag clear).

They are the naive shapes. The sine in both arms decompiles to the stock
parabola `x * (2 - |x|)`; the square is a bare sign test; the saw is
`phase + phase`.

`MultiWaveOscFast`'s own renderers at `0x0815661A` / `0x08156678` /
`0x081566B0` / `0x081566EC` — the ones patch H2/H3 replace with the
band-limited versions — are a **separate** set of bodies. The slot
evaluators do not call them; they share only the phase and increment fields.
So the band-limiting does not reach a modulator rendered through a slot
evaluator. Six inlined copies would have to be replaced to change that.

### 2.3 The evaluator: `0x081156C4(project, slot, local)`

Computes a parameter's modulated value and writes it back through
`CParam::setValue`. Callers: `0x08116D48`, `0x0811612C`, `0x08116EE8`,
`0x081164B4`, `0x08116D9C`.

Shape, for `local < 6`:

```
value = base
      + gate * (note + project[0x6004] - 57.0f) * followAmount     <- [FOLLOW]
      + <four more modulation terms, each amount * depth>
      + offset
raw   = clamp(value, 0, 0xFFFF)
param->value = raw / 65536.0f
```

Three things fall out of this:

* **[FOLLOW] reaches only local indices 0..5.** The guard is literally
  `if (local < 6)`. Locals 6, 7 and 8 (waveform, X-Mod type, X-Env dest) are
  set directly and take the `raw % table[local]` path instead. This is why
  [FOLLOW] applies to the oscillators and the cutoff and nothing else.
* **The firmware's [FOLLOW] reference note is 57 — A3**, not 60. The custom
  X-Mod type deliberately uses C1 instead, so it retains an audio-rate
  frequency in the Mono's low register; it is not intended to match [FOLLOW]
  at its root.
* **`gate` is 1.0 for slots 0 and 4** (the test is `slot & 0xFB`, true only
  for VCO1Tune and VCO2Tune) — those always track the keyboard fully.
  Everything else gates on `project[0x6044]`.

### 2.4 Where the played note lives

Written by the note handlers `0x081170E8` (note on) and `0x08117594`
(note off / voice steal), both reached from `0x08117A44`:

| project offset | type | meaning |
|----------------|------|---------|
| **0x5FCC** | float | the note VCO1Tune follows |
| **0x5FD0** | float | the note VCO2Tune follows |
| 0x5FD4 | float | velocity, 0..1 |
| 0x6004 | float | transpose, added to the note before the −57 |
| 0x6050 | u8 | the source/channel byte of the last note |
| 0x6028 | ptr | head of the held-note list; nodes carry the note at +0x0C |
| 0x6030 | u32 | held-note count |

`CNoteData` events carry the note as a **float** at `+0x08` and velocity
normalised 0..1 at `+0x0C`; MIDI out multiplies the latter by 127.0. The
held-note node is 24 bytes and takes `event+0x04..0x13` into `node+0x08`
(`ldmia`/`stmia` at `0x081172C6`), so the note is at `node+0x0C`.

**The two note fields are not simply "VCO1's note" and "VCO2's note".** Which
one an incoming note lands in depends on the path:

| condition | `0x5FCC` | `0x5FD0` | site |
|---|---|---|---|
| nothing held | new | new | `0x0811739A` |
| legato | previous | new | `0x0811728A` |
| slide (`event[4]==0 && head[8]==1`) | new | previous | `0x0811757C` |
| note off, stack left | remaining | remaining | `0x0811782E` |
| project init | same literal | same literal | `0x081162C8` |

Those five are every writer in the image. Anything that wants "the note that
just played" cannot read one field: it has to compare them, and fall back to
a note captured at the handler when they disagree. The X-Mod ratio patch does
exactly that — see patches.md, *What the ratio is taken against*.

---

## 2.5 Two base pointers, and a warning

`sub_08112488` — the per-block DSP update — and `sub_081473F4` — the
constructor that builds the modulation slots — do **not** take the project.
They take an engine sub-object at **`project + 0x44`**:

```
08115036  bl   0x0811621C      ; r0 = 0x200047F0, the project
0811503A  adds r0, #0x44       ; r0 = the engine
0811503C  bl   0x08112488
```

Inside those two functions every offset, `r5 = r4 + 0x2000` included, is
engine-relative. The CParam handlers, by contrast, call the getter and use
project-relative offsets. **The two sets of numbers differ by 0x44 and are
easy to mix up** — an earlier revision of this file did, and shipped a patch
that read the note 0x44 bytes off and therefore never moved.

| in `sub_08112488` / `sub_081473F4` | same field, from the project |
|-----------------------------------|------------------------------|
| `+0x48` slot 0 (VCO1Tune) | `+0x8C` |
| `+0x16C` VCO1Tune's value | `+0x1B0` |
| `+0x2E68` slot pointer array | `+0x2EAC` |
| `+0x2EDC` VCO1 pitch, 0..126 | `+0x2F20` |
| `+0x2EE0` VCO2 pitch, 0..126 | `+0x2F24` |
| `+0x2F04` VCO1 pitch as a DAC code | `+0x2F48` |

Anything that has to reach project state from inside the DSP update should
use the singleton's fixed address, `0x200047F0` — `0x0811621C` is
`ldr r0,[pc,#0] / bx lr` over that literal, so it is a constant, not a
lookup. That is what `np_xmod_pos` does.

## 3. The audio-rate X-Mod path

### 3.1 The frequency table

**`0x0817379C`, 257 floats.** Entry *i* is MIDI note `i * 127/256`:
entry 0 = 8.175799 Hz (MIDI 0), entry 256 = 12543.85 Hz (MIDI 127). One
table step is 0.49609 semitone.

So a normalised position `p ∈ [0,1]` maps to **MIDI note `127p`**, and
**one semitone is exactly `1/127` of the position.** Index is `p * 256`,
linearly interpolated. Two neighbours: `0x08172F94` is the free-running LFO
rate table (same 257-entry shape) and `0x0815F320` is the 15-entry sync
division table (8192 … 64, including the triplets and the quintuplet).

### 3.2 The 126 vs 127 question, settled

The two `apply()` handlers compute an audio-rate position as
`(knob − 0.5) + project[0x2F20]/126` (slot ≠ 0) or
`… + paramValue(project,0) + project[0x2F60]/126` (slot 0).

`project+0x2F20` is **live**, and it is the same field the per-block loop
uses: the loop's `engine+0x2EDC` *is* `project+0x2F20`. It is written every
block at `0x08112810` as `126 * clamp01(pitch) + 2*(fine − 0.5)`, clamped to
`[0, 126]`, and then turned into a DAC code — so it is VCO1's pitch CV.

That settles the divisor. The value is `126 × position`, so dividing by 126
recovers the position, and the position indexes the 257-entry table across
its full MIDI 0..127 span. **126 is correct**; 127 would introduce a 0.79 %
per semitone error rather than remove one.

An earlier patch (G3) rewrote those four literals to 127. It was withdrawn,
which was the right outcome — but the reasoning given at the time was wrong
twice over: the fields were called dead (a scan that missed a base register
established at function entry, thousands of instructions from its use), and
the listening test that "confirmed" the original claim could not have
distinguished it from its opposite.

### 3.3 Where the pitch is actually decided

**`0x08112488` is the per-block DSP update**, called with the engine
(`project+0x44`; see §2.5). Inside it, at **`0x08112C70`**, a loop walks the
19 owner pointers at `engine+0x2E68 .. +0x2EB0` and, for every owner with
`+0x25 != 0`, recomputes the position and calls `setRate`:

```
rate = owner->0x184                       (the LFOSpeed value, smoothed)
p    = (rate - 0.5)
     + (owner->4 == 0 ? <slot-0 base>     (a shaped depth term, not a pitch)
                      : engine[0x2EDC] * (1/126))   /* = project+0x2F20 */
setRate(owner+0x28, freqTable[p])
```

This runs **every block**, so whatever an `apply()` handler wrote survives at
most one block. Any change to an audio-rate modulator's pitch has to be made
here or it does nothing. That is the single most important structural fact in
this file, and it is what made the G3 patch pointless.

`engine+0x2EDC` and `+0x2EE0` (project `+0x2F20`, `+0x2F24`) are VCO1 and
VCO2 pitch in 0..126 units, `126 * clamp01(masterPitch) + fineTune`, written
at `0x08112810` and `0x08112C4A`. The first is converted through a
calibration table into a 16-bit DAC code at `engine+0x2F04`, so it is the
analogue oscillator's pitch CV — which is why its slope in CV units per
semitone is a property of the unit, not of the frequency table.

### 3.4 The type dispatch

`LFOTimeCParam::apply` (`0x0811BD48`) and `LFOSpeedCParam::apply`
(`0x0811BBA8`) are the same body twice, differing only in where the type
comes from — decoded from the parameter's own value in the first, read back
from `owner+0x10C` in the second:

```
0 free LFO    owner->0x25 = 0 ; rate = slowTable[value]
1 synced      owner->0x25 = 0 ; rate = transportRate / divTable[(uint)(value*15)]
2 audio rate  owner->0x25 = 1 ; rate = freqTable[audio position]
else                            rate = 0
```

Two stock defects are visible here:

* `LFOTimeCParam`'s copy builds its audio-rate position from `self->value`,
  which for that class is the **type** (0, 1, 2 as `n/65536`), not the rate —
  so it lands at about −0.5 whatever the knob says. Invisible only because
  the loop in §3.3 overwrites it.
* The synced index `(uint)(value * 15.0f)` reaches 15 against a 15-entry
  table when the knob is at exactly 1.0, reading `0x0815F35C` (7.9e34) and
  stopping the LFO. That is finding G2.

### 3.5 External MIDI clock and sync-LFO phase

The timing-clock byte is parsed as a one-byte realtime message and dispatched
through `0x0811B2CC` case 8. If its source matches `transport+0x74`, it calls
`0x08118B00`. That handler measures the interval with TIM5, keeps a 16-pulse
moving average, writes the resulting engine tick rate to `transport+0x00`
and `+0x04`, and calls `0x08116CD4` on every F8.

The walker calls the per-slot handler `0x0811CC70` with the integer conversion
of `transport+0x18`, a double position advanced by exactly 128/3 ticks per
F8. This is a second caller that the original function/xref scan missed.
Consequently the stock modulo in `0x0811CC70` is not dead: it is intended to
prevent phase reset on every MIDI pulse. Its integer cast does drift for the
four fractional divisions, and accumulated double rounding also presents
some integer boundaries one tick low. The patch uses exact rational forms of
all 15 division-table entries and treats that one-tick truncation correctly.
Rate remains free-running between the phase boundaries, which is necessary
for divisions finer than MIDI's 24 PPQN can directly express.

MIDI Start (`FA`) resets the transport and then enters the same run action;
Continue (`FB`) also re-arms the transport with its local phase position at
zero in this firmware, while Stop (`FC`) leaves through `0x08118624`. Thus
the next accepted F8 after either Start or Continue presents position zero
and deterministically retriggers a synced LFO. Song-position-aware Continue
semantics are not implemented for this LFO phase in the stock transport.

---

## 4. What has been changed

Twenty sites, listed with their original bytes in `tools/patch.py`; the
reasoning is in [patches.md](patches.md). Summary:

| group | sites | effect |
|-------|-------|--------|
| A1/A2/A4/B1/C/E1 | 8 | fault handlers, watchdog, `_sbrk`, heap high-water, I-cache, optional boot CRC |
| G1/G2/G2b | 1 | synced LFO rate follows transport; index clamped; MIDI F8 retrigger rationally gated |
| **H** | 11 | **a fourth X-Mod type: an FM operator ratio** |

Type 3 turns the rate knob into a ratio selector — 64 exact values, the
Yamaha four-operator coarse set — and places the modulator at that ratio to
VCO1's live table coordinate (`project+0x2F20`), the same field the stock
audio-rate path uses. Following that rather than a raw note snapshot is what
keeps the ratio through glide, pitch bend, detune and parameter locks.
The site-by-site breakdown, the ratio set, and the one unresolved assumption
are in [patches.md](patches.md#the-fourth-x-mod-type--an-fm-operator-ratio).

Verified with 78 emulation checks (156 in total), including the ones that
matter: every table entry equals `12*log2(R)/127` for its documented ratio,
unison's offset is exactly zero, ratio 2 is an octave over ratio 1 at five
different notes, and one octave played moves the modulator one octave.

**Not verified:** what the LEDs show in the fourth position. §1.2 rules out an
out-of-bounds read, but the indication may repeat one of the existing three.
Needs someone with the hardware.

## 5. Open threads

Roughly in order of how much they are worth.

1. **What is the CV slope?** `project+0x2F20` is VCO1's pitch CV in 0..126
   units, converted through a calibration table into a DAC code. How many CV
   units the calibration puts on a semitone is not established, and that is
   what decides whether the *stock* audio-rate modulator tracks the keyboard
   exactly, approximately, or not at all. The instrument says not at all;
   the chain that feeds the field says the note is in it. Reading the
   calibration table at `0x0811351C`'s literal pool would settle it.

   Type 3 rests on it: it adds the ratio to `project+0x2F20 / 126` and
   assumes that coordinate maps to the same pitch the digital table would
   give. That is the assumption the stock audio-rate path already makes, so
   type 3 is no worse off than stock — but if ratios drift across the
   keyboard on hardware, this is the place to look.

2. **Fixed ISR pools are now implemented.** All the
   hot allocations are ≤32 bytes (12 B ×158, 16 B ×89, 8 B ×82, 20 B ×36,
   24 B ×23). `operator new` is `0x081591D4` (459 callers), `operator delete`
   `0x08159070` / `0x08159074` (256 / 228).  A second direct branch to
   `free`, `0x08159C98`, was checked: it is exclusively the deallocation half
   of `std::pmr::new_delete_resource`; the paired allocation half at
   `0x081599E4` also bypasses `operator new`, so a pool pointer cannot reach
   it.  Both delete entries used by ordinary C++ objects are patched.
   `np_sbrk` carves the arena off the top of the heap; the high-water counter
   is already recorded.
   The patch carves 16 KiB from the heap top, routes exception-context
   allocations up to 512 bytes through bounded pools, and makes both live
   delete entries pool-aware.  Pool high-water/failure counters are exposed.
   Static vector reachability finds only DMA1_Stream0 and SPI3 calling
   `operator new`: 8/12/20-byte event objects and 512-byte deque blocks.
   *Note:* raising `BASEPRI` instead of `cpsid i` in `__malloc_lock` is a
   dead end — NVIC preemption priorities in use are 1 (EXTI4), **2 (SysTick,
   SPI3, DMA1_Stream0)**, 3 (TIM3), 5, 16, so both allocating ISRs sit at the
   same priority as SysTick and cannot be separated from it.
3. **USB/MIDI were moved out of SysTick.** USB is a coalesced main-loop
   service; MIDI TX drains from USART TXE.  The next open item is the MIDI
   producer's full-ring race.
4. **The X-Mod type indication.** §4 above.
5. **`0x08117DFC`** indexes the same per-parameter arrays as the evaluator
   (`+0xBEC`, `+0xD42`, `+0xDEC`, `+0xE98`, `+0xF42`, `+0xFEE`, `+0x1144`)
   and was never identified. Likely the preset/parameter-lock path — worth a
   look if anyone wants to understand how a patch is stored.
6. **Bootloader-side findings E2/E3/E4** in [audit.md](audit.md), untouched
   because a mistake there is not recoverable over MIDI.
7. **An 11-entry table at `0x08170C64`** contains all six pitch-offset values
   from the manual, but its readers are in the sequencer step processor. Not
   claimed to be the pitch-offset table; unresolved.

---

## 6. Addresses, in one place

### Functions

| address | what |
|---------|------|
| `0x08112488` | per-block DSP update; the audio-rate loop is at `0x08112C70` |
| `0x081156C4` | modulation evaluator, `(project, slot, local)` |
| `0x0811612C` | apply a note to the synth |
| `0x0811621C` | project singleton → `0x200047F0` |
| `0x08116C54` | register a CParam with the project |
| `0x08116C64` | read a parameter by global index |
| `0x08116CF4` | note → `0x0811612C`, with the retrigger test |
| `0x081170E8` | note on |
| `0x08117594` | note off / voice steal |
| `0x08117A44` | note event dispatch |
| `0x08118380` | transport singleton → `0x2000AF78`; tick rate at +0x04 |
| `0x08118B00` | accepted MIDI F8 edge; timing estimate and per-slot phase call |
| `0x0811B2CC` | MIDI realtime transport dispatch; case 8 is timing clock |
| `0x08116CD4` | walk all 19 modulation slots with a transport position |
| `0x08119678` | `_sbrk` |
| `0x0811BA3D` | `MultiWaveOscFast` default retrigger (vtable slot 5) |
| `0x0811BB50` | `XEnvDestCParam::apply` |
| `0x0811BB80` | `LfoWaveformCParam::apply` |
| `0x0811BBA8` | `LFOSpeedCParam::apply` — X-Mod RATE |
| `0x0811BD48` | `LFOTimeCParam::apply` — X-Mod TYPE |
| `0x0811CBBC` | `CParam::setValue(param, rampSteps, value)` |
| `0x0811CC70` | per-slot sync tick; reached at pattern wrap and on every MIDI F8 |
| `0x0811CD08` | modulation slot constructor |
| `0x08156904` | `MultiWaveOscFast::setWaveform` |
| `0x08156910` | `setRate(lfo, hz)` |
| `0x081562C8` | `MultiWaveOscFast::setMode` — only writer of `owner+0x10C` |
| `0x08159070` / `0x08159074` | `operator delete` |
| `0x081591D4` | `operator new` |
| `0x0815A0C0` | `malloc` |

### Tables

| address | what |
|---------|------|
| `0x0815E8C8` | discrete-parameter wrap counts, `ushort[9]` by local index |
| `0x0815E8E0` | destination descriptors, 16 bytes × 19 |
| `0x0815F320` | sync divisions, `float[15]` |
| `0x08172F94` | free-running LFO rates, `float[257]` |
| `0x0817379C` | frequencies, `float[257]`, MIDI 0..127 over 256 steps |

### Project fields (`0x200047F0` — a constant, see §2.5)

| offset | what |
|--------|------|
| 0x0044 | the engine sub-object: what `sub_08112488` and `sub_081473F4` are called with, and the base every offset in them is relative to (§2.5) |
| 0x008C | slot 0 (VCO1Tune); slots 1..18 follow on a stride of 0x298 |
| 0x2EAC .. 0x2EF4 | 19 modulation slot owner pointers |
| 0x2F20 | VCO1 pitch, 0..126 units — the same field the DSP loop calls `engine+0x2EDC`; becomes a DAC code |
| 0x2F24 | VCO2 pitch, same units |
| 0x5FCC / 0x5FD0 | played note (VCO1 / VCO2), float |
| 0x5FD4 | velocity |
| 0x6004 | transpose |
| 0x6028 | held-note list head |
| 0x6074 | 171 CParam pointers, `slot * 9 + local` |

### Patch blob (`0x08175200`)

`NPAT` header; entry addresses at +0x10 (early, late, wdg_hook, four fault
handlers, sbrk, diag, diag_size) and +0x44 (mod_tick, lfo_poll, xmod_time,
xmod_speed, xmod_pos). Diagnostic block at `0x2000E400`, 64 bytes, survives a
warm reset — magic `NORF`, fault record, boot count, heap high-water at +0x34,
last transport rate at +0x38.
