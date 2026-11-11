# Patch set — what was actually fixed

`tools/patch.py build` produces `build/mono_V2.2.0r-patched.syx`, a drop-in
replacement for the stock V2.2.0r update. It carries the stock application
with a verified set of rewritten vector/code/data sites and an appended NPAT
blob.  Exact byte counts and hashes are printed by the current build; do not
copy stale counts from this document.

```bash
make patch           # build/firmware-patched.bin + build/mono_V2.2.0r-patched.syx
make patch-dsp-ab    # also build the -Os image for a hardware timing A/B
make test-patch-static # inspect image, branch targets and SysEx without Unicorn
make test-patch      # run every injected entry point under emulation (when Unicorn works)
```

**Flashed and running on hardware (2026-09-01).** The rate-following part of
the synced LFO fix is confirmed on the unit: a synced X-Mod stays locked
after an internal tempo change. The external MIDI F8 phase-gate correction
added on 2026-09-02 is emulation-tested but not yet heard on hardware. G3 was
confirmed by ear on the unit before being fixed. The completed
artifact is checked by `make test-patch-static`: all patch branch targets,
every type-cycle arm, the boot vectors, the allowed stock-image diff, and the
SysEx round-trip. The packaged macOS ARM Unicorn still raises `SIGILL` during
`uc_mem_map`, but the cause is isolated to its host cache-line probe
(`MRS CTR_EL0` after a sandbox-denied `sysctl`). A locally rebuilt Unicorn
2.0.1 without that Apple fallback ran all 324 dynamic checks successfully.
The fault handlers and watchdog have not been deliberately provoked on
hardware yet. The secondary bootloader is untouched, so a bad image is
recoverable by flashing the stock file again.

## Entering the bootloader

**Hold [TEMPO] + [FUNC] and power the unit on.** Confirmed on hardware.

The manual documents [PATTERN] + [RUN]; that is wrong for this secondary
bootloader (manual predates it). The bootloader's own SysTick scan at
`0x08026944` gates on two switch bytes -- `+0x19C` and `+0x1AC` of the switch
manager -- and only leaves the flag at `0x200016C1` clear, i.e. stays in
update mode, when **both** read 0xFF. Two switches, and on this unit they are
TEMPO and FUNC.

Then: USB only (DIN MIDI will not work), sysex tool with **delay between
messages set to 1 ms**, send the .syx. Progress shows on the [SEQ] LEDs.

---

## What each fix does

| ID | Finding | Fix |
|----|---------|-----|
| A1 | fault handlers were infinite loops | record the fault (including FPU frames), then reset |
| A2 | no watchdog | IWDG, armed on the first main-loop iteration |
| A4 | `_sbrk` executed `bkpt` on a full heap | return `-1` so `malloc` fails cleanly |
| B1 | heap margin unknown | record the high-water mark of `brk` |
| B2 | allocating ISRs enter the globally locked heap | fixed 16 KiB ISR pools carved from the heap top; on exhaustion the stock heap path is used, counted (a mod-note copy burst exhausted them) |
| C  | caches never enabled | enable the instruction cache at start-up |
| E1 | no integrity check | optional CRC32 self test at boot (`--crc`) |
| G1 | synced LFO rate never recomputed | rate follows the transport, continuously |
| G2 | division index reads past the table | index clamped to the 15 entries |
| G3 | quintuplet and 1/64 retriggered on the wrong pulses | boundary computed from the MIDI pulse index, realigned on the common grid |
| G4 | synced sample-and-hold never stepped | the retrigger takes a new sample |
| G2b | MIDI F8 retrigger gate was removed; stock fractional gate also drifts | rational phase gate for all 15 divisions |
| H  | audio-rate X-Mod has no ratio and ignores the note | a fourth X-Mod type: an FM operator ratio |
| H2 | the modulator's sine is a parabola, 3.8 % THD | folded degree-7 polynomial, 0.00005 % |
| H3 | saw, square and triangle are not band limited | polyBLEP / polyBLAMP: 12, 17 and 2 dB less alias |
| J1 | MIDI DIN out is throttled by the 1 kHz SysTick arm | USART TXE interrupt drains at wire cadence |
| J2 | garbled DIN bytes are parsed as valid | dropped and counted; overruns counted separately |
| K  | the CV engine is preemptible by MIDI and USB | default EXTI2 priority 5 → 1; USART → 2; QSPI 16 → 15 |
| P  | no runtime timing attribution | DWT average/max/deadline counters for four IRQs and main |
| L  | the LED writer closes a SPI4 frame only on its *next* call and never waits for one, so with K two delayed SysTicks can raise CS inside a frame, inside the HT16D35A's 2 µs `tCSH`, or set `CSTART` within one SCK of the previous `EOT` (ST ES0392 §2.22.2 master stall) | hold the frame until 16 µs have passed since the last `CSTART`, and stretch the CS high pulse to `tCSW`; collisions counted at `DIAG+0xD44` |
| M  | the LED brightness setter marks an LED dirty *before* storing the value, so a SysTick between the two stores sends the old brightness and clears dirty — the new value then sits with nothing to send it | store the value first, then the flag, as the overlay setter at `0x0810A182` already does |
| N  | **the panel's event deque has no critical section.** The key/pot scanner pushes from DMA1_Stream0 while the main loop drains; a push that recenters the map between the load at `0x08157558` and the store at `0x0815755C` makes the consumer write back a stale map node, and its cursor leaves the live map. The UI then consumes nothing more: events leak and the panel freezes with the main loop, SysTick, audio and the LED writer all still running | replay the whole start-iterator transition with interrupts masked |
| O  | **the pot scan is re-armed blind.** Every SysTick sets `DMA1 S0 EN` and `ADC1 ADSTART` without checking that the previous row's transfer was consumed, and nothing in the firmware ever clears ADC1's sticky `OVR` (its NVIC line is off, its vector is Default_Handler). With the CV engine above SysTick the unit latches `OVR` and every pot reading freezes while the scan ISR keeps ticking — the parameter indicators stop following the knobs | re-arm only when `TCIF0` is clear and `EN` is low, clear `OVR` before `ADSTART`; skips and clears counted at `DIAG+0xD90/0xD94` |
| X  | X-envelopes and the main ADSR are linear ramps | one RC model for all fifteen: convex attack ending at 75 % of its asymptote, exponential decay/release (k = 3.6 ADSR, 4.32 X-Env), linear sustain — clocked by the stock stage machine, timing untouched |

Still not addressed: the main-loop MIDI producer's full-ring race and
bootloader E2/E3/E4.  Thread-mode malloc remains, but its PRIMASK duration,
failures and largest free payload are now measured.  USB is deferred out of
SysTick and MIDI transmission is interrupt-driven.

---

## Rewritten sites

```
0x0810000C  vector HardFault    -> np_hardfault
0x08100010  vector MemManage    -> np_memmanage
0x08100014  vector BusFault     -> np_busfault
0x08100018  vector UsageFault   -> np_usagefault
0x0810003C  vector SysTick      -> np_systick_irq (DWT wrapper)
0x08100060  vector EXTI2/audio  -> np_audio_irq (DWT wrapper)
0x0810006C  vector DMA1_Stream0 -> np_dma_irq (DWT wrapper)
0x08100108  vector IRQ50 = TIM5 transport tick (misnamed SPI3) -> np_spi3_irq (DWT wrapper)
0x0810903C  QSPI priority       16 -> 15
0x081093AA  USART1 priority     1 -> 2
0x081196A0  __malloc_lock       -> np_malloc_lock
0x081196B8  __malloc_unlock     -> np_malloc_unlock
0x08119720  SysTick USB call    -> np_usb_defer
0x0815906C  delete helper       -> np_delete
0x08159070  operator delete     -> np_delete
0x081591D4  operator new        -> np_new
0x0815EA38  heap size           0x40000 -> 0x3C000
0x08119678  _sbrk               -> b.w np_sbrk
0x08156022  Reset_Handler       -> bl np_early   (was bl SystemInit)
0x0815602A  Reset_Handler       -> bl np_late    (was bl main)
0x08119A02  main loop           -> bl np_wdg_hook (was bl System::instance)
0x0811CC70  mod slot tick       -> b.w np_mod_tick
0x0815E8D6  X-Mod type count    3 -> 4               (data only)
0x08150102  TYPE cycle, direct  3 -> 4               (data only)
0x08150556  TYPE cycle, alt     3 -> 4               (data only)
0x081506EC  TYPE cycle, batch   3 -> 4               (data only)
0x08150102  TYPE UI direct      modulo 3 -> 4
0x08150556  TYPE UI alternate   modulo 3 -> 4
0x081506EC  TYPE UI batch       modulo 3 -> 4
0x0811BD48  LFOTimeCParam::vfn2  -> b.w np_xmod_time
0x0811BBA8  LFOSpeedCParam::vfn2 -> b.w np_xmod_speed
0x08112C88  DSP slot loop        -> b.w np_xmod_pos
0x08112C8C  tail of the displaced vsub -> nop
0x08156656  MultiWaveOscFast sine     -> b.w np_sine
0x0815669A  MultiWaveOscFast triangle -> b.w np_tri
0x081566D2  MultiWaveOscFast square   -> b.w np_square
0x0815670E  MultiWaveOscFast sawtooth -> b.w np_saw
0x0811AC90  MIDI DIN transmit         -> b.w np_midi_tx
0x0810EEA0  MIDI DIN receive          -> b.w np_midi_rx
0x0810F400  EXTI2 priority 5 -> 1     (default; --no-audio-priority opts out)
0x0811C69E  QuantizedLinAD X-Env sample -> b.w np_xenv_q (RC model)
0x0811129A  LinAD X-Env sample          -> b.w np_xenv_l
0x08110ABA  ExpAD X-Env sample          -> b.w np_xenv_e
0x0811271E  DSP inline X-Env, OSC1      -> b.w np_xenv_osc1
0x08112952  DSP inline X-Env, OSC2      -> b.w np_xenv_osc2
0x08112F34  DSP inline X-Env, cutoff    -> b.w np_xenv_filter
0x0811255A  DSP OSC1 previous X-Env     -> b.w np_xenv_osc1_previous (last tick's level)
0x08112DDA  DSP main ADSR publish (s17 -> engine+0x2F2C) -> b.w np_env_main (RC model)
0x0810A002  LED writer dirty path       -> b.w np_led_frame (frame spacing + CS)
0x0810A11E  LED writer epilogue         -> b.w np_led_done (frame timestamp)
0x0810A166  LED brightness setter       -> value stored before the dirty flag
0x08157558  UI deque block retire       -> b.w np_ui_queue_pop (masked run)
0x0811972A  SysTick pot-scan re-arm     -> bl np_scan_rearm; b.w 0x0811973E
```

## MIDI

**Correction to the first audit:** the peripheral tick is 4 kHz, but the
branch containing MIDI and USB runs only when the byte at `0x2000B865` wraps
through its modulo-four state.  `sub_0811AC90` therefore writes one byte per
millisecond.  A MIDI byte takes 320 µs, so the old claim that the sixth byte
overwrites TDR was false; the real defect is throttling to 1,000 bytes/s on a
wire capable of 3,125 bytes/s.

`sub_0811AC90` now only enables `TXEIE`.  The USART1 vector is a combined
RX/error/TXE handler which sends one byte whenever hardware asserts TXE and
disables TXEIE on an empty ring.  The remaining producer defect is unchanged:
`sub_0811A904` advances the read index when full and can race the interrupt
consumer.  Queue high-water and full-ring observations are recorded so that
this can be measured on hardware.

The recovered call graph from every hardware vector reaches `operator new`
only from DMA1_Stream0 and SPI3.  Their direct event allocations are 8, 12
and 20 bytes; the DMA queue's `std::deque` can additionally request 512-byte
blocks.  The fixed allocator provides 256 × 32-byte slots and 15 × 512-byte
slots.  Exhaustion faults and resets instead of corrupting the ordinary heap;
the persistent fault code is 5, so that cause survives the warm reset even
though per-boot pool counters are cleared.

**DIN in was trusting broken bytes.** The receive ISR reads `RDR` and clears
every flag in `ICR` without ever reading `ISR`. Clearing blindly is what
keeps an overrun from wedging the port, so the port does recover — but a
byte that arrived with a framing or noise error reaches the MIDI parser as
though it were sound, which on a long cable is a note nobody played. Those
are now dropped and counted; an overrun still delivers its byte (that byte
is valid, an older one was already lost) and is counted separately, at
`DIAG+0x3C`, low half framing/noise, high half overruns.

**USB polling is deferred.** The 1 kHz SysTick arm records the USB driver and
sets a pending bit.  The existing main-loop hook calls the untouched stock
USB service.  Repeated ticks coalesce because the routine is time-based;
defer and coalescing counts are in the diagnostic block.

## The synced LFO: division boundaries and sample-and-hold

`sub_0811CC70` is reached on **every MIDI clock**, not once per pattern
wrap. The F8 handler at `0x08118B00` keeps a double accumulator at
transport+0x18, passes its floor down as `pos`, and then adds the literal at
`0x08118D68` — exactly **128/3**. So one quarter note is 24 × 128/3 = **1024
engine ticks**, and each division's period in MIDI pulses is
`division / (128/3)`:

| idx | note | division | pulses | idx | note | division | pulses |
|-----|------|----------|--------|-----|------|----------|--------|
| 0 | 2/1 | 8192 | 192 | 8 | 1/8 | 512 | 12 |
| 1 | 2/1 triplet | 6144 | 144 | 9 | 1/8 triplet | 341.33 | 8 |
| 2 | 1/1 | 4096 | 96 | **10** | **1/16** | **256** | **6** |
| 3 | 1/1 dotted | 3072 | 72 | 11 | 1/16 quintuplet | 204.8 | **24/5** |
| 4 | 1/2 | 2048 | 48 | 12 | 1/32 | 128 | 3 |
| 5 | 1/2 triplet | 1536 | 36 | 13 | 1/32 triplet | 85.33 | 2 |
| 6 | 1/4 | 1024 | 24 | 14 | 1/64 | 64 | **3/2** |
| 7 | 1/4 triplet | 682.67 | 16 | | | | |

**1/16 is index 10**, two thirds of the way up the knob — not the middle.
The knob is `floor(value × 15)`, so each division owns 6.7 % of the travel.

**The boundary is computed from the pulse index, on the common grid.** `pos`
is within one of `n × 128/3`, so the pulse index comes back exactly — but as
`(pos/128)*3 + ((pos%128)*3 + 64)/128`, not as `(pos*3 + 64)/128`, which
overflows a uint32 after about eight days of continuous external clock.

The two divisions whose period is not a whole number of pulses — the
quintuplet at 24/5 and 1/64 at 3/2 — retrigger where the two grids coincide,
every 24 and every 3 pulses. Snapping each nominal crossing to the next F8
instead would give 5/5/5/5/4 phase jitter; letting the oscillator finish its
intermediate cycles at the exact rate and realigning only on the common grid
is both drift-free and jitter-free. All fifteen are checked in
`tests/test_patch.py`.

**A synced sample-and-hold sat silent on one value.** S&H picks a new value
only where its phase wraps past +0.5. Resetting the phase to −0.5 on the
musical boundary takes that wrap away: when the rate is one cycle per
division — which is the point of syncing — the reset and the wrap fall on
the same moment, the reset wins, and the held value never changes again. The
retrigger now steps it, using the same generator and scaling as the stock
wrap handler at `0x08156778`. That is also what it should do musically: the
new value lands on the division rather than at whatever phase the oscillator
had drifted to.

**The encoder ramp stays at one step per call.** Deferring it to a musical
boundary was tried and reverted: it makes the knob's response depend on the
selected division, and at 2/1 the next step waits 192 clocks -- eight quarter
notes. One step per F8 is 24 a quarter, which is what CParam's ramp semantics
expect.

## Interrupt priorities

Recovered by reading each `__NVIC_SetPriority` call site directly; an
earlier automated pass got some of these wrong by picking up the register
setup of the *following* call.

| preemption | interrupt |
|------------|-----------|
| 1 | EXTI4, IRQ 101, **EXTI2 audio (patched)** |
| 2 | SysTick, TIM5 (transport tick; IRQ 50, previously labelled SPI3), DMA1_Stream0, **USART1 (patched)** |
| 3 | TIM3 |
| 15 | **QUADSPI (patched from invalid 16)** |

The default raises EXTI2 from 5 to 1 because TXE now generates wire-rate
USART interrupts.  USART is explicitly set to 2.  In the 1 kHz SysTick arm,
the old MIDI byte write is now only a bounded TXEIE kick and the USB call is
only a defer flag. `--no-audio-priority` exists for a controlled A/B, not as
the recommended image.

## The modulator's sine

Waveform 0 of `MultiWaveOscFast` — the shape an audio-rate X-Mod modulates
with — is

```
x = (phase - 0.25) * 4          x in [-2, 2), one cycle
y = x * (2 - |x|)
```

a parabola. Five instructions, and **3.8 % THD with the third harmonic at
-28.6 dB**. That is the right call on a part with no FPU. This one is a
Cortex-M7 with an FPv5-D16 single-precision FPU and single-cycle FMA
throughput, so it is simply leaving quality on the table — and as an FM
modulator every one of those harmonics opens its own family of sidebands,
which muddies the tone however exact the ratio is.

Replaced with the honest approximation: fold the cycle to a quarter wave
(`sin(pi*x/2)` is symmetric about `x = ±1`) and evaluate an odd degree-7
polynomial, coefficients least-squares fitted on the folded interval.

| | THD | 3rd harmonic |
|---|-----|--------------|
| stock parabola | 3.804 % | −28.6 dB |
| degree-7 Taylor, for comparison | 0.0041 % | −91.4 dB |
| **shipped** | **0.000044 %** | **−167.1 dB** |

Those are not predictions: `tests/test_patch.py` runs the patched renderer
under emulation for a full period, 2048 samples, and takes the DFT of what
comes out. The least-squares coefficients beat the Taylor series by 76 dB on
the third harmonic because they spread the error across the interval instead
of piling it at the peak.

Cost: about ten instructions more than the parabola. Nineteen oscillators at
the 32 kHz sample rate comes to roughly 2 % of one core.

The other four waveforms are untouched, and they are the developer's own
`TriangleOscNaive`, `SquareOscNaive`, `SawtoothOscNaive` and `SAndHOsc` —
*naive* meaning not band-limited. At audio rate they alias badly, and no
ratio fix helps that. PolyBLEP on the saw and the square would; it has not
been done.

## The fourth X-Mod type — an FM operator ratio

The engine internals this rests on — the parameter system, the modulation
matrix, the nineteen destinations and the audio-rate path — are documented in
[findings.md](findings.md).

Stock X-Mod has three types, selected by the LFOTime parameter: free LFO,
synced LFO, audio rate. Audio rate places the modulator at a pitch that does
not move with the keyboard, so an FM timbre dialled in on one note is a
different timbre on the next.

Type 3 is what an FM synthesiser actually offers. The rate knob stops being a
frequency and becomes a **ratio** selector, and the modulator sits at that
ratio to the played note. Both halves are necessary: tracking the keyboard
with a freely swept modulator still detunes the timbre, because a cent of
error in the ratio beats against the carrier.

### The ratio set

64 detents in **two banks of 48**, split at the middle of the knob.

**Lower half, detents 0–31:** 0.25 to 8.00 in quarters, the Dirtywave M8
grid. Every value is a rational `p/4`, so every one is harmonic — the
sidebands land on multiples of carrier/4, /2 or the carrier, and the tone
keeps a definite pitch. Unison is the fourth click, 2:1 the eighth, 3:1 the
twelfth, 4:1 the sixteenth. This is the half that does basses, keys and
plucks.

**Upper half, detents 32–63:** the irrational columns of the Yamaha
four-operator coarse set, `n * m` for `m` in {√2, π/2, √3}, sorted, 0.707 to
15.59. These are exactly what a quarter grid cannot express: with no
rational relation between carrier and modulator the reflected sidebands
never coincide with the upright ones, so there is no common divisor and no
fundamental. Bells, gongs, struck metal.

