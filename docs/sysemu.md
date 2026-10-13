# Whole-firmware emulation — 2026-09-15

**Later audit:** [ui-amount-audit.md](ui-amount-audit.md) reproduces a real
UI deque preemption race absent from serial IRQ scenarios. It also records
the limited ADC range of this model and why instruction counts alone do
not exclude hardware overruns. A separate full-range ratio/Amount probe
passes; that does not rule out the reproduced race or a real project.

`tests/sysemu.py` boots the real image under Unicorn from `Reset_Handler`
through the static constructors and `main`, then runs the main loop in
slices with software-injected interrupts.  It exists because the mod-note
UI hang could not be reproduced by calling functions in isolation: it
needs the UI, the project manager, the sequencer, the key scanner and the
CV engine all running against each other.

## What is modelled

| piece | how |
|---|---|
| CPU | Unicorn Cortex-M7.  Its model has no double precision; `tests/fp64_shim.py` catches the invalid-instruction hook and executes the ~25 FP64 forms the image uses (vldr/vstr d, vmov.f64, arithmetic, vcvt, vcmp, vrint, vsel, vmaxnm) on the real D registers. 407 such instructions run during boot. |
| Interrupts | injected by the harness between main-loop slices: the handler is called with IPSR set to its vector and the interrupted registers snapshotted/restored around it. SysTick (4 kHz), DMA1_Stream0 (LED row + key-scan row), TIM5 (transport tick), EXTI2 (CV engine, 8 per SysTick period = 32 kHz). Nothing is delivered while PRIMASK is set. |
| Flash | image + 0xFF elsewhere. Bank 1 below 0x08040000 (bootloaders) and bank 2 below the settings sector 0x081E0000 are read-only (writes dropped); 0x08040000..0x080FFFFF (the project journal's flash copy) and 0x081E0000 (settings) are writable with RAM semantics. |
| RCC / PWR / ADC | ready bits mirror enable bits; ADC control bits self-clear, status reads "ready, conversion done"; the first pot-ADC frame flags the two boot waits at `0x0811510A` / `0x08115202`. |
| QUADSPI | indirect read/write/erase against a 16 MB array that also backs the memory-mapped window at `0x90000000`; JEDEC ID answers `9D 60 18` (IS25LP128), which the driver insists on. |
| Panel | `HAL_GPIO_ReadPin` (`0x081029C0`) answers from a `pressed` set only when called by the matrix scanner (return address `0x0810F620`). Matrix index == UI key id, 0..46. The 23 pots are `pot_values[0..22]` (16-bit samples, 0x8000 = mid-travel; an earlier revision masked them to 12 bits and so only ever exercised the top 6 % of a parameter's range): the scanner (`0x0810F0F4`, one row of three pots per DMA1_Stream0 tick, row table `0x0815E0D0`) reads them from its ADC frame buffer, keeps a 32-sample ring per pot, inverts, and raises a 12-byte pot event when the average moves more than 0x180 of 0xFFFF (≈ 0.6 % of travel) from the last reported value. Pots 0–17 select and set parameter slot 0–17 (`UI+0x92`), 18–22 are the modulation pots of the selected slot: X-Mod amount (local 1), X-Env attack/decay/amount (locals 2, 3, 4), X-Mod rate (local 5); TYPE/WAVE set locals 7/6. |
| LEDs | the LED driver is written by **SysTick** (`0x08109FD0`, driver 4): each tick advances one LED index mod 70 and, if that LED is dirty, sends `80 <led> <brightness>` over SPI4 (`0x40013420`). `led_rows` holds one such 3-byte update per tick; a small dict replay gives the LED state. UP/DOWN are LEDs 0x51/0x52 (9 = lit in mod-note mode), the mini-keyboard 0x33+, SEQ 0x19–0x28. |
| RNG | `RNG_DR` (`0x48021808`) returns seeded pseudo-random words: the main loop reseeds the xorshift state (driver 10 + 0x1C) from it every iteration and sample-and-hold draws on that state, so without it S&H is stuck at one value. |
| Stubs | `Error_Handler` (`bkpt; bx lr`) is recorded and skipped; the timer clock query `0x08104F00` returns 240 MHz; `HAL_Delay` is fed by advancing `uwTick` at its spin. |

Boot to the main loop takes ~3 s of host time; `test_modnote_ui.booted()`
caches the booted state (registers + RAM + QSPI) in `build/sysemu-boot-*.pkl`
so scenarios start instantly and can be rewound with `restore()`.

## What the boot taught us about the firmware

* **Project storage is an event journal.**  At boot 512 KB are read from
  the QSPI flash into AXI SRAM at `0x24000000`; from `+0x100` on it is a
  stream of `[event id][fixed-size payload]` records replayed through the
  event registry at `0x2000CF24` until `EOFData`.  The ids and payload
  sizes are in `build/event-ids.json` (e.g. 6 ToggleModNoteMode, 9 EOF,
  12 PatchParam 8 bytes, 13 ModNoteParam 5 bytes, 88 CopyPatchToModNote,
  254 SOF).  The SysEx "project" transfer (`0x0812471C`, USB driver 7)
  parses the same records.  A fresh unit needs `SOF, EOF`; 0xFF (erased
  flash) has no factory and the loader aborts; zeros never reach EOF and
  the writer runs off the end of RAM — the factory formats the store.
* The store is also written to **internal flash bank 1 from 0x08040000**
  (`0x08143378` erases and programs it after disabling the D-cache with
  the set/way loop — so the D-cache is on in the stock; the audit's
  "caches never enabled" was wrong about that) and the settings live in
  sector `0x081E0000`.
* **Key groups** (switch table `SWITCHES+key*12+9`): 0 = TEMPO, FUNC,
  TYPE, WAVE (keys 0–3); 3 = SEQ 1–16 (4–19); 4 = keys 20–23; 1 = the ten
  function keys 24–33; 2 = the 13-key mini keyboard 34–46.  Modifier bits
  (`0x08172B28`): key 24→bit 3, 25→2, 26→0, 27→1, 28..33→4..9, TYPE 10,
  WAVE 11, SEQ group 12, group 4 13, notes 14, FUNC 15, TEMPO 16.
* **UI action registrations** (97 of them, hooked at `0x081494F0`): each
  is (modifier mask, `0xFFGGkkee`, thunk, callback thunk, flags).  `ee` is
  the **UI mode** the action belongs to (1 normal, 2 mod-note, 3 = a
  selection page), `GG` the key group, `FF` = 0x00 press / 0x02 release.
  (An earlier revision of this file read 0x02 as "transport running" and
  called key 25 PLAY; both were wrong.)  Key 26 = FUNC, **27 = RUN** (the
  only key whose tap sets transport+0x6C), 28/29 = UP/DOWN, 32 = RECORD,
  33 = DELETE, 24/30/31 switch panel pages (UI+0x8B = 4/3/2), TYPE+WAVE = 5.
  Decoded table: `tests/probe_ui_chord.py` registrations + the thunk
  targets (`b.w` at 0x08147xxx).
* **Mod-note mode is UI+0x84 == 2**, toggled by `0x081517C8` on the release
  of UP or DOWN while the other is held; works with the transport stopped.
  In mode 2: note key press → `0x08152194` → `0x0814FE5C` (pushes a 12-byte
  held-key record on the list at UI+0x328, count UI+0x330, maps key→mod
  note in UI+0x30C.., builds a `ControllerEvent<CNoteData>` with +0x10 =
  mod note index, +0x11 = 1, velocity 0.5) and release → `0x081521B0`;
  with RECORD lit (UI+0xED) both also submit `RecSeqData<NoteData>`.
  Mod note index = white-key column (`0x08172C88[key-34]`) + 8 × page
  (UI+0xE9); key 36 is mod note 1 + 8·page.  **RECORD + note key** submits
  `CopyPatchToModNoteData` (0x081522BC) — that is the only patch-to-mod-note
  copy in this firmware; selecting an empty slot does *not* copy (the manual
  sentence is from an older version).  **DELETE + note** = `ClearModNoteData`,
  DOWN + note = mute, UP+DOWN held + note = semitone.  WAVE/TYPE in mode 2
  cycle the selected mod note's slot waveform/type through
  `ModNoteParamData` (0x08150540: note, slot UI+0x92, local, raw value).
* **Mod-note storage.**  The project manager (`0x2000C68C`) keeps one linked
  list at +0x1BC of 0x18-byte records `[next][bank u8][type u8][slot u8]
  [local u8][value u16]…`: type != 7 are the pattern patch's parameter
  values (bank = layer 0/1), type 7 are mod-note parameters keyed by mod
  note in the bank byte.  Playing a mod note (`0x081209F4`, project side,
  from the CNoteData handler `0x081170E8` after the mute check
  `0x08120A84`) resets the bank table at project+0x4268/+0x5A74
  (`0x0811702C`: 19 slots × 9 words, locals 1–5 = 32768.0 "unset") and
  replays every type-7 record of that note as a 12-byte
  `ControllerEvent` (bank, 7, slot|local, float(raw)).  The next normal
  note replays the type≠7 records (`0x0811FD48`).  Raw values are the UI's
  integers (waveform 4 = S&H); a waveform of 5 stored by an older build is
  rejected by the CParam and leaves the previous shape.
* **Audio frame.**  EXTI2 fires at **4 kHz** and processes a block of
  **8 samples** (`0x08112488` and every slot evaluator run 8× per frame):
  the per-frame budget is 100,000 cycles, not 12,500.  Instruction counts
  per frame in the booted system: stock 18–21k (36k with all 19 slots on
  audio-rate S&H at the top of the knob), patched 28–35k (48k in that
  worst case) — the patched image is 30–50 % of the budget at one
  instruction per cycle, so audio-frame overrun is not a candidate for the
  UI symptoms.
* `CParam::setValue(param, ramp, value)` values for LFO waveform and
  type are `n / 65536` (S&H = 4/65536); knob parameters are 0..1.
* A stock `(33 held, 31 pressed)` gesture jumps to address 0 from
  `0x08157E2A` in the emulator — a null callback; not investigated.
* **An empty journal means an empty project: pattern length 0.**  With
  `SOF, EOF` alone both pattern layers have length 0 (`layer+0x60`, steps
  << 8), so the transport runs (TIM5 at 2048 Hz at 120 BPM, `0x08119270`
  per tick) but the step wheel (`0x08146694`, 128 buckets of 12 bytes per
  layer at pattern+0x8C / +0x6C8) never reaches a step and no note ever
  fires — and FUNC+UP/DOWN (rotate) walks its bucket index 255 → 0 through
  memory beyond the wheel and corrupts the pattern object, the UI object
  behind it and finally faults (an artefact of length 0, not a stock bug at
  length 16).  `Mono.factory_defaults()` submits
  `PatternParamData(page, param 0, 16)` for both layers (the same event the
  UI raises) and `booted()` applies it after every restore; steps are then
  programmed by holding a note key and tapping SEQ, and notes fire on RUN.
  Everything in the "what the emulator says" section below was re-run with
  a playing sequencer afterwards, with the same result.
* **Never snapshot or call firmware while the main thread has PRIMASK
  set.**  The stock malloc lock saves PRIMASK in one global word; a harness
  call into `operator new` from inside that region (or a restore that
  resumes there) makes the eventual unlock restore 1, after which no
  interrupt is ever delivered again — the main loop keeps iterating, the
  LEDs, keys and pots are dead.  That looked exactly like the reported
  hang once, and was the harness.  `snapshot()` and `call_preserving()`
  now step to a PRIMASK-clear boundary first (`run_unmasked`).  Snapshots
  hold registers by name, not raw Unicorn contexts (those carry host
  pointers and crash `uc_context_restore` across processes), and the boot
  cache is keyed by the image hash.  `TIM1->CNT`, which `0x081479A4` adds
  to the tick count as the microsecond, free-runs on the instruction count
  so time-budgeted loops end inside a slice.
* `TIM5` is injected at its programmed rate (`240 MHz / (PSC+1)(ARR+1)`,
  ARR from the firmware's reload variable `0x2000AFF0` when the register
  is still zero in a restored snapshot), EXTI2 once per SysTick (`audio=1`
  is the real rate: one 8-sample block per 250 µs).

## Scenario driver

`tests/test_modnote_ui.py` boots (or restores) an image and checks that
the main loop and LED output are alive.  The mod-note investigation
scripts (scratchpad `mn.py`, `userlike.py`, `fuzz.py`, `framecost.py`,
`worst.py`) drive the panel — RUN, UP+DOWN, RECORD+note copies,
DELETE+note clears, WAVE/TYPE cycling, real pot moves — with 32 kHz
audio, and print per step: transport, UI mode, selected slot/mod note,
modifier mask, held-key count, main-loop iterations, LED updates and the
UP/DOWN LED state, ISR allocations per vector, pool usage, waveform
bytes, record counts, patch-U key shadow, project events and UI
callbacks.  `fuzz.py` adds a check that every press/release the harness
made reached the switch handler `0x08155C7C`.

## Mod-note + S&H hang: what the emulator says (2026-09-15)

Stock and patched images behave identically, step for step, in every
flow tried: LFO-rate S&H, audio-rate S&H at the top of the rate knob
(noise), S&H copied into a mod note with RECORD+note, S&H set on the mod
note itself with WAVE, cleared slots, RECORD (step and live recording),
sequencer running with note trigs, 60 random gesture sequences.  No
stuck modifier mask, no stale key shadow, no lost switch event, no ISR
allocation beyond the scanner's key/pot events, no pool failure, LEDs
and mode toggles keep working.  The second report — normal mode, select
an oscillator, TYPE ×3 (the patch's fourth X-Mod type, ratio FM), turn
AMOUNT — was replayed on slots 0, 2, 3, 4, 8 with real knob moves, with
notes from the keyboard and from the sequencer, followed by RATE sweeps
and a mod-note round trip: same result, and a watch on every write into
the UI object, LED driver, switch manager, scanner and the patch's shadow
found no writer outside the stock UI/scanner code and the patch's own
fields.  Audio-frame cost in ratio mode is 30–36k instructions per 8-sample
block (budget 100k).  So the failing mechanism needs something the
emulator does not have: a real project's data (a Pgm Dump would replay
it exactly), or a peripheral that is not modelled (USB host, DIN MIDI,
real interrupt preemption, real cycle costs).  That is what the MIDI
telemetry in `docs/patches.md` is for: it reports the retained PC ring
after the watchdog reset that follows a dead main loop, and a heartbeat
with UI state while the loop is alive.

A follow-up LED audit checked the clarified symptom
(held C, live controls, frozen LEDs). It reproduces a lost LED dirty update,
shows that the sender ignores SPI status, and reproduces torn words in the
new MIDI telemetry. TXDR capture alone is not a wire-level SPI test, and the
current telemetry needs coherency fixes before diagnosing from its counters.