The Yamaha set's own harmonic column is not repeated — the lower bank
already covers it at four times the resolution — and everything above 15.59
is dropped, because position is `note/127 + offset` clamped at 1.0, so a
ratio of 16 already runs out of table at G5.

**The table is deliberately not monotonic.** Detent 31 is 8.00 and detent 32
is 0.707: turning past the middle drops the pitch and switches character.
That is the seam, and `tests/test_patch.py` pins it so nobody later
"fixes" it.

The tables live in
`patch/np_lfo.cpp` as the ratios themselves and as their table positions,
`12 * log2(R) / 127`; `tests/test_patch.py` checks every entry against its
ratio, that the set is exactly the 64 documented products, and that unison's
offset is exactly zero.

One semitone is `1/127` of the table position — the frequency table at
`0x0817379C` spans MIDI 0 to 127 over its 256 steps — so a position of
`note/127` is that note, and the ratio offset is added on top. **No reference
note is subtracted.** An anchor is a constant offset in position, so it would
make ratio 1.0 mean "unison transposed by the anchor" and put every other
entry out by the same factor.

### Where each piece goes

1. `0x0815E8D6` — the discrete-parameter wrap count for local index 7,
   **3 → 4**. Entry 6 is 5 (the five LFO waveforms) and entry 8 is 2 (the two
   X-Env destinations), which is what identifies the table. 4 also divides
   256, so the cycle no longer stutters where the underlying byte wraps.
2. `0x08150102`, `0x08150556`, `0x081506EC` — **the three UI copies of the
   same count**. The wrap table is not the only bound: each of the three TYPE
   actions carries its own literal, in the conditional arm that selects local
   parameter 7 over local 6 (`itete ne / movne rN,#5 / moveq rN,#3`). Leaving
   any of them at 3 makes type 3 unreachable from that control. The build
   verifies the whole conditional pair at each site, so a poke that landed on
   the waveform arm would fail rather than silently cut the waveform list.
3. **Nothing at all in `owner+0x10C`.** Three sites here used to be widened
   so that a mode byte of 3 would be accepted — `setMode`'s clamp at
   `0x081562C8` and the evaluator's two "is this slot audio rate?" tests at
   `0x0811582E` and `0x08115BBC`. All three are now left exactly as the
   vendor shipped them, and the tests assert that they match stock.

   The reason is the rule below. `np_xmod_time` calls `setMode` with **2**
   for the ratio type, so every stock reader of `owner+0x10C` sees an
   ordinary audio-rate slot and behaves as it always did. The fourth type is
   carried in `owner+0x25`, whose only reader in the image is `np_xmod_pos`.

   **`np_xmod_speed` must not reconstruct the type from that pair.** Reading
   "mode byte says audio, marker says ratio" looks equivalent to reading the
   type and is not: the marker is only ever written once the type has already
   been recognised, so a single moment where the two disagree collapses the
   slot to plain audio rate with no way back. A slot that falls back that way
   puts its modulator at `knob - 0.5 + VCO1 pitch`, and for a low knob that
   is a slow LFO — so an envelope routed to its depth reads as a pitch
   envelope instead of as FM. The handler therefore takes the type from
   `LFOTimeCParam`'s own value at `owner+0x1A4`, exactly as `np_xmod_time`
   does. `tests/test_patch.py` clobbers the marker and checks the ratio
   frequency survives.

   This was not cosmetic. With a 3 in `owner+0x10C`, routing the X-Envelope
   to the X-Mod amount (`FUNC` + `TYPE` + `WAVE`) stopped working the moment
   the type was switched to the fourth one: the envelope went back to
   driving the destination — oscillator pitch or cutoff — instead of scaling
   the X-Mod amount. Publishing 2 fixed it. The five readers of the mode
   byte do not by themselves explain the path, so the mechanism is not fully
   traced; what is established is that the fourth value was the trigger and
   that removing it restores stock behaviour, confirmed on hardware.
4. `0x0811BD48`, `0x0811BBA8` — both `apply()` handlers replaced with C++
   (`np_xmod_time`, `np_xmod_speed`). A fourth arm does not fit in the stock
   `cmp #1 / cmp #2 / cmp #0 / else` dispatch. The replacements also fix the
   two stock defects described in findings.md §3.4.
5. `0x08112C88` (+ a nop at `0x08112C8C`) — `np_xmod_pos` splices into the
   per-block DSP loop, which is where an audio-rate modulator's pitch is
   actually decided: it recomputes and calls `setRate` for every audio-rate
   slot, every block, overriding whatever the handler wrote. For slots
   flagged `+0x25 == 2` it substitutes the ratio and the note.

Types 0, 1 and 2 are unchanged. Reversible with
`python3 tools/patch.py build --no-xmod`.

### H4: the modulator's waveform, where it is actually rendered

H2 and H3 band-limit `MultiWaveOscFast`'s own renderers. That is not where a
modulation slot's oscillator is rendered. Each of the three per-slot
evaluators inlines all five waveforms **twice**, once per arm of the
X-Envelope branch — six naive copies that share only the phase and step
fields with `MultiWaveOscFast`, so band-limiting its methods never reached
the X-Mod modulator at all.

Each copy is entered through `cmp r3,#4 / bhi.w <bail> / tbb [pc,r3]`. All
six are replaced with a jump to `np_wave_shape`, which reproduces the stock
phase advance, the wrap, the S&H store-then-test order, and the `0.0` that
every one of the six bail targets loads for an out-of-range waveform.

| evaluator | slots | X-Env | dispatch | result | rejoins |
|---|---|---|---|---|---|
| QuantizedLinAD | 0, 4, 9 | on | `0x0811C6E0` | `s14` | `0x0811C714` |
| QuantizedLinAD | 0, 4, 9 | off | `0x0811C892` | `s15` | `0x0811C8C6` |
| LinAD | 1,2,5,6,10,11,12,17 | on | `0x081112F8` | `s13` | `0x0811132C` |
| LinAD | | off | `0x08111508` | `s15` | `0x0811153C` |
| ExpAD | 3, 7, **8 = X-Mod** | on | `0x08110B18` | `s13` | `0x08110B4C` |
| ExpAD | | off | `0x08110D34` | `s15` | `0x08110D68` |

The register budget comes from the stock S&H path, which spills `s0`, calls
the RNG and freely uses `s10`–`s15`: those six plus `r0`–`r3` and `lr` are
free at the dispatch. `s14` is additionally live in the two X-Env-on arms of
LinAD and ExpAD, so those thunks preserve it.

Measured on the patched image, non-harmonic energy relative to the
fundamental, 2048-point frame, modulator at 4390 Hz on the 32 kHz grid:

| waveform | naive | band limited | gain |
|---|---|---|---|
| sine | −41.2 dB | −125.7 dB | **84.5 dB** |
| square | −9.1 dB | −29.8 dB | **20.7 dB** |
| saw | −5.5 dB | −20.6 dB | **15.1 dB** |
| triangle | −26.3 dB | −27.2 dB | 0.9 dB |

The triangle barely moves, as expected: its harmonics already fall off as
1/n², so there was little to fold. The sine's 84 dB is the parabola's own
distortion leaving, not aliasing.

This costs a call per slot per sample where there used to be inline code —
roughly 40–60 cycles on top of about 12, for all 19 slots at 32 kHz. Measure
it rather than assume: `python3 tools/patch.py diag` reports the DWT audio
profile.

**The square keeps the inverted phase** chosen earlier for the synced LFO, so
a retriggered modulator starts high. That is a deliberate difference from
stock in this path too.

### H5: sample and hold reaches white noise

S&H draws a new value on a phase wrap, so its update rate **is** the
modulator frequency — and that is capped twice over: the frequency table
stops at 12543.9 Hz, and `MultiWaveOscFast::setRate` clamps the phase step to
0.5 whatever it is handed. Wide open that is one step every 2.55 samples:
close to noise, but still carrying the comb colouring and the
sample-to-sample correlation of a stepped signal. Drawing on *every* sample
is the end of that road, and it is white noise. No amount of speeding the
audio rate up gets there, because there is nowhere past one draw per sample.

So the last step is taken inside S&H: in the **top ten of the rate knob's
127 positions**, `np_wave_shape` draws every sample instead of every wrap.

**The trigger is the knob, not the phase step, and that distinction is the
whole patch.** An audio-rate slot's table position is `knob - 0.5 + VCO1's
position`, so the frequency a full knob produces moves with the played note:

| VCO1 note | frequency at full knob | phase step |
|---|---|---|
| C2 | 2828 Hz | 0.088 |
| C4 | 9967 Hz | 0.311 |
| C5 and up | clamps at 12543.9 Hz | 0.392 |

The first version of this thresholded the phase step at 0.35 and therefore
**never fired below about C5** — at C4 a full knob only reaches 0.311. Keyed
to the knob it is the same gesture at every note, and it is the last 8 % of
travel rather than the last 2 %.

* Everything below is bit-for-bit the stock behaviour, so no existing patch
  moves.
* Both sides of the switch are already hiss; what changes across it is
  bandwidth, roughly 6 kHz to the full 16. That is the brightness the knob
  is being turned up for, so the discontinuity is the feature.
* **Plain audio rate only** (`owner+0x25 == 1`). In ratio mode that knob
  picks one of 96 exact ratios and the top ten are real intervals worth
  keeping; free and synced LFOs use it for a period.

**Inside the zone the tie to Oscillator 1 is gone**, and not by arranging it
— by consequence. Audio rate normally hangs the modulator off VCO1's live
pitch, but once the draw happens every sample the phase is never consulted,
so the frequency stops reaching the output at all. `tests/test_patch.py`
proves it rather than asserting it: with one seed, the 256-sample output is
**bit-identical** at phase steps of 0.001, 0.088, 0.311 and 0.392 — the steps
a full knob produces from C2 through C5 and above.

One residual, stated because it is real: the *entry* test reads
`owner+0x184`, the LFOSpeed parameter's evaluated value, and locals 0..5 can
carry the evaluator's keyboard-follow term when one is routed to them. That
would shift where in the knob's travel the zone begins; it cannot change what
the noise sounds like once inside. Ten of 127 positions leaves room for a
modest shift.

**This adds no waveform and widens nothing.** An earlier attempt did add a
sixth waveform — `setWaveform`'s clamp, the wrap count and the waveform arm
of all three UI cycle sites. Every stock reader of the waveform byte was
checked first and all of them bail safely, but on hardware the sixth position
appeared in the UI and never reached the oscillator: the value is lost
somewhere in the UI command path (vtable `0x0816FF60`, handlers in
`0x0812xxxx`), which was not traced. All five of those pokes are reverted and
the tests now assert those sites match the vendor image. The lesson from the
X-Mod type byte held: the route that needed no stock field widened is also
the one that worked.

### Rule: never widen a stock field's value range

A byte the stock firmware writes is also a byte the stock firmware *reads*,
often from places that are hard to enumerate. `owner+0x10C` holds 0, 1 or 2
in every shipped build. Storing a 3 there and then widening each reader that
turns up is backwards: it makes correctness depend on having found them all,
and this image defeats that — `build/firmware.asm` dumps whole regions as
data where bogus symbols land on odd addresses, and linear disassembly
desyncs at inline literal pools, so a grep over either corpus can miss a real
reader. (The exhaustive method is to scan raw Thumb-2 encodings; see
findings.md.)

The extension therefore keeps its own byte. `owner+0x25` already had a
one-reader lifetime and the DSP splice is that reader, so it carries the
fourth type and `owner+0x10C` stays inside the range the vendor's code was
written against. Anything added later should follow the same shape: claim a
field, do not stretch one.

### What the ratio is taken against

```
position = ratioOffset[knob] + (note + transpose) / 127
```

The played note, and nothing else — but getting "the played note" needs
care, because the firmware keeps it in a **pair** of per-oscillator slots and
which one an incoming note lands in depends on the step:

| note handler path | `0x5FCC` (VCO1) | `0x5FD0` (VCO2) | |
|---|---|---|---|
| nothing held | the new note | the new note | `0x0811739A` |
| legato | the **previous** note | the new note | `0x0811728A` |
| **slide** | the new note | the **previous** note | `0x0811757C` |
| note off, stack left | what is left | what is left | `0x0811782E` |

The evaluator picks per slot — `cmp r1,#4 / beq` at `0x08115864` sends
VCO2Tune to `0x5FD0` and everything else to `0x5FCC` — so under legato the
two oscillators genuinely hold different notes, and a slid step swaps which
is which.

Reading `0x5FD0` alone, which is what the first version of this patch did,
therefore referenced the operator to the **previous** note on every slid
step. With slides placed by hand between neighbouring notes the error is a
tone or two and reads as a slightly loose ratio; with the slide lane
randomised it is a different interval on every slid step, and the ratio knob
stops corresponding to anything audible.

So: when the two slots agree, that value is the note. When they disagree the
step was legato or slid, and the note that actually just arrived is the one
`np_note_hook` recorded — `bl 0x08116D48` at `0x081172B0`, the call both of
those paths make once the choice is committed, redirected to a thunk that
copies `CNoteData::note` (`r4+8`) into `0x2000E4B4` and tail-calls the
original. `r0`–`r3` are preserved; the handler still has live values in them.

All of it is addressed through the project singleton's fixed address
`0x200047F0` — **not** through the DSP update's argument, which is
`project+0x44`, an engine sub-object (findings.md §2.5).

The obvious alternative is the one the stock audio-rate path uses: VCO1's
live pitch coordinate at `project+0x2F20`. That brings glide, pitch bend,
oscillator detune and sequencer parameter locks with it, and it is the right
answer *while VCO1 is the carrier*. It is the wrong one the moment it is
not — silence VCO1, put the FM on VCO2, and turning VCO1's tune knob drags
the modulator around for no reason the player can see. That is a normal way
to use the instrument, so the default is the self-contained one.

What the default gives up, stated plainly: it does not follow a glide (the
note field holds the endpoints, not the sweep), and it ignores the carrier's
own tuning, so a detuned VCO2 shifts what "ratio 1" means against it.

`--xmod-follow-osc1` builds the other one. The switch (`NP_XMOD_FOLLOW`) is
read by both `np_lfo.cpp` and `np_patch.S`, because the parameter handler and
the DSP splice have to compute the same position; the blob records which is
in force at header +0x64, and the tests read it and expect accordingly.

The splice rejoins the DSP loop at `0x08112C9C` rather than `0x08112C8E`,
which skips the loop's own `rate - 0.5` sweep and, in the default build, the
VCO1 term with it.

A patch saved with type 3 and then loaded on stock firmware selects no valid
type, and that X-Mod will be silent until the type is changed. Reflashing
this image brings it back.

## SR: the automation randomizer follows the pattern's scale (2026-09-09)

The stock parameter randomizer is hold TEMPO+FUNC, sweep a knob between
two limits, release FUNC.  It writes a random automation offset on every
step of the pattern, or of a held step range.  For a pitch that offset is
in arbitrary units, so a random OSC FREQ lane is atonal.  SR quantizes
three lanes to the pattern's scale and root:
- OSC1 FREQ (slot 0, local 0);
- OSC2 FREQ (slot 4, local 0);
- CUTOFF (slot 9, local 0).

Chromatic (scale 0) keeps the stock behaviour.

**Where.**  `bl 0x081576D4` at `0x08143C14` in the RandAutomationData
handler `0x08143B84` → `np_random_splice` (header +0xF4, np_random.cpp).

| Event | vtable | Fields |
|---|---|---|
| RandAutomationData (input) | `0x081700E0` | +6 pattern, +8 step, +10/+11 slot/local, +12/+13 lower/upper |
| ParamAutomationData (output) | `0x081641FC` | 20 bytes: +10 pattern, +13 slot, +14 local, +16 signed offset |

Only the offset's two bytes change.  The stock RNG is called once, as
before.  The range handler `0x08144E10` loops over the same per-step
handler.

**What is quantized** is the resulting nominal pitch, not the offset:

```
center = base + (note + transpose - 57) * follow       (OSC1/2)
center = base + ((note + transpose - 57) * follow) * globalFollow   (cutoff)
offset = round(256 * pitch - center)
```

- `note` is the step's note, read from the sequencer's step lists
  (`0x2000CFF4 + 0x8C + bank*0x63C + step*12`).  On a rest it is the last
  preceding note, wrapping the pattern; in a pattern with no notes it is
  the live keyboard note.
- `follow` is the coarse FOLLOW weight decoded as the evaluator does
  (`0x08115C52`/`0x08115CBC`).
- Cutoff also multiplies by the global FOLLOW (`engine+0x6044`).  Its
  coordinate is raw/256 at the DSP (×256/149 then ×149).

Among the scale's MIDI pitches whose offset stays inside the stock range,
`max*(lower-128)..max*(upper-128)` (max 127 for OSC, 150 for cutoff), SR
picks the one nearest the stock random result, the lower on a tie.  If
none qualifies, or the custom scale is empty, the step keeps its
automation.  The error is at most 1.5/256 semitone.  The scale tables are
`mgr+0x6C + index*14`: count, then degrees.

## Watchdog

IWDG1, prescaler /128, reload 4000 → **16.0 s**, adjustable:

```bash
python3 tools/patch.py build --wdg-timeout 30
python3 tools/patch.py build --no-watchdog
```

It is deliberately **not** started at reset. `np_wdg_hook` arms it on its
first call, which is the first iteration of the main loop, so the whole of
main's start-up — QSPI chip detection, USB enumeration, the 50 ms and 100 ms
delays — runs unwatched.

16 s is generous on purpose. Without hardware there is no way to measure the
worst-case latency between two visits to `0x08119A02`; a project save that
erases QSPI blocks synchronously is the case to worry about. The driver has
`_Async` variants for erase and page write, which suggests long operations do
return to the loop, but that is inference, not measurement. If the device
ever resets by itself during a save, raise the timeout.

Kicking from the main loop rather than from SysTick is the point: SysTick
keeps running through an application deadlock, so a watchdog fed from there
would never fire on the failures that matter.

---

## Diagnostic block

3456 bytes at `0x2000E400` in DTCM — past the application's `.bss` end
(`0x2000E2DC`, whose last word `0x2000E2D8` is the sbrk errno slot) and far
below the stack. SRAM keeps its contents across a warm reset, so the record
survives the reset the fault handler performs.  `+0x40..+0x41F` is cleared
on every boot; `+0x420` onwards only on a cold boot (missing magic).

| offset | field |
|--------|-------|
| `+0x00` | magic `'NORF'` |
| `+0x04` | fault: 1 HardFault, 2 MemManage, 3 BusFault, 4 UsageFault (5 "pool exhausted" was a fatal path in builds before 2026-09-14; exhaustion now falls back to the heap and only counts) |
| `+0x08` | stack pointer at the fault |
| `+0x0C` | stacked PC |
| `+0x10` | stacked LR |
| `+0x14` | stacked xPSR |
| `+0x18` | CFSR |
| `+0x1C` | HFSR |
| `+0x20` | faults since power on |
| `+0x24` | CRC32 expected |
| `+0x28` | CRC32 measured |
| `+0x2C` | boots since power on |
| `+0x30` | watchdog armed this boot |
| `+0x34` | highest `brk` ever reached |
| `+0x38` | last transport rate the LFO fix applied |
| `+0x3C` | MIDI RX error/overrun counters |
| `+0x40..+0xAC` | DWT configuration, allocator/pool/heap, MIDI queue and USB counters |
| `+0xAC..+0xB4` | event-pool ready flag and thread fallback count |
| `+0xB8..+0x12F` | five DWT records: count, 64-bit total, max, overruns, last |
| `+0x140..+0x163` | envelope constants (ln 4, 4/3, 1e-6, 1/32, 1/6, 1/24, 1/120, k = 3.6, k = 4.32), copied from flash by `np_early` each boot |
| `+0x180..+0x41F` | RC envelope state, 21 × 32 bytes: level, last stage / idle origin, attack start level, ramp start, 1/span, cached z and 1−e^−z; entry 0 the main ADSR, 1..19 the mod slots, 20 scratch |
| `+0x130..+0x13F` | MIDI telemetry sender state (phase, position, next heartbeat, sequence) |
| `+0x164..+0x16B` | the telemetry word currently being sent, latched whole, and the PC ring index frozen for this record |
| `+0x420` | `RCC_RSR` at this boot (reset cause; `np_early` then clears it with RMVF) |
| `+0x424` | PC samples taken so far (index into the ring, mod 256) |
| `+0x428..+0x437` | sample histogram: thread mode, audio (EXTI2), unused, other handlers |
| `+0x440..+0x50F` | `+0x60..+0x12F` as the previous boot left them (pools, heap, DWT records) |
| `+0x540..+0x93F` | **PC ring**: SysTick stores one word per tick (4 kHz), `PC[23:0] | IPSR << 24` of whatever it interrupted; a boot writes `0xB007xxxx` (boot count) |
| `+0x940..+0xD3F` | the ring as the previous boot left it, frozen by `np_early` before this boot's SysTick starts overwriting |
| `+0xD40` | the previous boot's sample count |
| `+0xD44` | LED frames the frame guard (**L**) held back |
| `+0xD48` | `DWT_CYCCNT` when the last LED frame was started |
| `+0xD4C` | LED frames started, monotonic |

Read it over SWD and decode it with (full procedure in
[performance-profile.md](performance-profile.md)):

```bash
python3 tools/patch.py diag build/diag.bin
```

## L: LED SPI frame spacing (2026-09-15, corrected 2026-09-16)

The panel LEDs hang off a Holtek HT16D35A on SPI4.  The stock writer
`0x08109FD0` runs from SysTick and sends one three-byte command per tick:

```text
0810A002  CS high     <- terminates the PREVIOUS frame
0810A012  CS low      <- starts this one
0810A0EC  clear EOT/TXTF, 80 <led> <brightness> into TXDR, CR1.CSTART
0810A120  return, with CS still LOW and the frame still shifting out
```

A frame is therefore only ever closed by the *next* call, and nothing waits
for one.  The configuration (register-derived, not scoped): `CFG1 0x30000007`
— 8-bit frames, `MBR` 3 = kernel/16; `CFG2 0x04420000` — master, simplex
transmitter, software NSS, `MIDI` and `MSSI` both 0, so no inter-data or
SS idleness at all; `CR2 3`; SPI4 fed from `pll2_q`.  With HSE 25 MHz,
`DIVM2` 5, `DIVN2` 64, `DIVQ2` 9 (the PLL fields hold divisor − 1) that is
35.56 MHz → **SCK 2.22 MHz, 450 ns per bit, 10.8 µs per 24-bit frame**.  The
datasheet also wants `tCSH` ≥ **2 µs** from the last SCK rise to CS going
high, and `tCSW` ≥ 100 ns of CS high; the stock back-to-back `BSRR` writes
give roughly 25 ns of that.  At the stock 250 µs tick spacing there is 237 µs
of margin and none of it matters.

**K** put the CV engine above SysTick.  When an audio block takes a large
fraction of its own 250 µs period — heavy modulation, sample-and-hold,
ratio FM — SysTick is pushed to the end of the gap, and two executions can
land under 16 µs apart.  Three things then go wrong at once, all at the same
instruction:

* CS rises inside the frame still on the wire, so the HT16D35A sees a
  truncated command;
* or it rises inside `tCSH`, so the last byte is not latched;
* or `CSTART` is set within one SCK period of the previous `EOT`, which
  **stalls the SPI master** — [ST ES0392 §2.22.2](https://www.st.com/resource/en/errata_sheet/es0392-stm32h742xig-and-stm32h743xig-device-limitations-stmicroelectronics.pdf),
  all H743 revisions.

Any of them leaves the panel dark while the main loop, keys, pots and audio
keep working, and the driver never re-sends its init sequence, so it stays
dark until power-off.  That is the reported symptom, and an A/B on the unit
confirmed the trigger: the same build with `--no-audio-priority` does not
show it.

`np_led_frame` replaces the first two instructions of the dirty path
(`0x0810A002`: `push {r4, lr}; movs r2, #1`), takes over both CS transitions
and rejoins the stock writer at `0x0810A01C`.  It holds the frame back until
`LED_MIN_GAP` = 6400 cycles (16 µs at 400 MHz — 10.8 µs frame + 2 µs `tCSH`
+ margin) have passed since the last `CSTART`, which `np_led_done` records
from `DWT_CYCCNT` at `DIAG+0xD48`; then it drives CS high, holds it for
`tCSW`, and drives it low.  It *waits* rather than skipping, so no LED update
is lost; the wait is bounded by `LED_SPIN_MAX` ≈ 30 µs so SysTick can never
hang on it, and it is skipped entirely when `np_runtime` could not start the
cycle counter.  Collisions are counted at `DIAG+0xD44` and frames started at
`DIAG+0xD4C`, both in the heartbeat — so the telemetry says whether the
collision happens on the unit, and whether frames keep going out.

Costs nothing in the normal path: one load and a compare per dirty LED.
`tests/test_patch.py::test_led_frame` (20 checks), `tests/test_static_patch.py`.
Under whole-firmware emulation the guard stays at zero collisions with ticks
a period apart and fires on every collision when they are back to back, with
the frames still sent.

A/B for the priority itself: `python3 tools/patch.py build --no-audio-priority`
leaves EXTI2 at the stock priority 5.

## N: the UI event deque is not thread safe (2026-09-16)

This is the one that freezes the panel.  The unit's own telemetry settled it:
across four heartbeats spanning the freeze, the main loop kept iterating at
6,070/s, SysTick sampled at its full 4,000/s, the LED writer started 1,282
frames/s without a break, and SPI4 read back healthy (`SR` = `TXP|EOT|TXTF|TXC`
with `CTSIZE` 0, `CR1` = `SPE=1, CSTART=0`) — while 23 of the 70 LEDs stayed
permanently dirty, the requested-brightness sum moved only 296 → 291, and the
writer's whole traffic was exactly those 23 entries cycling (1282/4000 =
32 % ≈ 23/70).  The renderer was asking for nothing new.  Meanwhile the small
event pool sat pinned at 192 blocks — exactly `kSmallThreadCount`, its thread
limit — with 356 allocation failures and 189 KB of heap still free: events were
being produced and never freed.

The panel object's input queue is a `std::deque` of event pointers, iterators
at `0x2000DE54 +0x1C..+0x40`, 128 pointers per block.  The key and pot
scanners push into it through `0x08157324` from **DMA1_Stream0**; the main loop
drains it through `0x081574F8`, called at `0x08119A60`.  Nothing serialises the
two.  When the consumer retires a block it advances the start iterator's map
node in separate steps:

```asm
08157558  ldr  r3, [r4, #0x30]     ; old start.node
0815755A  adds r2, r3, #4
0815755C  str  r2, [r4, #0x30]     ; publish the next node
0815755E  ldr  r3, [r3, #4]        ; the block, through the OLD node
08157560  add.w r2, r3, #512
08157564  str  r3, [r4, #0x24]
08157566  strd r3, r2, [r4, #0x28]
```

A push that recenters the map (`0x08157422..`) or replaces it
(`0x081573BE..`) rewrites **both** iterators' node pointers, at `0x081573FE`
and `0x0815740A`.  One landing between the load and the store leaves the
consumer holding a stale node, which it writes back as `old + 4`; its cursor
is then outside the live map.  From there the corruption goes one of two ways:
the next dispatch reads a null event and reaches `0x0815A6E0` (`b .`, abort),
which stops the main loop — the outcome
[ui-amount-audit.md](ui-amount-audit.md) reproduces — or `start` overshoots
`finish`, the queue looks empty for ever, and the main loop carries on
perfectly while the UI is deaf.  The second is what the unit shows, and it
accounts for every number above.

`np_ui_queue_pop` replays the whole transition with interrupts masked and
rejoins at `0x0815756A`.  Guarding this side alone is sufficient: the producer
runs in an ISR and can never be preempted by the consumer.  Only four bytes of
stock change — the stub replays the run, so `0x0815755C..0x08157569` is left in
place and simply unreachable.  Cost is about fifteen cycles of `PRIMASK`, once
per retired 128-event block.

`tests/probe_ui_queue_race.py` reproduces the corruption at both hazardous
boundaries on a stock image and then shows every instruction of the guarded
transition running with `PRIMASK` set on a patched one;
`tests/test_patch.py::test_ui_queue_pop` checks the iterator arithmetic and
that a caller's own `PRIMASK` is preserved either way.

## O: the pot scan re-armed blind (2026-09-17)

The p7 and p8 captures located the freeze.  While AMOUNT was swept on
the frozen panel, the DMA1_Stream0 scan handler kept entering at exactly
4 kHz, every pot's 32-sample ring kept advancing (`+1500` writes per 3 s =
one per mux revolution), the three-word ADC buffer held live conversions
(noise of a couple of 12-bit LSBs, values multiples of 16 from `LSHIFT = 4`)
— and **`pot_enqueue_attempts` did not move, no ADC channel moved, and
`ADC1_ISR` read `ADRDY|EOSMP|EOC|EOS|OVR`** in every snapshot.  The last pot
event ever queued was the slot select; AMOUNT's `+0xC` sat at the value of
its last published event and the renderer faithfully drew it.

The scan, from the code.  `main()` calls `HAL_ADC_Start_DMA` once (three
conversions into the buffer at `driver2+0x6C`, `0x30040000`).  From then on
the SysTick tail at `0x0811972A` does, every tick:

```asm
0811972A  DMA1 S0CR |= EN         ; on an H7 re-enabling reloads NDTR = 3
08119736  ADC1 CR   |= ADSTART
```

and the stream's TC handler `0x0810F208` folds the buffer into each pot of
the current mux row (record = `driver2 + pot*104`: ring index `+0xBA`,
average `+0xBC`, current `+0x72`, last published `+0x76`), advances the row
(`driver2+0xA32`) on GPIO C1/C2/A8, and enqueues an event when the average
moves more than 384 counts.  ADC1 runs `DMNGT = 3`, `OVRMOD = 1`, `OVRIE = 1`
— but IRQ 18 is disabled in the NVIC and its vector is Default_Handler, so
an overrun is never seen and **never cleared**.  Nothing checks, before
re-arming, that the previous row's completion was serviced or that the stream
is idle.  At the stock priorities SysTick and the stream handler always run on
time and the sequence is safe by construction; with the CV engine above them
(**K**) both are pushed around by a 200–235 µs audio block, the ordering
assumption breaks, and the overrun latches.

`np_scan_rearm` replaces the two register writes: it re-arms only when
`LISR.TCIF0` is clear (the previous row was consumed) and `S0CR.EN` is low
(the stream is idle), clears `ADC1_ISR.OVR` if it is set, and only then
re-enables the stream and starts the ADC.  On a healthy tick that is byte for
byte the stock sequence plus one flag test.  A tick that finds the previous
row unconsumed is skipped and counted (`DIAG+0xD90`); an overrun cleared is
counted (`DIAG+0xD94`); both ride in the P6 snapshot (schema 4).  Eight stock
bytes change — `bl` to the stub and a `b.w` over the displaced sequence,
which is left in place unreachable.  `tests/test_patch.py::test_scan_rearm`
covers the four cases; the emulator now models DMA1 stream 0's completion
flag honestly (it used to read `TCIF0` as permanently set).

**Confirmed on the unit (2026-09-17, p9 capture).**  Over 144 s of
normal use the guard skipped 3,104 re-arms — the previous row still
unconsumed, about 21 times a second and only under load — never once found
`OVR` set, and the panel did not freeze.  So the blind re-arm on an
unconsumed row is the fault and the overrun was its consequence; with the
collision refused, the latch never forms.  The exact register-level route
from the blind re-arm to `OVR` is inferred from the p8 state rather than
traced.

## M: publish an LED's brightness before its dirty flag (2026-09-16)

The brightness setter `0x0810A14C` stored the dirty flag first:

```asm
0810A166  strb.w r3, [r0, #0x97]  ; dirty = 1
0810A16A  strb.w r2, [r0, #0x94]  ; brightness
```

A SysTick between the two runs the LED writer, which sees dirty, sends the
**old** brightness and clears dirty once its smoothing has converged.  Main
then stores the new brightness with nothing left to send it, and repeating
the setter with the same value returns early at `0x0810A15A` without
repairing the flag — that LED stops following until an unrelated change
happens to set dirty again.  The two stores are independent (both registers
are final before the first), and the overlay setter at `0x0810A182` already
uses the safe order, so the fix is to swap them.

`tests/probe_led_transport.py` injects the real SysTick handler at each
instruction boundary: on a stock image the middle boundary loses the update,
on a patched one none of the three do.

### Telemetry over MIDI (2026-09-15)

Nothing on the unit can display the block and SWD is not available, so
`np_main_service` pushes it out as **control changes on MIDI channel 16**
(`0x0811A904` fans the stock 4-byte output ring out to DIN and USB, and the
sender only writes while that ring has 16 free slots, six CCs per main-loop
pass).  CC 119 is a record tag, CC 102..106 carry one 32-bit word MSB first
(4 + 7 + 7 + 7 + 7 bits):

| tag | when | content |
|-----|------|---------|
| 1 | 2 s after a boot whose reset was not a power-on, or after a recorded fault | magic `NPTL`, boots, `RCC_RSR`, fault record, the previous boot's pool/heap/DWT maxima, the sample histogram, the frozen ring's index |
| 2 | right after tag 1 | the frozen PC ring, 256 words, oldest first |
| 3 | every 3 s | uptime, histogram, live DWT maxima, pools, heap, UI mode/page/view/override (view ≠ 1 means the 1 kHz parameter-LED refresh `0x0814F80C` is off), modifier mask, held keys, key shadow, LED writer index + refresh/dirty flags, LED frames delayed/started, `SPI4_SR`, `SPI4_CR1`, the LED witnesses below, the 24 newest samples (53 words) |

**Which half of the LED path died.** Entry *i* of the driver's 70 LEDs is at
`+0x94 + 20i`: requested brightness at `+0`, dirty at `+3`, and the value the
SysTick writer smooths towards it at `+12`.  Only the renderer writes
`requested`; only the writer clears `dirty` and moves `smoothed`.  The
heartbeat carries the sum of `requested`, the count of dirty LEDs and the sum
of `smoothed`, which separates the two failures without a single extra splice:

| requested | dirty | smoothed | meaning |
|-----------|-------|----------|---------|
| moves | stays high | frozen | the SPI transport is dead, the UI is fine |
| frozen | falls to 0 | catches up | the renderer stopped, the transport is fine |
| frozen | frozen | frozen | both stopped |

**The first fragment of every word carries the word's index mod 8 in bits
4..6** — a CC value has seven bits and the word's MSBs need only four, so this
costs nothing.  Without it one dropped CC shifted every field after it onto
the wrong name, silently; the first capture from the unit lost six words and
`tools/diagmidi.py` had been reading PC samples as SPI registers.  The
decoder now marks a lost word `LOST`, keeps the rest on their fields, and
refuses to decorate a record from a firmware without the stamp.  The sender
also leaves three quarters of the stock output ring free (it used to fill it
to within 16 slots), because the sequencer's 24 ppqn clock shares that ring
and the main-loop producer's full-ring race is still unfixed — that is where
those six words went.

Three sender defects were fixed on 2026-09-16, because a diagnostic that lies
is worse than none:

* a word was re-read on every pump call, and a burst is only six CCs, so the
  five fragments of one word routinely straddled two calls and could mix two
  values — with the fragments landing either side of a carry the result
  decoded to a number that never existed (reproducibly `0`).  The word is now
  latched whole when its first fragment goes out (`DIAG+0x164`).
* `live_sample()` re-read the moving ring index per word, so a heartbeat's 24
  samples were not guaranteed to be a consecutive history.  The index is now
  frozen when the record starts (`DIAG+0x168`).

`audio_max_cycles` measures the **whole** stock EXTI2 handler, and that
handler renders eight samples per call, so the deadline behind
`audio_overruns` is the 250 µs buffer interval (100,000 cycles), not the
31.25 µs of one sample.  It used to hold the single-sample figure, which
counted almost every frame as an overrun.  `hist_audio` still cannot be read
as audio load: the sampler runs from SysTick, which **K** puts below the
audio handler, so it can never sample inside it.

Record with a MIDI monitor (macOS: *MIDI Monitor*, source = Mono, keep
channel 16 visible), then `python3 tools/diagmidi.py capture.txt` symbolises
the samples.  After a hang that ends in the 16 s watchdog reset the tag-1/2
records show where the processor was in the last 64 ms; while a hang is only
in the UI (main loop alive) the heartbeats keep coming and show the same.
Emulator check: `tests/test_telemetry.py`.

The stacked frame is only dereferenced when the stack pointer is inside
DTCM. Extended Cortex-M7 FPU frames are advanced by 72 bytes before LR/PC/
xPSR are read. A bad SP is exactly the case where faulting again inside the
handler would put the device back where it started.

`+0x34` is the answer to "how close is the heap to full": subtract
`0x30000000` for bytes used at peak, out of 245760.  The removed top 16384
bytes are the bounded ISR pools, not lost RAM.

**Caveat:** nothing on the device displays any of this. Reading it needs a
debug probe. A future patch could report it over SysEx; the block is laid out
so that is a small addition.

---

## The sequencer runs in main(), and main() was re-sorting the pattern (S1, S2 — 2026-10-02)

**Where a step is processed.** TIM5 (IRQ 50 — mislabelled "SPI3" in the DWT
profiler until p31) is the transport clock: its handler `0x081194A4` only
advances the target tick `transport+0x10` and sets the flag `+0x88C`.  The
ticks themselves are processed by **main()** — it tests the flag and calls
`0x08119270` at `0x08119AC0`, which walks every tick up to the target and
calls the step processor `0x08146694` for each.  MIDI input is parsed in
main() too (`0x0811B700`, called at `0x08119A4A`), and the pattern-store
pass below runs just before the transport check (`0x08146E70` at
`0x08119A78`).  So the main-loop period is not only UI latency: it is
how late a sequenced step or an incoming note can be.

**What main() spent its time on.**  Every iteration calls `0x08146E70`,
which runs `0x0815888C` on both tracks of the pattern store (sequencer
`0x2000CFF4`, tracks at `+0x8C` and `+0x6C8`, 64 steps each: a
`std::list` of events per step at `+i*0xC`, a list of pending edits per
step at `+0x300+i*0xC`).  That pass commits pending edits under a 125 µs
budget per track, and `0x081585C0` ends **every** step with
`std::list::sort` (`0x081584AC`) — also when there was nothing to commit.
An unchanged pattern is therefore re-sorted on every main-loop iteration:
64 bin heads initialised and n log n splices per step.  On sysemu, 16 steps
with a note and six parameter locks each: **27,300 instructions per main
pass, 67 % of them that sort**, 82 % the whole pass.  On the unit the budget
caps it at 2 × 125 µs, which is the ~390 µs main period every capture shows.

The key is the byte at `node+8`: `0x80 + microshift` (the reader
`0x08157D1C` plays a step's events in that order).  The sort is stable —
both merge loops take the left node on equal keys (`cmp; bhs`).

* **S1** — `bl 0x081584AC` at `0x08158708` → `np_list_sort`: one walk; if
  no key is below its predecessor the list is returned untouched, otherwise
  the stock sort runs.  Exact: a stable sort of an ordered list changes no
  link and no size.  360 random lists (ties, nearly sorted, shuffled, sizes
  0..40) come out byte-identical to the stock sort; an ordered 8-event step
  costs 64 instructions instead of 1,155.
* **S2** — the two calls in `0x08146E70` (`bl` at `0x08146E76`, tail `b.w`
  at `0x08146E82`) → `np_seq_worker`: when no clear is running
  (`+0x635`), no rotation (`+0x632 != +0x633`), no step has a pending edit
  and every step is in order, the stock pass would call no callback and
  change no list — only its own cursor `+0x610` and the `+0x638` flag.  The
  cursor is read nowhere but in the pass and in the flush `0x08158A48`,
  which sets it, calls `0x0815888C` **directly** until it reaches a target,
  and is deliberately not routed here (it would never terminate).  `+0x638`
  is written 1 only inside a clear and its readers consult it only during
  one.  Anything else falls through to the stock pass.

Measured (sysemu, `tests/test_seq_sort.py`, 15 checks): main-loop pass
median **19,900 → 10,300 (S1) → 2,800 (S1+S2)** on the test's locked
pattern; with a precise per-instruction profile on the 16-step one,
27,300 → 9,500 → 3,200.  The pattern store after the same UI session is
identical in all three variants (event by event: key, vtable, parameter,
value), and the same pattern played back gives every note-on/off at the
same transport position.  `--no-seq-sort` / `--no-seq-idle` build the A/B.

Not changed, and why: each sequenced note re-evaluates all 171 parameters
(`0x0811612C` → `0x081156C4`, ~207 calls, ~28,600 instructions) — that is
the step's own work, and skipping parameters needs a dirty-tracking model
of locks, mod notes and morph that does not exist yet.

---

## Audit corrections in p31 (2026-10-02)

**A1's frame decode was wrong for every fault taken with FP state.**  An
extended exception frame keeps r0..xPSR at the stacked SP and appends
S0–S15, FPSCR and a reserved word *above* xPSR (ARMv7-M ARM B1.5.7).  The
handler added 0x48 when EXC_RETURN bit 4 was clear and so recorded S15,
FPSCR and the reserved word — with lazy stacking, stale stack — as LR, PC
and xPSR.  The audio handler and most of main() use the FPU, so nearly every
real fault would have been reported with a garbage PC; `test_patch` encoded
the same wrong layout.  Fixed, test rewritten to the architectural layout.
This matters for the open DELETE+RECORD reboot: the stock firmware traps on
purpose in three places, `bkpt` with no debugger being a HardFault —
`Error_Handler` `0x08119B08` (50 HAL call sites: QSPI, internal flash,
init; the stacked LR names the failing call), main() at `0x08119A14` when
the QSPI (project flash) operation queue reports an error, and `_sbrk`
(already replaced by A4).  `tools/diagmidi.py` now names these PCs.

**An instant attack retriggered in decay waited for the block boundary.**
Since p26 the RC envelope takes a stage change on its own sample, but with
an attack shorter than a tick the stock runs trigger, attack and the switch
to decay inside one tick: a note landing in decay leaves the stage at 1 and
only moves the decay accumulator `+0x24` back up.  The hold path did not
look at it, so the RC level kept falling on the old note for up to seven
samples — measured 3..6 late at mid-block hits on the main ADSR and the
inline OSC1/OSC2/cutoff X-Envs (the held slots were fine: their gate flags
already cut the block).  `test_note_every_step` placed its notes on block
boundaries on purpose and could not see it.  The hold path now compares
`+0x24` with `E_XLAST` in decay; `test_note_mid_block` drives a retrigger
at every block offset.  Cost +75..+144 instructions per audio block.

---

## LV: the LEDs levelled (p63 — 2026-11-11; replaces p62's WL)

The stock asks each LED for its own level: FOLLOW 63 when on, TYPE 32/48,
the current pattern 24, a note step 16, the dim states 2..7.  In a dark
room the white ones glare (FOLLOW, SEQ 1–16, TYPE, WAVE).  p62's WL scaled
the white LEDs to a quarter, but the aim is one low level for every LED,
with only the playhead and the tempo blink brighter.

**Where.** M2's `np_led_smooth` sits in the writer `0x08109FD0` right where
the target meets the fade (`0x0810A098`).  It now first passes the target
through `np_led_target` (np_ui.cpp) and writes the result back to `s15`
too, because the writer's dirty test compares `(u32)s15` with what it
sends.  The fade still runs, so changes stay smooth.  The writer's target
is `base + pulse + overlay` (entry `+0x94..+0x96`) plus an animation term:
`+0x98 / +0x9A × 16`, negative over a base of 31.  That animation is the
playhead (period 256) and PAGE A's tempo blink (period 150).

**The levelling**, preset `{dim, normal, bright, high}`:
- off stays off;
- a static request of 1..7 goes to `dim`, 8..23 to `normal`, 24 and up to
  `bright`;
- TYPE (LED 45) and WAVE (46) show the type and the wave by brightness, so
  they keep small steps: TYPE 16/32/48 → 1/2/3, WAVE 4/8/12/16 → 1..4
  (scaled up above `normal` 4);
- a positive animation rises to `high`; a negative one darkens the LED in
  proportion.

**Presets:**
- 1 `{1,1,1,4}`: every lit LED at the HT16D35A's lowest PWM step, 1/63.
  Dim, normal and bright then look the same: in pattern mode the current
  pattern no longer stands out, and in the parameter views neither do
  lock steps.
- 2 `{1,2,3,8}`, 3 `{2,4,6,16}`, 4 `{3,8,12,32}`: brighter, with the
  three classes apart.
- 5: the stock's own levels.

`--led-level` sets the power-on preset (default 1).  TEMPO+FOLLOW steps
1 → 2 → 3 → 4 → 5 → 1 (`D_LED_LEVEL`, `0x13F4`) until the next power-on.
The press runs the stock in mode 0 and consumes the key, so FOLLOW is not
toggled; the stock's TEMPO+FOLLOW was a plain FOLLOW toggle.  All 70 LEDs
are marked dirty so the change shows at once.  TEMPO+FUNC+FOLLOW, the
pitchbend randomizer, is unchanged.  Below preset 1 only the HT16D35A's
global brightness is left (the init sends `37 3F` at `0x08109B1A`); that
is not used.

After a few TEMPO presses the stock starts a tempo blink on PAGE A, the
first step and the selected key (the animation fields; the tempo does not
change).  Those blinks level as `high` like PAGE A's.

`tests/test_led_level.py` (12 checks, read off the SPI frames, fresh boots):
1. Control: the stock sends FOLLOW 62, a note step 15 and PAGE A 15.
2. Preset 1: every lit LED sends 1 (FOLLOW, note step, PAGE A, keyboard
   bright and dim, the selected knob), TYPE 2/3, WAVE 1/2/3/4.
3. Playing: the playhead reaches 3–4, PAGE A blinks above 1, and FOLLOW
   stays at 1.
4. TEMPO+FOLLOW: FOLLOW 3, 6, 12, 63, 1, and a dim key 1, 2, 3, 2, 1, with
   no toggle and no event.  FOLLOW alone toggles.
5. TEMPO+FUNC+FOLLOW leaves the preset alone.

`test_led_fade` now runs at preset 5, so that M2's fade itself is what it
measures.  `test_seq_led`, `test_ui_chord`, `test_scale_library`,
`test_spi_p6`, static and bootloader pass.  Image 523,104 bytes.

## R7: generator ranges +60 % (p61 — 2026-10-15)

`Gen::range` widens every drawn range by three tenths of its width on each
side (was a fifth): 60 % wider than the recipes' own ranges in all (p49
+25 %, p50 +15 %, p61 +20 %), clamped to 0..0xFFFF.  A VCO's tune and detune
still draw `exact()`, so pitches are as before.  Bootloader, static and a
generator smoke (single instruments, a drum kit, both full sets: 96
instruments, no fault) pass; the long sound suites were not run, and their
per-instrument bounds may now be crossed more often.  Image 522,528 bytes.

## PJ, PB: a save cut short no longer bricks the power-on (p60 — 2026-10-13)

**The stock.** A project slot (32 × 512 KB on the QSPI flash, slot n at
`n << 19`) is a 0x100-byte header and an event journal: `[id][payload]` from
SOF (254) to EOF (9).  A save erases the slot and programs it from the
start, so a power loss in the middle leaves either a journal that runs into
erased flash (`0xFF`) or an erased slot.  main loads **slot 0 at every
power-on** (`0x081199AE`: `bl 0x08124440`, the read and the replay
`0x08124054`).

The journal reader `0x08123C18` looks an id up in the factory registry
(`0x08123A88`, `unordered_map::operator[]`).  When it finds no factory it
calls the empty `std::function`, which throws `bad_function_call`
(`0x08159882`), then `abort` (`0x0815A224`), then `b .` at `0x0815A6E0`.
That happens before any of the replay's own checks.  The first event takes
the same path at `0x08124406`.  An id of 0 already ends the journal
cleanly; only unknown ids abort.

Reproduced in sysemu on the stock image: slot 0 cut short stops the
power-on load in the abort loop, and main is never reached.  Our watchdog
did not help, because it is armed on the first main-loop pass.

The replay does have checks: `0xA1` no length (no EOF seen), `0xA2` the
header's pattern table differs from the rebuilt one, `0xA3`.  A failed
check calls the error screen `0x0814FA54`.  The screen shows the code on
SEQ 1–8 and waits for keys 30+31.  With USB connected, those keys send the
project out as SysEx (`0x08123FE8`, `0x0812471C`).  Then the screen halts
(`b .` at `0x0814FAE2`).  It never returns and never repairs the slot, so
even a detected bad slot 0 stops every power-on.

**PJ** (`--no-journal-fix` for the stock):
- **The reader.** At `0x08123CAE`, the empty-function path becomes
  `movs r3,#0; b 0x08123C2E`, the reader's own "no event" exit (the one id 0
  takes).  A journal cut short therefore ends at its first erased byte.  It
  has no EOF, so the replay's length check fails with `0xA1`.
- **The first event.** At `0x08124406` it becomes `movs r1,#0xA4;
  bl np_project_fail; b 0x08124356`.  That fails as `0xA4` and returns from
  the replay before anything is reset.  The stock's own "not a SOF" branch
  was not used: it imports the bytes as an old-format project, and from
  `0xFF` it built 255-step patterns.

**PB** (`--no-boot-fallback` for the stock):
- **The power-on load.** `0x081199AE` calls `np_boot_load`, and the three
  check calls (`0x08124166` A2, `0x08124362` A3, `0x0812436E` A1) call
  `np_project_fail`.
- **During the power-on load,** a failure is noted (`D_PBOOT`, `0x13F0`:
  running, first code, count, fell back) and the replay continues.  The
  image buffer then gets an empty journal (zero header, SOF, EOF), which is
  replayed.  An empty journal leaves every pattern at 0 steps, so all 64
  get 16 with the event the UI sends (`PatternParamData`; its byte 6 names
  a pattern, and it takes effect only on a pattern a layer is playing).
  Layer 0 therefore visits each one through the stock's switch
  `0x08125A90(mgr, pattern, layer)` and ends on pattern 1, and layer 1's
  pattern gets the event again.  With 0-step patterns, LOAD + SEQ
  requested no load in sysemu, and a rotate overran its store (RG below).
- **Slot 0 in flash is left as it is** until the next save.
- **Any later load** keeps the stock's screen.  On the unit, the watchdog
  restarts it from there after 16 s, and the restart loads slot 0.

`tests/test_project_journal.py` (16 checks; every power-on is a fresh boot):
1. Control: the stock with slot 0 cut short ends in the abort loop.
2. Slot 0 cut short: main runs, `0xA1`, an empty project.  Each layer is
   on its own pattern, and patterns 8, 17, 34, 49 and 64 have 16 steps.
   Slot 0 is untouched, and it plays.
3. Slot 0 erased: `0xA4`, the same result.
4. A sound slot 0: no fallback.
5. Recovery: after the fallback, FUNC+SAVE writes slot 0, and the next
   power-on loads it without a fallback.
6. A damaged slot 1 loaded after power-on: the stock screen with `0xA1`,
   no abort.

**RG: a pattern rotate stays inside the step buckets** (`--no-rotate-guard`
for the stock).  A UI fuzz in mod-note mode found this crash.  The steps
were PATTERN + PAGE D + SEQ 1, which selects pattern 49, a pattern of
length 0 in sysemu's project.  Then FUNC+UP faulted in a `std::list` swap
(`0x08159820`).

- **The mechanism.** The RotatePatternData handler (`0x08129410`) passes
  its range to `0x081583AC(store, direction, start, end)` through
  `0x08145EC0`.  The range comes from the pattern's length − 1, which is
  255 for a length of 0.  A track's store holds 128 buckets of 12 bytes.
  The rotate pass (`0x0815888C`) then swapped buckets 255 down to 0
  through the memory past the store until a node was garbage.
- **Not changed:** a stock rotate at lengths 1–64 (checked at 15 lengths).
- **The fix:** the tail call at `0x08145EE0` goes to `np_rotate_guard`.
  The pass walks a byte index from one end of the range to the other: down
  from `end`, or up from `start`.  So a reversed range wraps past 0 or 127
  the same way, and only `start <= end <= 127` goes through.
- **Reach on the unit.** In the stock it needs a pattern of length 0.  An
  empty journal leaves every unused pattern at 0, and so did PB's fallback
  before it set all 64.  A unit's own projects would need a pattern saved
  at length 0; whether one can be is not verified.

`tests/test_rotate_guard.py` (11 checks):
1. Control: the stock faults on that rotate.
2. With RG, FUNC+UP/DOWN on the 0-step pattern faults nothing, and every
   list is intact.
3. A 16-step pattern still rotates: its notes move and none is lost.
4. A reversed range (start 20, end 5) sent as an event: the stock's lists
   break, and RG drops it.

Image 522,528 bytes.

**Also in p60:**
- **PL: a one-step pattern.** `np_proj_service` started the slot's read
  only outside the last step (`left > 256`).  A one-step pattern has only a
  last step, so a load asked while it played waited for a stop.  Pattern
  changes and saves stayed blocked meanwhile.  The read may now start in
  the only step.  `test_sync_load.py` scenario 7.
- **N2: a bounds check.** `np_modnote_replay` skips a record whose slot or
  local index is out of range (19 × 9).  The stock handler has no such
  check either (`0x08117E94`); only a damaged project carries one.

**Found by the audit, not changed:**
- **The stock expects every pattern to have steps.**  With the 0-step
  patterns an empty journal leaves (sysemu's projects), three things broke:
  - LOAD + SEQ asked no load;
  - a rotate overran its store (RG fixes that one);
  - clearing a step's probability (`ClearStepProbabilityData`, handler
    `0x08128868`) called an empty callback in the step-list walker
    `0x08157DC4` (PC 0, found by the UI fuzz in normal mode).

  So PB gives all 64 patterns 16 steps, and the UI fuzz now starts from a
  project like that.  A unit's own projects are expected to have no 0-step
  pattern, since the stock would trip over one.  That is an inference, not
  checked against a unit's project.
- **Event payloads are trusted.**  A journal with valid ids but bad fields
  made `0x081583E0` write through a pointer built from them (sysemu: an
  unmapped write).  On the unit that is a fault, and A1 resets.  Only a
  slot damaged in the middle carries such events, not one cut short.
  Norand's project SysEx carries them too.
- **Norand's project SysEx.**  The receiver (`0x08119D20`, MIDI object
  `+0xD1C` mode, `+0xD22` buffer, `+0xDA4` index) recognises
  `F0 00 21 50 00 10 00 01`.  At F7 it unpacks the message and parses it as
  journal events with an XOR check (`0x081266FC`).  An unknown id there
  also aborts; the watchdog resets the unit after 16 s.
- **The receive index has no bound.**  Only data bytes (< 0x80), F0 and F7
  reach the receiver.  At byte 130 the write lands on the index's low byte
  and folds it back inside the buffer, so the writes stay inside
  `+0xD22..+0xDA4`.
- **Other `abort` paths.**  Of the 101 calls to the `__throw_*` helpers
  and `abort`, 27 are `length_error` for a vector or deque at max_size,
  which does not happen.  2 are `bitset<17>::set/reset` on the UI modifier
  mask.  24 are `bad_function_call`; the data-driven ones are the journal
  reader, the replay's first event, the USB dump of the error screen and
  the project SysEx.  31 are the allocator's size check: `bad_alloc` or
  `bad_array_new_length` for a request over 1 GB.  17 call `abort`
  directly, among them operator new on a failed malloc (`0x081591EE`) and
  consistency checks in the note handlers (`0x0811758A`, `0x08117A2A`);
  these 17 were not traced one by one.

## SL3: library loads left trigs on the pattern (p59 — 2026-10-13)

The stock value handlers in the settings end by consuming their key
(`0x0814A358`, our `np_ui_consume`), so the key's release does nothing.  A
library press ran the stock press in mode 0, where no handler consumed it.
Its release, in mode 3, then entered a note on that step.  It is
reproduced in sysemu in both release orders (SEQ first, PAGE first).

`np_ui_switch` now consumes the SEQ key after the load.
test_scale_library checks that no trig is left on the 64 steps.  UI tests,
static and bootloader pass.  Image 522,176 bytes.

## SL2: the keyboard shows a loaded scale at once (p58 — 2026-10-13)

The mini keyboard shows the scale only when a view is drawn.
`0x0814A08C` draws on a mode or view change; the settings renderer
`0x0814F3B0` draws on a value key and tail-calls the keyboard renderer
`0x0814ECC8(ui)`.  A library load went through neither.  Found in sysemu by
hooking the LED setters: on a PAGE tap, LED 51 + note is set to 2 by
`0x0814EF2A`, inside `0x0814ECC8`.

Ruled out:
- re-sending the scale pick through the queue;
- UI+0x31C (the cache invalidated on a mode change);
- UI+0x8D / +0x8E.

`np_ui_switch` now calls `0x0814ECC8(ui)` right after the load.
test_scale_library checks it: while PAGE D is held, loading D7 (Messiaen 5)
lights notes 1, 5, 6, 7, 11, and D10 (a major triad) then leaves 4 and 7.
Image 522,176 bytes.

## SL: a library of 64 scales on PAGE + SEQ in the scale setting (p57 — 2026-10-13)

**What the stock has.**
- **The table.** `mgr+0x6C`, 20 entries of 14 bytes: count, 12 degrees, a
  byte.  Indices 0–15 are the built-in scales, copied at boot by
  `0x0811F85C` from flash `0x081643A0` (224 bytes; index 15 is a copy of
  Blues).  Indices 16–19 are Custom 1–4.  Their count is set to 1 (root
  only) at boot and again by the project replay `0x08124054` before it
  replays the journal, so they belong to the project.
- **Picking.** The settings value handler `0x08149CBC` (setting 1 = scale):
  SEQ n picks index n, NOTE keys set the root, PAGE adds 16 (`adds r4,#16`).
  Pattern param 4's maximum is 20.  There are no 64 slots.
- **Editing.** Hold PAGE + a NOTE key → `SetCustomScaleNoteData` (vtable
  `0x0816425C`, id 22: +6 custom 0–3, +7 note 1–11).  Its handler
  `0x08127B74` toggles the note; the root cannot be toggled.  The saver
  (`0x081262FC` → serializer `0x08125DE8`) writes one such event per note of
  each custom scale.
- **PAGE + SEQ is not free.** With PAGE held, a SEQ key still picks built-in
  scale n (queued through `0x08157578`).
- **UI modes.** The settings run in UI mode 3 (UI+0x84) with the setting in
  UI+0xEC.  The gesture that opens them was not reproduced in sysemu.  The
  tests set the state directly.

**SL.**
- **The hook.** `np_ui_switch` (np_ui.cpp) catches a SEQ press while a PAGE
  key is held in mode 3, setting 1.  The stock sees that one press in mode 0,
  which has no actions; its bookkeeping and event ownership stay.  Then
  `np_scale_library(page, seq)` (np_mnrand.cpp) sends one
  `SetCustomScaleNoteData` per note that differs from library scale
  `page*16+seq`.
- **One transaction.** The events go synchronously under one transaction
  tag, so the load is one undo step and is saved like a hand edit.
- **The library.** `kScaleLib`: 64 masks, 128 bytes.  The names and notes
  are in `docs/scale-library.json` (and `docs/scale-library.md`).  None duplicates a built-in scale or
  another library entry.
  - A: the East (maqams, ragas, Balkans).
  - B: pentatonics and hexatonics.
  - C: modes and jazz (incl. bebop).
  - D: symmetric scales, Messiaen modes and chord tones (triads, 7ths,
    fifths, fourths, a cluster).

**Verified.** `tests/test_scale_library.py`:
- all 64 load exactly, and mode, setting and the custom pick stay;
- hold PAGE + NOTE still toggles on top;
- a plain SEQ still picks a built-in scale;
- FUNC+SAVE writes custom 1 = Hijaz as its six SetCustomScaleNoteData.

test_ui_chord, probe_ui_chord, test_ui_burst, test_kits, static and
bootloader pass.  Undo (FUNC+ACCENT) was not exercised.

Image 522,144 bytes (2,144 under the cap).  The manual gained section 10:
built-in scales, custom slots and the library tables.

## ST2: styles move to G#, A# is any instruments again and clears the style (p56 — 2026-10-11)

- **TEMPO+FUNC+G#** (key 42) in mod-note mode generates all 40 mod notes in
  the style of the mod-note page it is pressed on: 1 techno, 2 dub, 3 acid,
  4 electro, 5 ambient.  Stock, this chord did nothing.
- **TEMPO+FUNC+A#** is any instruments again, as before p55.
- **The kits follow the last full generation.**  After G#, the C# and D#
  kits take its style; A# clears it.  The style no longer outlives the next
  full generation, so no reboot is needed.

`generate_all(mgr, tag, style)` takes style 0 (A#) or page + 1 (G#).
`D_MNRAND_KITS` counts A# in bits 16.. and G# in bits 24...
test_kits 4c: G# on page 3 sets acid (style byte 3), 40 new; A# then sets
style 0.

Timing (sysemu): regenerating all 40 over an existing set is ~22 ms of
main at 400 MHz, A# or G# alike.  That includes 894 ModNoteParamData
resets of the old records, each walking the record list (`0x0811FE00`).
On an empty project it is ~11 ms.

Image 521,728 bytes (2,560 under the cap).  Bootloader, static, test_kits
(17) and test_black_keys/test_kit_and_scale pass.

## ST: styles for A# and the kits (p55 — 2026-10-11)

**Choosing the style.**  TEMPO+FUNC+A# takes the style of the mod-note page
it is pressed on (UI+0xE9): 1 techno, 2 dub, 3 acid, 4 electro, 5 ambient.
No other key is needed.  The plain mixed A# is gone.  The style is stored
in `D_MNRAND_MUTED`'s top byte (style + 1; the low 24 bits still count
mute redraws).  The drum kit (C#) and the bass kit (D#) built after it use
the same style.  With none since boot they are as before.  Single-key
generation (TEMPO+FUNC+white, black + white) is not styled.

**A style** (`kStyle`, 28 bytes each) is:

- **weights** for all 43 instruments, 0–15, two per byte;
- **ranges for one Kit** drawn per generation (`draw_kit`, shared with the
  drum kit).

The kit's room, brightness and drive season every instrument
(`instrument()` now seasons when `cx.kit` is set).  Its coherence goes
across the whole set: every hat and cymbal shares one metal, toms tune over
the last kick.

| Style | Room (×0x100 raw) | Bright (st) | Drive (×0x100) | Weighted towards |
|---|---|---|---|---|
| techno | −12…0 | −2…+6 | 0…+24 | kick, hats, clap, stab, rim, bass, fm-bass, reese, metal, chord |
| dub | +4…+20 | −10…−2 | −16…0 | chord 12, bass, rim, siren, shaker, wind, crackle, pads |
| acid | −8…+4 | 0…+8 | +8…+28 | acid 14, kick, hats, clap, growl, zap |
| electro | −12…−2 | +2…+8 | −4…+16 | kick, snare, 808, cowbell, conga, talk, bleeps, zap, sync-lead |
| ambient | +8…+24 | −6…+2 | −16…−4 | pad, drone, bell, mallet, am-bell, fm-ep, pluck, wind, crackle; few drums |

Drums are 54 / 41 / 50 / 52 / 20 % of the weight and basses 17 / 16 / 25 /
18 / 6 %, in that order.

**Verified in sysemu** (A# on each page):

| Page | Style byte | Drums | Basses | Notable |
|---|---|---|---|---|
| 1 | 1, techno | 25 | 7 | |
| 2 | 2, dub | 16 | 8 | 4 chords, 2 sirens |
| 3 | 3, acid | 18 | 8 | 5 acid |
| 4 | 4, electro | 21 | 11 | 808 ×4, cowbell ×2, conga ×2 |
| 5 | 5, ambient | 8 | 3 | fm-ep, bell, pluck, pads |

Each set's hats and cymbals had one VCO1 tune.  C# and D# afterwards kept
the style byte.  The styles' drive and brightness move loudness a little
by design.  The levels (R4/R5) are the neutral ones.

Image 521,664 bytes (2,624 under the cap).  Bootloader, static, test_kits
and test_black_keys/test_kit_and_scale pass.

## p54: no telemetry, the tempo, mute draws, four instruments, three techniques (2026-10-10)

**Telemetry is a build option.**  `tools/patch.py --telemetry` builds the
MIDI CC sender: boot record, PC ring, heartbeat, LED/P6/TR/EV/PM words, and
the pot-span walk.  Off by default (`-DNP_TELEMETRY=0`): the sender's
helpers go unreferenced and `--gc-sections` drops them.  The image goes
from 524,160 to 519,488 bytes (−4,672).  Header flag `FLAG_TL = 0x08`
(+5) marks a telemetry build; test_telemetry and test_telemetry_p6 skip
without it.  Still in: the SysTick PC sampler, the fault record and the
UI observers, all small.

**Envelopes follow the tempo (`tempo_fit`).**  The recipes' times are set
at 120 BPM.  At generation, ATTACK, DECAY and RELEASE scale by 120/BPM,
clamped to ×0.5…×2.  The tempo is the transport's tick rate:
`0x08118380()` + 4, a float, 2048 at 120 BPM (np_lfo.cpp's source).
Attacks are capped at one beat (385 ms of the decay curve × 1.3 ≈ 500 ms at
120).  On the bench, 17 of the 33 renders under −45 LU were attacks of
0.8–6 s under a 300 ms note.  It applies to every generated instrument and
the drum kit.  A sound already generated keeps its times when the tempo
changes later.  Verified by hooking `tempo_fit` in sysemu at 90/140/240
BPM: every time ×(120/BPM) exactly, the cap 513/330/192 ms.

**Mute draws are drawn again (`mute`, up to five tries,
`D_MNRAND_MUTED` 0x1BFC counts them).**  A draw is mute when:

- no oscillator is up (a level, a level X-Env or a level X-Mod) and the
  filter is not self-oscillating (RESO < 0xF000; the drop's pinged
  self-oscillation sounds on the unit — the bench's noiseless model leaves
  it silent);
- or a low- or band-pass (COLOR ≤ 0x9000) stays shut more than 6 st below
  the lower sounding oscillator even at its brightest (ENV AMOUNT, the
  cutoff's X-Env and |X-Mod| added).

A noise source (S&H at audio rate on a VCO pitch) always passes.  Replayed
on the 255 bench renders, it flags exactly one: the zap at −69.6 LU.  It is
deliberately conservative: very short sounds (5 ms hats, 27 ms zaps) are
clicks by design, not mute.

**Four instruments** (appended: 39 cowbell, 40 rim, 41 conga, 42 chord):

- **cowbell** (D#, weight 3): two squares a fifth minus 10–30 cents apart
  (~1.48), band-passed.  Three kinds: the 808's knock-and-tail (an X-Env on
  VOLUME over a lower base), a TZFM clank, or choked short.
- **rim** (D#, 4): rimshot (a high ping and a click of noise), clave (a high
  sine and a ringing filter), or wood block.
- **conga** (D#, 4): conga, bongo or timbale.  The pitch bends up into the
  note or falls, 35 % slapped (noise burst); the timbale's resonance rings.
- **chord** (G#, G#+A#, 4): the dub chord.  Minor third, fifth, minor
  seventh or ninth in the scale, dark and resonant, the filter plucked, 40 %
  a per-step S&H on the cutoff.  stab already makes bright two-note stabs
  in the scale; this is its dub counterpart.

perc's cowbell, rim/clave, wood block/conga and bongo cases moved into
these.  perc keeps the metal can, AM ping, zip, blip, metal ping, triangle
and tick, and its weight goes 6 → 4.  The drum kit's B key draws perc,
cowbell, rim or conga.

**Three spice techniques** (spice draws below(16)):

- colour FM: a ratio X-Mod on COLOR at the note (1× or 2×), so the filter
  turns BP/LP/HP each cycle;
- resonance FM: the same on RESO;
- slow evolution: a 0.02–0.12 Hz LFO on VCO1's wave or the cutoff.

The colour and resonance FM depth is `deeper_fm`'s ×1.4 too (ratio type).
How they sound on the analog filter is untested: the bench's filter is a
model.

**Levels.**  Bench: 6 draws of each new instrument and 3 of each capped
pad, ~6 min on 8 cores.  Same rule as p51: the midpoint of median and
energy at −29 LU, energy ≤ −26.5, at most 100.

| Instrument | Loudness | Level |
|---|---|---|
| conga | −26.6 at 64 | 49 |
| chord | −25.4 at 64 | 42 |
| cowbell | −32.6 at 64 | 100, VOLUME's headroom |
| rim | −38.2 at 64 | 100; a click, which the 400 ms window underrates |
| perc, without the moved cases | −26.8 at 100 | 78 |

With the attack cap the long-attack sounds stopped being silent and came
out loud: pad 86 → 42, drone 100 → 55, phaser-pad 63 → 35, riser 40 → 33.
drone's median went from −59 to −24.  wind stays at 100 (−38, full volume).

Image 521,248 bytes (3,040 under the cap).  Passing: bootloader, static,
test_patch, test_kits, test_black_keys (37 → cowbell, rim, cymbal, tom;
42+44 → chord among them) and test_kit_and_scale (B: perc or conga).  The
long sound suites were not run.  Test tables: NAMES, WEIGHT and GROUP +4,
perc's weight 4.

## K4: TEMPO+FUNC+F# puts every mod note back on C4 (p53 — 2026-10-10)

**What the stock page randomizer does.**  RandPageNoteData (vt
`0x08170128`, handler `0x08143D5C`) spawns 1–4 RandNoteData events (vt
`0x08170098`, handler `0x081434DC`) on random free steps of the chosen SEQ
page.  In mod-note mode (request +12 == 2) the handler takes a random mod
note of the current mod-note page, `(rng & 7) + page * 8`.  It writes the
pitch as a constant: `movw r6, #15300` at `0x08143696`, 60 × 255, C4.  A key
press plays the mod note's own pitch, UI+0x98 semitone and UI+0xC0 octave,
set by UP+DOWN+key or by the bass kit (K2).  So once a key has its own
pitch, the randomizer's steps play it elsewhere.  sysemu, A minor after the
bass kit: keys 57, 59, 62, 65, 69; their steps all 60.

**K4.**  In mod-note mode, TEMPO+FUNC+F# (key 40) sets UI+0x98 and UI+0xC0
of all 40 mod notes to 0 (C4); the records, i.e. the sounds, stay as they
are.  It sends no event.  `np_mn_chord_check` takes key 40 before the stock
check; stock, that chord did nothing.  test_kits 4b: the bass kit's pitches
go to 60 on all 40 notes, the records are unchanged, key C' plays 60.

Not done (offered): have the randomizer write the key's own pitch instead
of C4, a hook at `0x08143696`/`0x081436A2`.

Image 524,160 bytes (128 under the cap).  Bootloader, static and kits pass.

## R6: D#+F# FM basses, a deeper FM bass (p52 — 2026-10-10)

**D#+F#** (black-key bits 6) picks `G_FM = 512`: fm-bass alone (F# keeps it
among the deep basses).  It works in mod-note (R2) and normal mode (P1).
D#+G# and other unlisted sets still take the union of their single keys.

**Why the FM was thin.**  In p51 half the DX fm-basses left the X-LFO amount
at 0.  The index came only from the X-Env routed to the amount, which dies
within 12 ms–1.1 s, so the FM was the attack and the tail was a sine.

**The recipe now.**  Final values, after the +40 % widening and `deeper_fm`:

| Part | p51 | p52 |
|---|---|---|
| Branch | 40 % DX, 40 % TZFM, 20 % both | a third each |
| DX index that stays (X-LFO amount on VCO2 pitch) | 50 % of patches, 3–43 st | always, 5–56 st (+34…+76 % of the right half) |
| DX index envelope | peak 20–121 st, decay 12 ms–1.1 s | peak 19–126 st, decay 5 ms–2.7 s |
| DX swell | none | 30 %: index attack 26–426 ms |
| DX ratios | 0.5…6 | 0.5…7: 0.5, 1, 1, 1.5, 2, 2, 3, 4, 5, 7 × carrier |
| DX irrational ratios | 20 %, 16 of bank B | 25 %, 24 of bank B |
| TZFM base | 0–37 % | 0–51 % |
| TZFM envelope decay | 14–821 ms | 6 ms–1.8 s |
| 4-op | 35 % | 50 %, index 10–105 st, stays 0–47 st |
| Moving growl | none | 40 %: synced X-LFO on TZFM, S&H (every step its own) or sine/triangle wobble, 1/4…1/16, depth 0.05–0.44 |
| Carrier wave | sine to 16 % of the way to triangle | sine to 30 % |
| Filter | cutoff ~520 Hz–open, ENV −5…+40 | cutoff ~750 Hz–open, ENV 0…+50, resonance 0–53 % |

**Level.**  Bench: 21 renders, 7 processes, distinct seeds, ~3 min.  At
level 24 the new recipe measured −30.0 LU (midpoint of median and energy).
Level 27 puts it at −29.

Image 524,128 bytes (160 under the cap).  Bootloader, static, kits and the
black-key test pass (37+40 → fm-bass ×4; 37+42 → the union).
`test_patch_random` and the long sound suites were not run.  Test tables
updated: GROUP fm-bass 520, COMBO 37+40, `want & 0xFF` for the
`D_MNRAND_LAST` byte, test_kits checks `& 24`.

## R5: G#+A# plucked and struck, levels again (p51 — 2026-10-10)

**G#+A#.**  The one pair left over now picks the plucked and struck tonal
instruments: pluck, bell, stab, fm-ep, mallet, am-bell (`G_PLUCK = 256`; G#
alone keeps them, so `Group` and `Entry::group` are 16 bits now).  Both
mod-note (R2) and normal mode (P1) read it through `black_groups()`.
`D_MNRAND_LAST` keeps only the low 8 bits of the groups, so G#+A# shows as 0
there.  `test_black_keys` now presses 42+44 (fm-ep, bell, pluck, am-bell).

**Why p50 still stuck out.**  The p50 levels came from the median of three
draws.  A further 6 draws per instrument (234 renders, ~23 min on 8
processes) showed two faults:

- **The median hid the loud draws.**  Within one instrument the draws spread
  9 dB rms (bass −39..−15, pluck −36..−18, tom −52..−17), and the median sat
  at the target while the loud third stood 6–10 dB over it.
- **`leveled()` scaled VOLUME's base only.**  The gate is a synced X-Mod on
  VOLUME at nearly full depth over a low base: p50 cut it 8.9 dB on paper,
  and the renders came out 3 dB louder.  Crackle (audio-rate X-Mod on
  VOLUME), clap (X-Env on VOLUME) and some mallets and pads are built the
  same way.

**Fixes.**

- **The whole VOLUME scales.**  `leveled()` now scales the X-Mod and X-Env
  depths too.  The depths are cubic, so they take the factor's cube root:
  five Newton steps in float, exact to 5 digits for the levels in use.
- **New levels.**  Each instrument's level puts the midpoint of the median
  and the energy mean of its 9 draws at −29 LU.  The energy mean is capped at
  −26.5.  The kick stays at full volume as the reference (energy −27.4,
  median −28.3), and no level goes past 100.

New levels (x/64, p50 → p51):

| Change | Instruments |
|---|---|
| Down | bass 77 → 37, sync-lead 81 → 43, stab 64 → 31, growl 45 → 22, tom 85 → 47, pluck 85 → 46, acid 85 → 52, metal 85 → 58, phaser-pad 85 → 63, bell 35 → 21, hoover 26 → 16, hat-open 41 → 31, fm-bass 34 → 24, fm-ep 27 → 19, reese 24 → 18, bleeps 27 → 21, crackle 58 → 49 |
| Up | lead 23 → 28, wobble 19 → 21, clap/zap/perc → 100 |
| Gate | 23 → 17, but its depth now follows: about −11 dB |

The quiet full-volume ones (hat-closed, shaker, drone, wind, drop) are at
100 and cannot go louder through VOLUME.

**Not done: a per-patch correction.**  A loudness model from the patch's own
settings (VCO mix, the filter shut below the note with ENV AMOUNT, decay
time) cut the within-instrument error from 8.8 to 6.8 dB rms on the fit.
In simulation, with VOLUME clamping, it cut the spread only from 9.0 to 8.8.
That is not worth the 288 bytes left.

Image 524,000 bytes (288 under the cap).  Bootloader, static, kits and the
black-key test pass; the long sound suites were not run (sound tweak,
checked by ear on the unit).

## R4: ranges +40 %, levelled instruments (p50 — 2026-10-10)

**Ranges.**  `Gen::range` now widens each drawn interval by a fifth of its
width on each side (+40 % in all, was +25 %); pitch still `exact()`.  The
long sysemu sound tests' bounds were set for +25 % (p49) and were not re-run.

**Levels.**  The bench (8 processes, 3 draws × 39 instruments, 300 ms
notes) measured each render's K-weighted momentary loudness (BS.1770
pre-filter at 32 kHz, 400 ms, the maximum; the WAV's peak normalisation
undone with the render's printed peak).  Median of the three per
instrument, from −64.5 (drone, a slow attack under a 300 ms gate) to −14.7
(siren); the kick −29.5, the median −26.7.  Target −27 LU: louder ones cut to
it (at most −12 dB: siren −12, 808 −10.7, wobble −10.5, am-bell −9.5, lead
−9, gate −8.8, cymbal/reese/mallet −8.4, hoover −7.7, bleeps −7.5, fm-ep
−7.4, fm-bass −5.5, bell −5.1, hat-open −4, riser −3.8, growl −3.1, talk −2,
crackle −0.9), quieter ones raised at most +2.5 dB (the VOLUME headroom, and
an instrument whose draws spread widely must not have its loud ones stick
out).  Stored as `Entry::level` (x/64, in the entry's padding byte) and
applied by `leveled()` to the output volume after the recipe, spice and
deeper FM (and to a kit member after its seasoning).  The voice is a model:
the unit's analog drive and filter will differ.

## R3: black-key pairs, wider ranges, deeper FM (p49 — 2026-10-09)

**Groups** (`enum Group`, one bit each; `kCombo` maps a held set of black
keys, bits C# D# F# G# A# = 1 2 4 8 16, to one):

| keys | group | instruments |
|---|---|---|
| C# | G_KICK | kick |
| C#+D# | G_HATS | snare, clap, hat closed, hat open |
| D# | G_DRUMS | tom, perc, cymbal, shaker, drop, metal |
| F# | G_DEEP | bass, fm-bass, reese, 808 |
| F#+G# | G_BASS | acid, wobble, growl |
| G# | G_TONAL | lead, pad, pluck, bell, stab, sync-lead, hoover, phaser-pad, gate, fm-ep, mallet, drone, am-bell, talk |
| A# | G_ZAP | zap, bleeps, glitch |
| F#+A# | G_OTHER | riser, siren, wind, crackle |

Any other held set picks the union of its keys' own groups.  The same
`black_groups()` serves R2 (mod-note mode, black keys held + a white key) and
P1 (normal mode, TEMPO+FUNC + a black key, a second black key pressed while
one is held picks the pair).  The bass kit (D#, K2) draws from G_DEEP|G_BASS;
spice goes to G_DEEP, G_BASS and G_TONAL.

**+25 %.**  `Gen::range` widens every drawn interval by an eighth of its
width on each side (clamped to 0..0xFFFF).  A VCO's tune and detune (slots
0, 1, 4, 5) draw `exact()` — through `Patch::draw`, used by `base`/`bases` —
and so does a kit's shared metal detune.  `irange` (semitones, intervals,
divisions, the kit's room/bright/drive) is unchanged.

**FM +40 %.**  `deeper_fm()`, after every recipe (and a kit member's
seasoning): each slot whose X-Mod runs at audio rate or at a ratio of the
note, except the S&H noise, gets its X-Mod amount and — when its X-Env is
routed to that amount (dest 1, the index envelope) — the X-Env depth 1.4×
as far from the centre.

**Room** (the image was at the cap): the withdrawn Y4 sqrt memo's code
(`np_sqrtlut*`, 104 bytes, never installed since p29), the early boot's
per-word clears of the diag counters `np_runtime_init` clears again right
after (≈ 120 bytes), and `np_ratio_value` (512 bytes, the ratio *values* the
offsets were computed from — only tests read it; `test_patch.py` now parses
them from `np_lfo.cpp`).  Header words +0x16C..0x174 and +0x58 are 0.  Image
523,776 bytes, 512 under the cap.

---

## PL: a project loaded while playing takes over at the pattern's end (p47 — 2026-10-09)

**The stock** (sysemu): SEQ in LOAD mode (`0x08149DC6`) calls `0x0811F8DC`,
which queues a read of the slot's 512 KB into the store image at
`0x24000000` and records the slot (mgr+0x72C).  The read's completion is an
std::function whose invoker (`0x081244D0`, literal at `0x0811F938`) replays
the journal (`0x08124054`): the whole project changes wherever the transport
stands — the tracks keep their positions, so the rest of the pass plays the
new project's steps.  A pattern change, by contrast, is queued (mgr+0x189 +
layer × 2), promoted by the step processor at the pattern's end
(`0x0811FC80` → mgr+0x7BC/0x7BD) and carried out by main's project service
(`0x08125AC4`, called at `0x08119A70`) about 50 ms ahead of the boundary.

The tracks: `seq` = `0x2000CFF4`, track *t* at +*t*·36 — +0x44/+0x48 the
position in ticks (256 a step, 2048/s at 120 BPM), +0x60 the pattern's
length in ticks; the transport (`0x2000AF78`) +0x6C is "running".

**PL.**  Four sites, `D_PLOAD` (diag +0x1BF8: bytes requested-while-playing,
reads-completed, caught-up):
- the request (`bl` at `0x08149DC6` → `np_load_request`) counts when playing
  and calls the stock load;
- the completion (the literal → `np_load_done`) replays at once if no synced
  request is outstanding (stopped: the stock), otherwise only counts;
- main's project service (`bl` at `0x08119A70` → `np_proj_service`): while a
  synced load is outstanding it drops a promoted pattern change (mgr+0x7BC);
  once every requested read is in and track 0 is in the **last step** of its
  pattern (or the transport has stopped), it replays, clears the queued
  pattern changes, and puts both tracks as many ticks before the end of
  their new patterns as track 0 had left — step 1 of the new project falls
  on the old grid, whatever its pattern's length;
- the save key (`bl` at `0x081516A2` → `np_save_gate`) does nothing while a
  load waits (the image already holds the new project), and the
  flash-touching pastes wait (`np_qspi_defer`).

A load asked for after the last step began, or whose read is not in by the
boundary, takes over at the end of the next pass.  The replay runs in the
last step: 0.3 M instructions for a small project, ~41 M (~100 ms at
400 MHz) with 40 full mod notes (the stock replay inserts records one by
one); a replay longer than the step makes step 1 late by the excess.  No
stuck note across the swap (sysemu: held-note count and amp at 0 after the
boundary, stock and PL alike).

**p48 — the switch was erratic on the unit after p47.**
Taken apart with two real projects (a note on every step against a sparse
one), two faults, the second the stock's own:
- *The old project's last step lost its note.*  A step's notes fire once the
  position is past their tick; the swap at exactly the last step's tick
  (left = 256) replaced the project first.  The swap now waits until 32
  ticks into the last step (kLead 224: 16 ms at 120 BPM, then ~109 ms for
  the replay).
- *The load stumbles the moment it is asked for* — stock and PL alike.  The
  slot is read by one polled `HAL_QSPI_Receive` of the whole 524,288 bytes
  (`0x08103E9C`; QPI fast read 0x0B, 6 dummy cycles, prescaler 11: ~16.7 MHz
  × 4 bits ≈ 8.3 MB/s ≈ 63 ms on the unit, 101 ms in sysemu) in main, and
  every step that falls in it comes late (sysemu: step 8 at +216 ms, step 9
  at +35).  Playing, PL now holds the read (D_PLOAD byte 3 = slot | 0x80)
  until 16–64 ticks into a step that is not the last, so it has the rest of
  the step to run in; stopped, the stock's read at once.

**Emulator note.**  sysemu's projects grow out of a two-event journal, so a
save leaves the patterns without a section of their own pointing at the end
of the journal, while the load's replay rebuilds them as 0 and the stock's
own check (`0x0812414C`, code 0xA2) refuses the slot — on to the recovery
screen (`0x0814FA54`: code on the LEDs, a default project, wait for a
sysex project, `b .`).  `tests/test_sync_load.py` writes the replay's values
into the saved slot (a unit's factory-laid-out projects have them).

Size: 352 bytes; the image is 524,192 (96 under the cap); p48: 524,256 (32 under).
`tests/test_sync_load.py` (15 checks): mid-pattern, stopped, save and pattern
change while waiting, an 8-step new pattern, a late request.  `--no-sync-load`.

---

## K2, K3: a bass kit in the scale, and every mod note at once (p45 — 2026-10-08, K2 reworked p46 — 2026-10-09)

**K2, TEMPO+FUNC+D#.**  A different bass on every key (p46; p45 gave all
eight one recipe with per-key articulations): each key draws its own bass by weight, none twice until all seven
basses (bass, acid, fm-bass, reese, wobble, growl, 808) are on the page,
so a page holds all seven and one more.  Each key is a degree of the
pattern's scale up from the root — C the root, D the second, …, C' the
octave; a scale of fewer notes carries on into the next octave; no scale:
C major, each key its own note — and its bass is built for that note, so
what the recipe puts in the scale (a fifth, an interval) is in it from
there.  The root sits around C4 (C..F above it, F#..B below).

A mod note's pitch is **UI state**, not project data: UI+0x98[n] semitone
and UI+0xC0[n] octave (−5..+5), zeroed with the UI object (`0x0814A978`),
set only by UP+DOWN+key (`0x08152990`) and key+UP/DOWN (`0x08150FF4`); a
recorded step keeps its own pitch.  K2 writes the page's eight to the
degrees, so playing or recording the page plays the scale.

**K3, TEMPO+FUNC+A#.**  R1 on all forty mod notes (five pages), one
transaction; each at its own pitch, no immediate repeat per mod note.
sysemu: 22 kinds in one draw, 15..35 records each (926 in the list), no
Error_Handler, heap high-water ≈ 68 KB of the 240 KB pool; main is busy
5.4 M instructions ≈ 13 ms (D#: 1.5 M, C#: 1.0 M).

`D_MNRAND_KITS` counts C# in bits 0–7, D# in 8–15, A# in 16–23.  p46
image: 523,840 bytes (448 under the cap) — the next feature needs space
first.  `tests/test_kits.py`; `--no-kit` gives the stock back
for all three.

---

## P1, D1: the instrument randomizer for the patch; clear patch spares mod notes (p44 — 2026-10-08)

**What the stock has.**  In normal mode (UI+0x84 = 1) TEMPO+FUNC + a note
key only plays the note — no event (sysemu, C, C#, D, D#, F#, C').
TEMPO+FUNC+[FOLLOW] (group-0 key 1) sends `RandPatchData` (+6 = UI+0xEF,
+7 = **5**); its handler `0x08144494` gives about half of slots 2, 3, 6..17
a random base — but in record type 5, which is the [FOLLOW]-held offset
layer (pitchbend, manual p. 2: holding key 1 and turning pot 5 sends
`PatchParamData` type 5), **not the patch**.  A knob turn writes type 0.

The patch is stored like a mod note: records in the manager's list
(`+0x1BC`, `[4]` layer, `[5]` type, `[6]` slot, `[7]` local, `[8]` value)
over a defaults table — `0x08164480` for the patch (via `0x08120A9C`),
`0x0815E8A0` for a mod note, **identical** 19 values; locals 1–5 0x8000,
6–8 0.  So every recipe means the same in the patch.  `PatchParamData`
(`0x0815EE7C`, 16 bytes): +4 tag, +8 pattern (UI+0xEF), +9 type, +10 slot,
+11 local, +12 value (u32); a value equal to the default removes the record.

**P1.**  The thunks `0x08147B94` / `0x08147B9C` hand a key to the
normal-mode press `0x08151DA4` / release `0x08151FFC` with a final `b.w`;
those now go to `np_nm_key_press` / `np_nm_key_release`.  With TEMPO and
FUNC held (and UI+0x84 = 1) a press builds an instrument — any kind on a
white key, the R2 group on a black one (C# kick, D# other drums, F#
basses, G# tonal, A# effects) — for the note the press is about to play
(`0x0814A3C8`: key − 34 + UI+0xEA × 12), writes it as PatchParamData type 0
(every value off the reset, and the reset for every old record the sound
does not set), marks the CH fade's last instrument (a patch note is 0x1FF)
as switched, and calls the stock press, which plays it (the N1 snap applies:
it is a note start).  Both stock handlers read the RECORD flag (UI+0xED)
only to record the note, and nothing they call reads it, so for the chord's
key it is held at 0 across the press and its release: no step.

**D1 — a stock bug found on the way.**  `ClearPatchData` (`0x081648B4`,
+6 pattern; sent by DELETE (key 33) + RECORD) is handled by `0x081279C0`:
for each layer showing that pattern it resets every record whose **byte +4
is the layer, whatever its type**.  A mod note's records (type 7) carry the
mod note number there — so clearing layer 0's patch also reset **mod note
0** (layer 1's: mod note 1).  Measured: records of mod notes 0, 1, 5 →
DELETE+RECORD → 1 and 5 left.  P1 therefore does not use ClearPatchData,
and D1 replaces the match `ldrb r2,[r3,#4]; cmp r2,r6` at `0x08127A12`
with `bl np_clear_match`, which also turns type 7 away (flags out, r2 dead
after).  The patch's offset layers (types 1–5) are still cleared, as stock.

The stock copy NOTE+RECORD (CopyModNoteToPatchData) does not clear the
patch first: a parameter the mod note leaves at its default keeps the old
patch's value (sysemu: the project patch's VCO1 +6 st stayed).  Not changed.

Size: P1 + D1 = 768 bytes; the image is 523,424 (864 under the cap).
`tests/test_patch_random.py`: the chord, the records, all 39 instruments
built at C4 and played (the mod-note test's checks), black keys, RECORD,
no chord / mod-note mode, 120 draws, D1.  `--no-patch-random`,
`--no-clear-fix` give the stock back.

---

## X1: the X-Mod as an instrument, and a listening bench (p43 — 2026-10-08)

### The bench (`tools/bench/`)

`render.c` is a digital stand-in for the analog voice; `bench.py` drives the
real firmware in sysemu, captures **every control value the DSP publishes,
every sample** (31 floats at `engine+0x2EB4..+0x2F2C`), renders them to a
WAV in `build/bench/wav/` and prints where the level peaks, how fast it
falls 20 dB, the spectral centroid early and late, and the pitch (YIN).
`--cv` prints the control values, `--set "slot.local=raw,..."` plays a
hand-made mod note.

The control values, mapped by sweeping every knob (sysemu):

| float | what | law |
|---|---|---|
| 0–3 | VCO1 saw, square, sine, triangle level | exponential: 1.0 at unity, −0.0502 per halving (≈120 dB per unit); a linear crossfade between neighbouring shapes |
| 4–7 | VCO2 square, saw, triangle, sine — with VCO2's volume in them | same, max 0.945 (−6.6 dB) |
| 8 | TZFM depth | exponential |
| 9 | VCO1 volume (VCO1's shape also feeds the TZFM, so its level is separate) | exponential, max 0.945 |
| 10, 11 | VCO1, VCO2 pitch | semitones, MIDI |
| 12 | cutoff | semitones, 81 = 440 Hz |
| 13 | output volume | exponential |
| 14, 15 | resonance, colour | linear 0..1 |
| 30 | amp envelope | linear |

Every exponential CV is the firmware turning a linear knob into dB, so a
modulation in knob units is linear in gain: X-Mod on a level is true AM.
The voice model: polyBLEP shapes at 4× oversampling, thru-zero linear FM of
VCO2 by VCO1's shape (up to 4× its frequency), hard sync, a mixer that
drives the filter (negative half clips first), a resonant TPT SVF plus a
pole (18 dB) with BP→LP→HP and the all-pass between, the VCA.  The
shapes, the TZFM depth, the filter and the drive are plausible analog
behaviour, not Mono's circuits; the parameters are exactly the firmware's.

The bench's first catch: p42's FM bass could put its cutoff at 65 (≈ 200 Hz
at C4) and a spice X-Env could close it further — the FM was there in the
pitch CV and filtered away (centroid ~100 Hz).  FM voices keep the cutoff
at 95 and up now.

### Techniques (`np_mnrand.cpp`), all verified at the CV level

* `op_fm` — a VCO's pitch FM'd by a **ratio** X-Mod (note-tracked), its index
  an X-Env on the X-Mod amount (dest 1), sometimes a steady remainder.
* `am` — an X-Mod at audio or ratio rate on a **level** (VCO1, VCO2, output):
  AM sidebands, ring-ish gating with a square, S&H grit.
* `filter_fm` — ratio or audio-rate X-Mod on the **cutoff**, optionally
  faded by an X-Env: filter FM that stays in tune with the note.
* `wave_fm` — a ratio X-Mod on a **waveform**: the shape flips at the note's
  rate, harmonics without the filter.
* `humanize` — a small **synced S&H** on decay, ENV AMOUNT or cutoff: every
  step a little different (hats, acid accents).
* `ping` — the oscillators off, a blip on VCO1's level into a filter at the
  edge of oscillation: the filter rings (Mono forum tip).
* `four_op` — each VCO's pitch FM'd at an irrational ratio plus VCO1→VCO2
  TZFM: four operators of metal (the manual's "4-op hybrid FM").

Where they went: FM and pinged-filter kicks; snares with audio-rate S&H on
the cutoff or the noise level, or a humanised decay; 4-op hats and cymbals
with humanised decays and audio-rate sizzle; percussion adds a metal can,
an AM ping and a filter-FM zip; bass adds note-locked filter FM, wave FM,
sub AM or an FM bite; FM bass can FM its modulator (four operators); acid
gets per-step accents and lengths; wobble can wobble the colour or the
waveform (and the resonance with it); growl's filter FM can be a ratio;
lead an FM operator or wave FM; pad a drifting colour and a glistening FM;
pluck an FM pluck; bell AM bells; stab 90s FM stabs; gate a humanised
cutoff; bleeps AM; drone colour drift and slow FM.  Spice gains filter FM,
wave FM and humanise.

**Correction — the 512 KB cap is real and p43 as first shipped (525,440
bytes) bricked the update.**  I read the bootloader's message-count check
and concluded the image could run to the settings sector; I had not read
the stream header parse, which caps the *uncompressed* size at `0x80000`
and refuses the update after bank 2 has already been erased (protocol.md
§3.4).  The unit stays in the bootloader and takes a good file again.
The fix: the recipes' all-constant `filter`/`adsr`/`xenv` calls take their
arguments from tables in flash (`KFILTER`, `KADSR`, `KXENV`), runs of
constant bases merge into one call (`KBASES`), and `np_mnrand.cpp` is
compiled `-fno-inline`: 27,353 → 24,573 bytes; the image is 522,656 bytes,
1,632 under the cap.  `tools/patch.py` now refuses any image over
`0x80000`; `tests/test_bootloader_accepts.py` runs the bootloader's own
routine on the .syx (it reproduces the failure on the first p43 file: the
erase, no flash word written, the return).  The emulation harnesses in
`test_patch.py` and `probe_ui_chord.py` map the 896 KB application region
(they had mapped 512 KB and faulted on the larger image).

New instruments: **metal** (drums: struck cans, sheets, gongs — 4-op or
index-enveloped FM, band- or all-pass), **am-bell** (ring-modulated bells,
sometimes with a TZFM ping), **talk** (saws into a resonant band-pass: a
wah, a "yoi" or a moving colour), **wind** (drifting band-passed noise),
**crackle** (noise sampled-and-held in level at a low audio rate, a
tempo-jumping cutoff), **808** (a long tuned kick with drive).

Sources for the sound design: the Mono manual; [MusicTech](https://musictech.com/news/gear/norand-mono/)
and [Sound On Sound](https://www.soundonsound.com/reviews/norand-mono) on
X-Mod/X-Env; [Elektronauts](https://www.elektronauts.com/t/norand-mono-analog-bass-synth-with-sequencer/141049?page=10)
on mod-note kits; 808 cymbal synthesis ([Cherry Audio](https://docs.cherryaudio.com/misfit-audio/transistor-808-cymbal));
FM ratios ([DecentSampler](https://decentsampler-developers-guide.readthedocs.io/en/1.21.1/topic-how-to-use-fm-synthesis.html),
[Elektronauts wood/ratio offsets](https://www.elektronauts.com/t/wood-sounds-tutorial-with-ratio-offsets/99029));
AM/RM ([cmtext](https://cmtext.indiana.edu/synthesis/chapter4_am_rm.php));
humanising decays with S&H ([Elektronauts](https://www.elektronauts.com/t/how-to-humanize-closed-hi-hats/41614)).

---

## W1: wider sounds, spice, a better seed (p42 — 2026-10-08)

* **Every recipe wider, most with characters.**  The kick is a boom (808),
  a punch, a hard driven kick, a sub or a clicky techno kick, each with its
  own pitch window (−36..−20 overall), drop (5–60 st over 8–200 ms) and
  decay (0.09–2.5 s), a click or noise burst, sometimes a TZFM growl or a
  resonant closing filter.  The snare body runs from thick to piccolo
  (−17..+2), its noise from wash to rattle, four filter characters
  (band→low, all→high, ringing, lo-fi).  Hats 12–40 st metal, 8–130 ms
  closed, 180–1200 ms open, 15 % band-passed ticks.  Perc has eight:
  cowbell, rim, blip, wood, a metal ping, the triangle, a bongo, a tick.
  Bass three octaves, five VCO2 layouts, any wave from triangle to saw,
  cutoff 40–105 with resonance to 0xB000 and a (sometimes negative)
  envelope, any ADSR.  FM bass: carrier −36/−24/−12, a ratio X-Mod with
  modulator 0.5–6× the carrier (20 % from the irrational bank), index
  4–36 st falling in 30–700 ms and 40 % some that stays, **or** analog
  TZFM from VCO1 at a musical interval, or both.  Everything else
  likewise (see `patch/np_mnrand.cpp`).
* **Spice** (`spice()`): after a bass or tonal recipe, none to three (30 /
  40 / 20 / 10 %) of: an LFO on the cutoff (free or synced), an X-Env on
  the cutoff, a TZFM bite, a drifting VCO2 detune, a moving waveform, a
  pitch chirp on both VCOs, vibrato, breathing resonance, drive, tremolo —
  each only on a slot the recipe left unmodulated.
* **Seed:** a state that lives across presses (`D_MNRAND_SEED`, 0x1BE8,
  cleared at boot), advanced by the golden ratio, stirred with CYCCNT and
  the stock generator, and avalanched with murmur3's finaliser.
* **No immediate repeat:** a random draw that gives the instrument this mod
  note already has is drawn once more (a group of one — C# — keeps it).
* FM bass weight 5 → 6 (sum 161).

Measured over 30 draws each (sysemu): kick tune −34..−20, decay
0.1–2.0 s, cutoff 76–147, resonance to 0xBB; bass −36..−12, decay
0.08–1.45 s, cutoff 42–102, resonance to 0xA5, 0–5 modulated slots; pad
1–6 modulated slots (average 3.3).

---

## K1, S1: a drum kit in one gesture, and the pattern's scale (p41 — 2026-10-08)

**K1 — TEMPO+FUNC+C#** (mod-note mode) builds a drum kit over the eight mod
notes of the current page: C kick, D snare, E clap, F closed hat, G open
hat, A tom, B perc, C' cymbal (60 %) or shaker.  The TEMPO+FUNC randomize
handler `0x081524CC` checks its key with `0x0814A3E4`, which turns a black
key away (verified: TEMPO+FUNC+black reaches the handler and does nothing);
the check call at `0x081524D4` becomes `np_mn_chord_check`, which for C#
builds the kit and answers 0 so the stock creates no event.  Other black
keys get the stock check (D#, F#... are kept for later kits).  One
transaction tag for all eight.

What makes it one machine — drawn once per kit (`Kit`):

* room: a raw offset on every decay and release, about ×0.6..×1.6;
* brightness: ±6 semitones on every cutoff;
* drive: a raw offset on every sounding VCO level;
* one metal: the closed hat, the open hat and the cymbal share VCO1's
  pitch, VCO2's interval and detune, and the TZFM (`metal()`);
* the tom an octave, a fifth or an octave and a fifth over the kick
  (`Kit::kick_note`), in the scale if there is one.

**S1 — the pattern's scale.**  Every generation (R1, R2, K1) reads the
playing pattern's scale and root — bank 0 at manager+0x18D, [4] scale, [5]
root, degree lists at manager+0x6C (the layout `np_random.cpp` reads; set
with the Scale page or pattern parameters 4 and 5) — and the pitch the mod
note plays at (UI+0x98[n] + (UI+0xC0[n] + 5)·12, what `0x0814FE5C` puts in
its note-on; C4 unless UP+DOWN+note changed it).  Chromatic (or an empty
custom scale) changes nothing.  With a scale:

* VCO2 intervals (stab, gate, lead, pad, phaser-pad, drone, pluck, bass's
  fifth, tom's body) are drawn from those that land on a scale tone; if
  none does, an octave, unison or fifth that does;
* the kick sits on the root or the fifth (44–65 Hz widened by two
  semitones each way so the root is always there), the snare body and the
  tom on the root or fifth of their ranges;
* perc (cowbell, rim, blip, wood) and zap end on scale tones;
* the drop's ringing filter sounds a scale tone (coordinate c sounds MIDI
  c − 12).

A mono interval is fixed in the mod note: it is in the scale against the
note the mod note plays at; on another degree it may not be.  Bleeps' S&H
stays atonal (quantising it would need the DSP).

`D_MNRAND_KITS` (0xFFC) counts kits; `D_MNRAND_KIND` (0x1BC0, 40 bytes) is
each mod note's instrument + 1 as last generated (cleared every boot).
`--no-kit` builds without K1.  `tests/test_modnote_random.py` sections 6–7:
two kits — the layout, every member non-trivial, nothing off the page
touched, no stock event, one metal, the tom over the kick; TEMPO+FUNC+D#
does nothing; in A minor all VCO2 degrees, kicks and drops in the scale;
chromatic, the kick over its whole range again.

---

## R2: black keys pick the instrument group (p40 — 2026-10-08)

In stock mod-note mode the five black keys do nothing (verified in sysemu:
the press handler `0x08152194` runs, its key check `0x0814A3E4` maps a black
key to 0xFF and returns — no note, no event; TEMPO+FUNC+black does nothing
either).  Use them to choose what the randomizer makes: hold a black key,
press a white one.  TEMPO+FUNC+white stays "any instrument".

| black key | group |
|---|---|
| C# (35) | kick |
| D# (37) | every other drum: snare, clap, hats, tom, perc, cymbal, shaker, drop |
| F# (40) | basses: bass, acid, fm-bass, reese, wobble, growl |
| G# (42) | tonal: lead, pad, pluck, bell, stab, sync-lead, hoover, phaser-pad, gate, fm-ep, mallet, drone |
| A# (44) | effects: zap, glitch, bleeps, riser, siren |

Several black keys held = the union; weights inside a group are the
catalogue's.  No hold time: the black keys are out of the way of
playing.

* **Hook:** the press handler's tail `b.w 0x0814FE5C` at `0x081521AA` (after
  the key check has put the key's mod note in UI+0xE8) → `np_mn_key_press`.
  It reads the black keys from the UI consumer's switch state
  (`np_ui_pressed`, np_ui.cpp — the state as of this event, not the DMA
  scanner's), builds the instrument (`generate`, shared with R1) and then
  calls the stock press, which plays the note-on **synchronously** — so the
  press plays the new sound.  The transaction tag is taken the way
  `0x08157578` takes one (manager +0x4C advanced, +0x58 = 0), since the
  events are dispatched directly instead of queued.
* **No step recorded:** the two reads of the RECORD flag (UI+0xED) — in the
  press (`0x0814FEB8`) and the release (`0x081521FE`) — become calls to
  `np_mn_rec_press` / `np_mn_rec_release` (np_patch.S), which return 0 for a
  mod note marked by a black + white press (`D_MNRAND_MARK0/1`, 0xFF4/0xFF8,
  a bit per mod note) and clear the mark on the release.  A plain press
  clears it too, so a lost release cannot stop recording for long.
* **From silence:** `generate` sets the CH "last instrument" of a layer that
  last played this mod note to 0x1FE, so if the old sound still rings, the
  next note-on fades it (1 ms) and the new one starts from 0 — for R1 too.

`D_MNRAND_LAST` gains the group mask in its top byte.  `--no-black-keys`
builds without R2 (it also needs R1 and the UI fix).
`tests/test_modnote_random.py` section 5 (10 checks): each black key and
D#+F# four times — every instrument in its group, the note-on
after the build; a forced kick's drop is in the very note the press played
(18 st above at 1 ms, settled by 100 ms); a plain white key generates
nothing; with RECORD lit a plain press submits RecSeqData on press and
release, black + white submits nothing, the mark is cleared, and the next
plain press records again.

---

## R1: the mod-note randomizer builds instruments (p38 — 2026-10-07)

Stock TEMPO+FUNC+note in mod-note mode randomizes every knob independently,
which mostly yields noise.  R1 picks an instrument first (kick, hat, snare,
clap, pad, bass, zap, glitch, …) and then builds that instrument.

**What the stock does** (`0x0814476C`, decoded): for slots 0, 2–4, 6–12
(it skips both DETUNEs) it draws the base and locals 1–5 — X-Mod amount and
rate, X-Env attack, decay and amount — and for 13–16 the base, each value
independent and skewed towards the top (`exp(-8 (u-0.5)^2)`, so nothing
below 13 %).  Waveforms, X-Mod types, X-Env routing, OutVol and Sync are
never touched and the mod note is not cleared, so whatever it held before
survives underneath.  Seventy unrelated knobs: every result is a detuned,
half-modulated buzz.

**R1** puts a branch over the stock entry (`0x0814476C` → `np_modnote_random`,
`patch/np_mnrand.cpp`, header +0x1B8).  Its one caller, the thunk at
`0x0811EC6C`, and the contract — take the event, delete it — stay.  It:

1. picks an instrument by weight — kick 12, snare 9, clap 6, closed hat 9,
   open hat 5, tom 5, perc 7 (cowbell / rim / blip / wood), bass 11, acid 6,
   lead 5, pad 5, pluck 6, FM bell 5, zap 5, stab 4, glitch 6;
2. starts from the mod-note reset (`0x0811702C`: bases from the defaults
   table `0x0815E8A0`, locals 1–5 0x8000, 6–8 0) and lets the recipe set
   what makes that instrument, each value drawn from a musically chosen
   range;
3. dispatches `ClearModNoteData` for the note, then one `ModNoteParamData`
   per parameter that differs from the reset — synchronously through
   `0x081576D4` with the request's +4 word, exactly as the stock tags its
   own (11–30 events instead of 70).

The seed is the stock RNG (`0x08147984`) xor `CYCCNT` xor a counter.

### The units the recipes are written in (all measured in sysemu)

| parameter | raw → unit |
|---|---|
| VCO tune | `(57 + st) · 256`: st semitones from the played note (a mod-note key plays C4 = 60) |
| DETUNE | ±1 st around 0x7FFF |
| cutoff | `c = raw/256 + (note − 57)` with FOLLOW, c in semitones, 81 = 440 Hz |
| ENV AMOUNT, X-Mod amount, X-Env amount | bipolar around 0x8000, **cubic**: depth = full · x³, full = 126 st on a tune, 149 on the cutoff |
| main ADSR | decay/release peak→10 %: 0x2000 6.8 ms, 0x4000 32, 0x6000 105, 0x8000 287, 0xA000 695, 0xC000 1546 (release floor 8 ms); sustain linear |
| X-Env **local 2 = decay** | peak→10 %: 0x2000 5.7 ms, 0x4000 26.7, 0x6000 88, 0x8000 239, 0x9000 377 |
| X-Env **local 3 = attack** | to peak: 0x2000 10.5 ms, 0x4000 50, 0x6000 164, 0x8000 449, 0xA000 1087 |
| X-Mod free LFO | `0.01 · 1000^(raw/65536)` Hz — 100 s to 100 ms (manual: 40 s) |
| X-Mod audio rate | `(raw/65536 − 0.5) · 127` st from VCO1's live pitch |
| X-Mod ratio | detent `d` = raw `d · 512 + 256` |

Locals 2 and 3 are the other way round from what earlier notes in this
project assumed (attack first): measured, local 3 moves the time to the
peak and local 2 the fall after it.

### What each instrument is

* **kick** — VCO1 sine at −31..−24 st (44–65 Hz), X-Env on its pitch,
  instant attack, +18..+44 st falling in 20–110 ms; 40 % a click (VCO2
  square/saw high, its level pinged 3–12 ms); open low-pass; AD 180–700 ms,
  30 % a long 808 (0.7–1.6 s); 20 % driven.
* **snare** — sine/triangle body at −12..−5 st with a small pitch drop, 60 %
  of the time a body shorter than the noise (VCO1 level pinged); noise =
  VCO2 high with an **S&H X-Mod at audio rate** on its pitch (Mono has no
  noise source; this is the nearest thing), optional TZFM grit; band-pass
  into low-pass or towards high-pass; AD 110–280 ms.
* **clap** — the snare's noise, band-passed around 1–3 kHz, and a square at
  ~110 Hz on the output volume whose depth an X-Env routed to the X-Mod
  amount (FUNC+TYPE+WAVE) carries away in 25–45 ms: the hand claps, then
  the tail.
* **hats** — two squares at inharmonic pitches through heavy TZFM (+ S&H
  noise half the time), high-pass colour, cutoff 3.9–11 kHz; closed decays
  22–70 ms, open 200–600 ms.
* **tom** — a kick an octave up with a gentler drop, sometimes a fifth or
  octave of body.  **perc** — cowbell (two squares ~1.48 apart, band-pass),
  rim/clave, blip (pitch up or down), wood block (a ringing resonance).
* **bass** — saw/square at C2 or C3, alone, with a sub, a detuned unison or a
  fifth; low-pass 52–80 with a positive envelope; sometimes an FM pluck
  (pinged TZFM) or a sync sweep.  **acid** — one saw or square, cutoff
  42–66, resonance high, a big envelope sweep, short decay, little sustain.
* **lead** — two oscillators (unison detuned, octave, fifth), sustain
  0.55–0.9, half the time a delayed vibrato (an X-Env routed to the X-Mod
  amount fades a 4.5–6.5 Hz sine in).  **pad** — soft waves, detuned, attack
  0.25–1.5 s, release 0.9–3.5 s, a slow LFO on the cutoff, sometimes the
  manual's evolving phase (a slow LFO on DETUNE) and a waveform shimmer.
  **pluck** — enveloped low-pass, no sustain.  **stab** — two saws an
  interval apart (minor/major third, fourth, fifth, sixth, seventh, octave).
* **bell** — the manual's pinged FM: VCO2 a sine carrier, VCO1 the
  modulator a slightly detuned interval away (octave, twelfth, two octaves,
  ~5:1, …), TZFM pinged by an X-Env decaying 0.3–1.5 s, long AD.
* **zap** — a high sine/square with a 24–60 st pitch drop (30 % a dip that
  swoops back), sometimes through a resonant sweep.
* **glitch** — the stock's idea, bounded: a short sound with two or three
  destinations (pitches, cutoff, TZFM, waveform, volume, resonance)
  modulated by S&H / square / saw at synced, audio or ratio rate, and never
  a high-pass with the cutoff at the top (silence).

**p39 adds seventeen**, using what the first sixteen left alone — the
synced LFO (ten divisions, one per 10 % of the rate knob, `np_lfo.cpp`
`kDivSelect`; measured: they run from the tempo with the sequencer stopped
too, 1/8 at 120 BPM = 4.0 Hz), the ratio X-Mod type, filter FM, the
self-oscillating filter, hard sync, the all-pass colour, long X-Env attacks
and a gate on the volume:

* **cymbal** — the hats' metal with a long AD: ride (some ping kept, 0.7–1.6
  s) or crash (all wash, 1.4–3 s).  **shaker** — high-passed noise with a
  5–15 ms swell.  **drop** — the filter as the oscillator: resonance
  0xF400–0xFFFF, a little VCO1 to excite it, an X-Env on the cutoff that
  starts below and rises (water drop) or pings and falls.
* **FM voices** put the carrier on VCO2 (TZFM modulates VCO2 by VCO1, so that
  is Mono's FM layout), silence VCO1 (its default level is up), and put a
  ratio X-Mod on VCO2's pitch whose depth an X-Env routed to the X-Mod amount
  carries — a DX operator with an envelope on its index.  Measured: ±11.7 st
  at the start decaying to ±0.2 by 150 ms for a 12 st setting.
  **fm-bass** — carrier two octaves (or one) down, modulator 1–4× the
  carrier, index falling in 60–250 ms, sometimes a little that stays.
  **fm-ep** — 1:1 TZFM pinged for the bark plus a 14:1 tine on the pitch,
  sometimes tremolo.  **mallet** — marimba (4:1, 10–35 ms), kalimba (an
  inharmonic 4.24–6.93), vibraphone/glockenspiel (3.5:1 or √2, tremolo).
* **reese** — two saws 10–30 cents apart driving the filter, a 0.1–0.6 Hz
  LFO on VCO2's DETUNE.  **wobble** — a synced sine/triangle/saw on the
  cutoff (1/4, 1/4T, 1/8, 1/8T, 1/16), resonant low-pass.  **growl** —
  filter FM: an audio-rate X-Mod on the cutoff (unison, octave or fifth of
  VCO1) whose depth an X-Env swells over 40–200 ms.
* **sync-lead** — VCO2 synced to a quiet VCO1, swept by an X-Env or an LFO.
  **hoover** — detuned saws scooping 3–7 st up into the note (the same X-Env
  on both pitches), a 3–7 Hz LFO on VCO1's waveform between square and saw.
  **phaser-pad** — the colour at the all-pass point with resonance, a slow
  LFO on the cutoff sweeping the notches.  **gate** — a sustained two-saw
  chord chopped by a synced square on the volume (1/16, 1/8, 1/8T).
* **bleeps** — a synced S&H on the pitch (and half the time on a resonant
  cutoff): a new random note every 1/16, 1/8 or 1/32.  **riser** — noise and
  a saw swept up by X-Env attacks of 1–4 s (an X-Env always completes its
  attack, whatever the gate does), or a downlifter falling over 1–3.5 s.
  **siren** — a 0.3–2 Hz sweep of 4–12 st, or a UFO warble with TZFM
  shimmer.  **drone** — low, sustained, slow LFOs on TZFM, VCO2's waveform
  and the cutoff at unrelated rates.

The catalogue is a table (`kCatalogue`: recipe, weight); an instrument's
number is its row, so new rows go at the end.  Weights sum to 160: drums
about 40 %, basses 20 %.

Everything is relative to the note the mod note is played at; the recipes
assume the C4 a mod-note key plays.

`D_MNRAND_N` (`0xFE8`) counts randomizations, `D_MNRAND_LAST` (`0xFEC`) is
`instrument | note << 8 | events << 16`, and `D_MNRAND_FORCE` (`0xFF0`) is a
test hook (1 + instrument; 0 = random; cleared at boot).
`--no-smart-random` builds the stock handler.

`tests/test_modnote_random.py`: the chord (TEMPO = key 25, FUNC = 26)
randomizes the key's mod note; every instrument, forced, twice (on two
mod notes, so the switch fade starts each from silence — a mod note
retriggered continues from its own tail, as the stock does): the
records are exactly what the handler counted, none is a reset value, no
other mod note is touched, and the note played behaves like the instrument
(see the test for the per-instrument checks); 240 free draws hit every
instrument within 0.3–2.5× of its weight and the record list does not grow;
the stock handler written back writes 70 knobs and no waveform/type/routing.

---

## CH: an instrument switch fades the sounding one out first (p37 — 2026-10-07, for A/B)

After N1/N2: the amp ADSR and the X-Envs continue from
the level they are at (stock), so a hat after a kick with a long tail starts
at the kick's level (VCA 0.99, no attack from silence) while after another
hat it rises from 0 — the same hat sounds different by what preceded it.
p26 zeroed the level at the trigger and clicked (a step from a ringing tail,
and an attack squeezed because the stock's ramp kept its residual; reverted
in p28).

This one holds the note back instead.  When a note-on switches instrument —
a different mod note, or mod note ↔ plain note, per layer — while the amp
envelope is above 1 % and no note is held (a legato note does not retrigger):

1. `np_cnote_entry` (in place of `bl 0x081170E8` at `0x08117AD0`; the
   decision is `np_choke_note_on`, np_runtime.cpp) takes the event;
2. the DSP (`np_env_main`) publishes the amp level at the fade's start
   falling linearly to 0 over `np_choke_samples` (blob +0x1B4, from
   `--choke-ms`, default 1 ms = 32 samples) — the envelope runs on underneath
   — and on the last sample `np_choke_reset_envs` puts the amp ADSR and the
   14 X-Envs (slots 0–12 and 17; 13–16 and 18 have none) at rest at 0: stage
   idle, the stock's level `+0x40` 0 — so the stock trigger, which starts its
   attack from `+0x40` (stored at `+0x20`), runs its full course — and the RC
   entries' level and slope 0;
3. `np_main_service` then calls the stock handler with the held event: its
   mod note, parameters (N1-snapped), attack from silence.

Any other note-on, and every note-off (`np_noteoff_entry` over the prologue
of `0x08117594`, six callers), first lets a held note-on go
(`np_choke_flush`: fade stopped, envelopes reset with interrupts masked,
stock handler called).  Same instrument again, a note after silence, or
legato: untouched.  Counters `D_CHOKES` (`0xFE4`); state `0xFC8..0xFE0`.

`tests/test_modnote_switch.py` section 4 (7 checks): kick → hat fades
(largest downward step 0.031 = 0.99/32), reaches 0, and the hat's attack and
decay then equal the hat after silence to 0.0002; it costs 16 samples of
onset in emulation (the fade overlaps the note's own processing); the same
instrument again does not fade; a note released inside its own (stretched)
fade is applied, then released, nothing held.  Sequenced (kick on step 2,
hat on 3): one fade, the hat from silence, nothing left pending.

`--choke-ms 0` builds without it (both call sites stock).

---

## N1, N2: mod notes switch instantly (p36 — 2026-10-06)

A kick mod note with the filter closed, then a hat mod note with it open:
the hat should start open and crisp, not re-tune
from closed to open; the same for every parameter and envelope.

**What the stock does.** Every one of the 19 CV destinations passes its base
value (the parameter, its follow and lock terms — X-Env and X-Mod are added
after) through a one-pole TPT lowpass, `y = (g·x + s)/(1+g)`, `s' = 2y − s`,
with `g = tan(π·100/32000) = 0.009817` at `owner+0x1D8` (state `+0x1DC`,
input `+0x1E8`, output `+0x1EC`; `g` written once, by the base constructor
`0x0811CF28`): a 100 Hz de-zipper, τ 1.6 ms, 90 % after ~3.7 ms.  Right for
knobs; wrong for a mod note, which switches a whole instrument at a note-on.
Measured on sysemu, kick → hat: the cutoff 38–47 % of the way after 1 ms,
VCO1 pitch from the kick's, resonance, volumes, wave, FM the same.  The
mod note's parameters themselves arrive before its trigger and without a
ramp (`CNoteData` handler `0x081170E8`: replay `0x081209F4`, then the note
start `0x08116CF4` → `0x0811612C`; `setValue` with 0 ramp steps); the delay
is all in the smoother.  *Correction:* the 2026-09-18 note "no slew on that
path" looked at the CParam ramps and missed this.

**A regression of mine, fixed here.** Since p19 the slots evaluated once per
block (`np_slot_hold`: VCO fine/wave/vol, TZFM, reso, color, filter env,
out vol) ran the smoother once per block with the per-sample `g` — a
12.5 Hz lowpass: ~30 ms to 90 % instead of ~4 (measured: resonance 28 %
after 4 ms against the stock's 92 %).  `np_slot_hold` now substitutes
`gN = (1 − aᴺ)/(1 + aᴺ)`, `a = (1 − g)/(1 + g)` for the call — one step
decays the state exactly as N per-sample steps — and restores `g`.

**N1.** The tail call `b.w 0x0811612C` at `0x08116D30` (the note start every
note-on path reaches; a legato or slid note goes through `0x08116D48` and
keeps its glide) goes to `np_note_start`, which asks the DSP for eight
samples with the smoothers bypassed — before the stock's 171-parameter
evaluation (an audio block can land inside its ~70 µs) and again after it.
`bl 0x08112488` at `0x0811503C` (the per-sample DSP call) goes to
`np_dsp_sample`: with a snap pending it sets every smoother's `g` that holds
the stock value to 1e9 (output = input on that sample), runs the sample,
puts each state on its input, and after the eighth gives `g` back; else one
load and a tail call.  Eight samples cover the block boundary at which a held
slot evaluates.  Counters `D_SNAP` (`0xFBC`), `D_SNAP_G` (`0xFC0`),
`D_SNAPS` (`0xFC4`).  `--no-note-snap`.  Consequence for ordinary playing:
a new note's pitch (and keytracked cutoff) jumps instead of the stock's
~4 ms micro-glide; a knob moved between notes is still smoothed.

**N2.** `0x081209F4` allocated a `ControllerEvent<CPatternParamData>` per
stored parameter, dispatched it through the controller's type registry
(type-name hashing) and freed it — only for the handler (`0x08117DFC`, type 7
at `0x08117E94`) to store the bank value at `project+0x4268 + layer·0x180C +
(slot·9 + local)·4` and re-evaluate that one parameter (`0x08116D9C`),
unless `project+0x6034+layer` is set.  Per parameter ~90 instructions in
`new`, ~510 in the dispatch, ~70 in `delete` around ~270 of work.
`np_modnote_replay` does the handler's two things directly.  A full mod note
(108 parameters): note-on handler 183k → 78k main instructions, the sound
~0.26 ms sooner.  `--no-fast-replay`.

`tests/test_modnote_switch.py` (10 checks): kick → hat, every smoother's
output equals its input from the first sample after the note start, cutoff
18 → 149 on that sample; with N1 written back the stock slide is there and
the held resonance keeps the inline cutoff's pace (54/48 % at 1 ms, 76/72 %
at 2 ms — p19..p35 had ~15 % against 72 %); N2 gives the same bank tables
and 171 parameter values as the stock replay.

Measured, **not changed** (they change how a patch sounds, not just when):
* The amp ADSR and the X-Envs continue from the previous note's level
  (stock, analogue-style; p26's reset to zero was reverted in p28 for its
  click).  A hat after a kick with a long tail starts at the kick's level
  (VCA 0.99) — no attack from silence — while after another hat it rises
  from 0 in ~0.75 ms: the hat sounds different depending on what preceded
  it.  Envelope *times* do not leak: decay rates match either way.
* X-Mod LFO phase is never reset by a note (free-running): an LFO- or
  audio-rate-FM-based drum differs hit to hit.
* The note start re-evaluates all 171 parameters (~35k instructions,
  ~90 µs) on every note.

---

## Q1: a paste during a save froze the unit (p35 — 2026-10-03)

The project store has two access styles.  **FUNC+RECORD** (`0x0815169E` →
`0x081263C0` → `0x081262FC`) serialises the project into `0x24000000` and
queues a C++20 coroutine on the QSPI driver's operation queue (driver 8,
`std::deque` of 8-byte handles at `+8+0x48`), resumed from main at
`0x08119A0E` (`0x0810E2C4`; a coroutine that yields an error string makes
main `bkpt` at `0x08119A14`).  The erase coroutine (`0x0810C894`) borrows
the driver's read-modify-write buffer (`Borrowable`, flag `+8+0x44`) and
holds it across `HAL_QSPI_AutoPolling_IT`; pages are then programmed.
**Load** (FUNC+DELETE, SEQ) is queued the same way (`0x0811F8DC`).

Five project events go to the flash **synchronously** from the project
service instead (`0x081262F4` = sync save of the current slot,
`0x08124440` = sync 512 KB read + replay): `CopyPastePatchParamData`,
`CopyPastePatchData`, `CopyPastePatternData`, `CopyPasteModNoteData` when
the clipboard came from another project (save this one, read that one,
read this one back), and `CopyPasteProjectData` always (two saves).  One of
them arriving while a queued save or load is still in flight finds the
erase buffer borrowed — `"Borrowable already borrowed"` (`0x0810ED76`),
`abort()`, `_exit` = `b .` (`0x0815A6E0`) in main — or the HAL busy
(`HAL_QSPI_Command` returns `HAL_BUSY` when `State != READY`) and goes to
`Error_Handler`'s `bkpt`.  Stock: the panel freezes for good (no watchdog);
with A2: a watchdog reboot 16 s later, PC ring full of `0x0815A6E0`.  The
window is the save's duration: 45 ms–0.3 s per 4 KB sector, 0.3–1.5 s per
64 KB block, 0.2–1 ms per 256-byte page (IS25LP128 datasheet), i.e. up to
seconds for a large project.

**Fix:** `0x08157672`, the head of the project service loop (`0x08157650`,
also entered by `np_project_first`), branches to `np_project_gate`, which
asks `np_qspi_defer` (np_runtime.cpp) whether the event at the head is one
of the five and the QSPI queue is non-empty; if so the node stays at the
head and the pass ends at the loop's own exit.  main runs the queue at the
top of its next pass, so the paste is applied, in order, once the save or
load has finished.  Counter `D_QSPI_DEFER` (`0xFA4`).  `--no-qspi-defer`
restores the stock.

`tests/test_qspi_defer.py`, sysemu with the new asynchronous QSPI model
(`Mono.enable_qspi_async`: MDMA transfers and the QUADSPI status-match/TC
interrupts completed after a delay; the plain model never raised them, so a
save never finished in emulation), the erase stretched to 50 ms:
stock FUNC+RECORD then a project paste → `abort()`; this image → the paste
is held back, runs after the save, the destination slot holds the project;
idle queue → not held back; stock loop head written back → the hang
returns.  The QSPI model also learnt ISSI's `0xD7` sector erase (the driver
uses it; the model used to program over unerased data).

What the sync paths still cost (not changed): main — and with it the
sequencer, MIDI input and the panel — is blocked for the erase + program of
the save (a small project: one 4 KB sector + 17 pages, measured on sysemu;
45–320 ms per save on the datasheet) and ~8.2 M instructions (~20 ms) per
512 KB read + replay of a small project.  A project paste is two saves.

## MF1–MF5: MIDI flow and poly pressure (p35 — 2026-10-03)

**MF1/MF2 — the output fan-out.**  `0x0811A904` (main) drains the
256-event output ring (`midi+0x408`, tail `+0x808`, head `+0x80C`) to USB
and to the DIN byte ring (`+0xA18`, write `+0xC1C`, USART read `+0xC18`).
It advances the tail before the USB append (`0x081101DC` → `0x08108D54`),
whose staging buffer (`0x200005C0`, length `0x20000704`) holds 64 packets
between the 1 kHz flushes; the append's `2` ("full") is ignored, so every
packet past 64 in a burst is lost.  The DIN copy goes byte by byte and
resolves a full ring by dropping its *oldest* byte — the middle of a message
already queued; the receiver then reads the rest under the wrong status
(running status), e.g. a note-off's key as a CC.  The telemetry alone sends
6 bytes/ms against DIN's 3.125.  The main loop's `bl 0x0811A904`
(`0x08119A2C`) now calls `np_midi_fanout` (np_midi.cpp), each port waiting
only for itself: with USB up an event is taken when USB staging has room
(else it waits in the output ring for the next pass) and its DIN copy is
made whole or skipped — DIN at 3125 bytes/s must not throttle USB, nor a
stalled USART stop it; with USB down DIN is the only port and the event
waits for DIN room.  A first version gated both ports in the stock loops
and coupled USB to DIN's speed; it was replaced before release.  The rest
is the stock's: the MIDI-activity LED timeouts, the `+0xD18` gate (SysEx
dump), the packet built by `0x081101DC`, DIN bytes per status (1 for F0–FF,
2 for C0/D0, 3 otherwise).  An output ring that overflows meanwhile drops
whole events, oldest first, by its producers' own rule.  Counters
`D_MIDI_USB_HELD` (`0xFA8`), `D_MIDI_DIN_HELD` (`0xFAC`, USB down),
`D_MIDI_DIN_DROP` (`0xFB8`, messages DIN skipped with USB up).

**MF3/MF4 — the receive rings.**  The DIN receive ring (`midi+0x810`, head
`+0xA14` written by the USART IRQ, tail `+0xA10` by the parser
`0x0811A7A0` in main) and the USB-MIDI receive ring (`0x20003260`, 512
words, head `0x20003A64` from the OTG IRQ, tail `0x20003A60` by
`0x081102B4` in main) both resolved "full" in the interrupt by advancing
the **reader's** tail.  The reader keeps its tail in a register and writes
it back, so an overflow inside that window is undone and the ring reads as
empty: up to 511 entries lost at once, note-offs among them.  It needs main
to fall a full ring behind (a long main stall — a sync paste — with dense
input).  Now the producer owns only its head and drops the newest entry
(`din_rx_put` inside `np_midi_rx`, with J; `np_usb_rx_put` at `0x08108D4A`).
Counters `D_MIDI_RX_DROP` (`0xFB0`), `D_USB_RX_DROP` (`0xFB4`).

**MF5 — poly pressure.**  The input dispatcher `0x0811B700` sends `A0 kk vv`
down the `D0` path with the first data byte as the pressure — the key.
Aftertouch on the synth's channel followed the note number; on another
channel the message was forwarded as channel pressure = key.  `0x0811B754`
(`beq; bhi` after `cmp r0,#0xA0`) becomes `bhs.w np_polyat`, which moves
`vv` into place for A0 and keeps the B0 branch.  `--no-polyat-fix`.

`--no-midi-flow` restores MF1/MF2/MF4 (MF3 lives in J's handler).
`tests/test_midi_flow.py` (25 checks), the stock fan-out against
`np_midi_fanout` and the receive handlers on the bare rig: a 100-CC burst
reaches USB complete across flushes (stock: 64 + 36 dropped); a 251-event
mixed stream through a 3-byte-per-pass USART — with USB up, USB gets every
event and DIN whole messages only, the skipped ones counted (stock: DIN cut
mid-message); with USB down, DIN byte-identical; full receive rings keep the
reader's index; poly pressure through the real dispatcher on sysemu reads
100 (stock 60) and forwards 90 (stock 61).  sysemu now drains DIN at the
wire rate (`Mono.usart_tx`, TXE every 320 µs while TXEIE is set) and logs
the bytes in `din_bytes`; before, the DIN ring never emptied in emulation.

Seen while testing, **not changed**: the synth's own pressure/CC74 echo to
MIDI out truncates (`D0 77` in → `D0 76` out, `0x08117C60`), and CC74's echo
is rate-limited to one per 25 ms per controller with no trailing send
(`0x0811B450`), so the last value of a fast move can be missing.

---

## V: the dim SEQ-key state, dimmer (p34 — 2026-10-02)

The SEQ keys' two states, dim and bright, look almost the same on the
unit.  The renderers request 16 for the bright state (a note step, a step
holding a lock of the selected parameter) and 24 for the current pattern,
and **5** for every dim one — a long note's tail (`0x0814EFF8`), a note step
in the parameter views (`0x0814F1B4`, `0x0814F25C`, `0x0814F304`), a pattern
with content in pattern mode (`0x0814F09C`).  The HT16D35A's 64 PWM levels
are linear and the eye is roughly logarithmic, so 5/63 next to 16/63 reads
as nearly equal.  The dim level is one immediate per renderer
(`0x0814F05E`, `…F0CE`, `…F1E4`, `…F28C`, `…F33C`); `--seq-dim N` sets it
(default **2**, the mini keyboard's own dim level; stock 5).  Pattern mode's
rarer class (`2 × bit`, a pattern flagged in `mgr+0x734` but not
`mgr+0x73C`) becomes 1 by turning `lsls r4,#1` at `0x0814F0F0` into a move,
so it stays below the dim level.  `tests/test_seq_led.py`: same session on
the stock and on this image — bright levels unchanged, dim 5 → 2.

The X-Env event log (EV/PM) is **off by default** from p34 (`--ev-log`
builds it); TR, P6 and the boot record are unchanged.

---

## T1: the morph pattern's unlinked X-Env trigs fired at morph 0 (p33 — 2026-10-02)

**Found with the EV log on the unit** (`capture-20261002-214940.txt`): on a
clean pattern 9 with one note, 220 TRIG-from-PARAM events on the cutoff
against 27 notes, every one on a step of pattern 1, whose map (PM) showed
32 steps of notes, locks and unlinked cutoff trigs.  Pattern 1 is the
**morphing pattern** — layer 1, [FUNC]+[PATTERN], pattern 1 unless chosen —
and the morph knob ([FUNC]+[CUTOFF], project+0x604C) was at 0.

The stock gates the morph track's notes (`0x08117A44`: a track-1 note is
dropped below 0.02, a track-0 note above 0.98) and the note path's X-Env
trigger (`0x0811612C`: morph-track unlinked trigs count only above 0.01),
but not the trigs themselves: the step processor `0x08146694` ticks every
pending event of both tracks and `SequencerEvent<ParamTrigData>`'s tick
(`0x0811DCB8`) fires regardless.  So the morph partner's unlinked trigs
played in every pattern, at their own pattern length, firing that slot's
X-Env and resetting its LFO — the "ghost rhythm" on the cutoff, and only
there because only the cutoff had trigs in pattern 1.  Not a patch
regression: the stock image does the same (`tests/test_morph_trig.py`).

**Fix:** `blx r2; b 0x08146766` at `0x08146804` (the generic per-tick call,
track index at `[sp,#4]`) → `np_morph_trig`: a ParamTrig tick on track 1 is
skipped while morph ≤ 0.01; the node's count and lifetime are untouched.
`tests/test_morph_trig.py` (8 checks): stock fires on 1/3/7/10 at morph 0;
patched only on the note; at morph 0.5 the morph pattern's trigs and notes
play as in the stock; restoring the stock call brings the ghosts back.
`--no-morph-trig-fix` builds the stock behaviour.

---

## EV/PM: the X-Env event log (diagnostic, p32 — 2026-10-02)

On the unit the cutoff X-Env fired on its note and a couple more times
per pattern on a fresh pattern with one note; sysemu does not reproduce it
(23 attack/decay combinations, long notes, telemetry looped back into MIDI
in, a trig pattern in the second layer).  So the unit reports it itself.

* **Stock triggers.**  `0x08156074` (`movs r3,#1; strb r3,[r0,#0x2f];
  strb r3,[r0,#0x2d]; bx lr`, r0 = owner + 0x1F0) is the only X-Env trigger;
  its callers are the note path (`0x0811612C`, returns to `0x08116210`), the
  param-trig / unlinked-X-Env path (`0x08117FE8`, returns to `0x08118022`)
  and the main ADSR (`0x0814770C`, r0 = `0x20004834`).  Its first two
  instructions branch to `np_ev_trig`, which logs the slot by caller and
  replays them, preserving every register the stock leaves alone.
* **RC restarts.**  `np_env_rc_step` logs a new attack and the p31
  decay-accumulator restart, by state entry (0 = main ADSR, slot + 1).
* **Log.**  `np_ev_put` / `np_ev_put_isr` append {uwTick, transport position
  (ticks since PLAY, 256 per sixteenth), type << 24 | bit} to a 128-entry
  ring at DIAG+0x1420, merging same-type same-millisecond entries (a note
  triggers a dozen X-Envs at once).  DIAG grows to 0x1C00; the stack was
  measured to reach no lower than 0x2001FC78.
* **Records.**  EV (tag 7, 52 words, CRC): up to 16 events, sent within
  20 ms.  PM (tag 8, 52 words, CRC, every 5 s): both tracks' 64 steps as
  note / automation / param trig / XoX / expressivity flags, the param trigs
  with their slot, the layer patterns and lengths.  TR, EV and PM now all
  wait for the first heartbeat decision, so a warm reset's boot record goes
  out first (TR used to precede it by a second).  `tools/diagmidi.py` prints
  the timeline with step numbers; `tests/test_evlog.py` checks it end to end
  on sysemu (15 checks).  `--no-ev-log` builds without it.

---

## Instruction cache

`np_early` invalidates ICIALLU and sets `SCB->CCR.IC`. Safe here because the
application never writes flash bank 2 — the bootloader owns that, and it runs
before the application.

The **data** cache is deliberately left off. Turning it on needs cache
maintenance around every DMA buffer in D2 SRAM, or MPU regions marking them
non-cacheable, and getting that wrong produces silent audio and MIDI
corruption rather than an obvious failure.

---

## Optional boot self test

```bash
python3 tools/patch.py build --crc
```

Computes a CRC32 over the 479704 stock bytes at boot (~30 ms) and records
both the measured and expected values in the diagnostic block. It only
records — it does not refuse to run — because the alternative, halting, would
turn a false positive into a brick, and because nothing on the device can
show the result anyway.

Off by default for that reason. The implementation is verified against
`zlib.crc32` over the full image under emulation.

---

## What is verified, and how

`make test-patch` runs `tests/test_patch.py`, which loads the real patched
image into Unicorn as a Cortex-M and checks:

* **np_early** — clears the block on a cold boot, keeps it on a warm one,
  bumps the boot counter, sets `CCR.IC`, writes ICIALLU
* **np_sbrk** — returns the previous break, advances it, refuses an
  oversized request with `-1` without moving the break, accepts an exact fit,
  handles negative increments, and never lowers the high-water mark
* **np_runtime / allocator** — unlocks and enables DWT, keeps constructors on
  the original heap, allocates/reuses both fixed pools, exercises all 15
  512-byte blocks, verifies exhaustion never calls malloc, and routes pooled
  and ordinary pointers through the correct delete path
* **profiles/defer** — checks all four IRQ-to-profile mappings, 64-bit totals,
  the audio deadline, PRIMASK restoration, USB coalescing and main-loop timing
* **np_wdg_hook** — arms the IWDG once with the configured prescaler and
  reload, kicks it on every later call, leaves the peripheral alone when the
  flag is clear
* **fault handlers** — record the fault id, counter, CFSR, HFSR and basic or
  extended FPU frame, preserve pool-exhaustion cause 5, skip a frame outside
  DTCM, and write `0x05FA0004` to AIRCR
* **MIDI** — TXE kick/drain ordering, 512-byte ring wrap, bounded one-byte IRQ
  work, and RX framing/noise/overrun handling
* **np_mod_tick / MIDI F8** — recomputes the rate from the transport, clamps
  a parameter value of 1.0 to the last table entry, and gates retrigger on
  exact rational boundaries for all 15 divisions; the harness also executes
  the real `0x08118B92 -> 0x08116CD4` F8 call site
* **np_lfo_poll** — refreshes exactly the synced slots when the transport
  rate changes and does nothing at all when it has not
* **np_crc32** — matches `zlib.crc32` over the whole image
* **wiring** — runs the patched `Reset_Handler` itself and the patched
  main-loop call site, so the rewritten branches are what gets exercised

What emulation cannot tell you: whether 16 s is enough, whether the
instruction cache changes timing anywhere it matters, and whether the LSI
oscillator behaves as assumed. Those need the device.
