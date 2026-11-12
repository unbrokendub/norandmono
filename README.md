# Norand Mono — patched firmware (unofficial)

A patch set for the **Norand Mono** firmware **V2.2.0r**. It fixes bugs in the
stock firmware, makes the engine lighter, and adds sound-design and workflow
features. The main ones are an instrument generator for mod notes, a fourth
X-Mod type for FM, and a 64-scale library.

This is an independent project, not affiliated with or endorsed by Norand.

**This repository does not contain Norand's firmware.** You build the patched
image yourself from the official V2.2.0r update file. The build applies byte
patches to that file and appends a blob of new code (C++ and ARM assembly).
The result is a normal SysEx update for the stock bootloader.

The unit's bootloader is never modified. A bad or unwanted image is undone by
flashing the official update the same way.

---

## Contents

- [What's new](#whats-new)
- [Bugs fixed](#bugs-fixed)
- [Using the new features](#using-the-new-features)
- [Building](#building)
- [Flashing](#flashing)
- [Testing](#testing)
- [Repository layout](#repository-layout)
- [Status and limitations](#status-and-limitations)

---

## What's new

### Sound and modulation

- **A fourth X-Mod type: RATIO.** TYPE now cycles free LFO → synced LFO →
  audio rate → **ratio**. In ratio mode the RATE knob picks a frequency
  ratio to the played note, so an FM timbre stays the same on every note.
  There are 128 detents:
  - the left half is harmonic, 0.25–16 in quarter steps;
  - the right half is the irrational Yamaha-style set, 0.71–17, for bells
    and metal.

  See [docs/ratio-table.md](docs/ratio-table.md).
- **Clean modulators.**
  - The modulator's sine was a parabola with 3.8 % THD. It is now a
    degree-7 polynomial, about 0.00005 % THD.
  - Saw, square and triangle are band-limited (polyBLEP/polyBLAMP), so
    audio-rate modulation and FM alias far less.
- **White noise from S&H.** In the top ~8 % of the RATE knob, an audio-rate
  S&H modulator draws a new value every sample, which is true white noise.
- **Analog-style envelopes.** The main ADSR and all X-Envelopes use one RC
  model: convex attack, exponential decay and release. Timing is unchanged.
- **Instant mod-note switching.** Switching to another mod note no longer
  glides every parameter over ~4 ms, so a hat after a closed-filter kick
  starts crisp. When a note changes instrument, the sounding one fades out
  in 1 ms and the new one starts from silence.
- **Synced LFOs.**
  - They follow tempo changes continuously.
  - A synced S&H steps on every division.
  - All 15 divisions, including quintuplets and 1/64, retrigger exactly
    under external MIDI clock.

### The mod-note instrument generator

In mod-note mode the stock TEMPO+FUNC+key randomized every knob
independently. The generator builds a real instrument instead, from 43
recipes: kicks, snares, hats, toms, percussion, basses, acid, FM bass, leads,
pads, plucks, bells, stabs, FX and more.

- **It follows the pattern.** It uses the pattern's scale and root and the
  mod note's own pitch.
- **Balanced output.** Levels are balanced between instruments, and
  envelope times are fitted to the current tempo.
- **No dead patches.** A patch that would be inaudible, such as a filter
  shut below the note, is drawn again.
- **Variety.** A "spice" layer adds 0–3 modulations to basses and tonal
  sounds: filter LFOs and envelopes, TZFM bites, drifting detune, vibrato,
  tremolo, drive, colour/resonance FM at the note, and slow timbre drift.
- **Pick the kind of sound** with the black keys: single keys and pairs
  select instrument groups (see the table below).
- **Kits.**
  - A drum kit over the page, sharing room, brightness, drive and the hats'
    metal.
  - A bass kit where each key is a scale degree with a different bass.
- **Whole sets.** All 40 mod notes at once, either any instruments or in one
  of five styles: techno, dub, acid, electro, ambient.
- **Outside mod-note mode,** the same gesture builds a random instrument into
  the pattern's patch.

The instrument list is in [docs/instruments.md](docs/instruments.md).

### Scales

- **A 64-scale library.** In the scale setting, hold PAGE A–D and press SEQ
  1–16 to load a scale into that custom slot. The library covers maqams and
  ragas, pentatonics, modes and jazz scales, symmetric and Messiaen modes,
  and chord tones. The load is saved with the project and can still be
  edited note by note. See [docs/scale-library.md](docs/scale-library.md).
- **Scale-aware automation randomizer.** TEMPO+FUNC + the OSC1 FREQ, OSC2
  FREQ or CUTOFF knob writes random automation. It is now quantized to the
  pattern's scale and root. Chromatic behaves as stock.

### Workflow

- **Project load in time.** Loading a project while the sequencer runs
  waits for the current pattern to end, then starts the new project from
  step 1, like a pattern change.
- **Dimmer "dim" SEQ LED state,** so dim and bright steps are easy to tell
  apart.
- **Levelled LEDs for a dark room.** Every lit LED is at the lowest
  brightness step. Only the playing step and PAGE A's tempo blink are
  brighter; TYPE and WAVE keep small steps, so the type and the wave still
  show. TEMPO+FOLLOW steps through brighter presets (dim, normal and bright
  then apart) and the stock levels, until power-off. `--led-level` sets
  the power-on preset.

---

## Bugs fixed

Stock V2.2.0r bugs, found by reading the binary and reproduced in a
whole-firmware emulator. Details and addresses are in
[docs/patches.md](docs/patches.md).

### Panel freezes

The panel went dark or stopped responding while audio kept playing:
- the key/pot event queue had no critical section, so a scanner push could
  corrupt the consumer's cursor;
- the pot scan was re-armed without checking the previous transfer, and
  nothing ever cleared the ADC overrun flag, so the knobs froze;
- the LED brightness setter flagged an LED dirty before storing its value;
- the LED writer could raise chip select inside an SPI frame.

### Other panel bugs

- Button chords could leave the UI stuck in a mode (e.g. TYPE+WAVE
  unlink).
- A pattern rotate (FUNC+UP/DOWN) whose range fell outside the step store
  ran past it and crashed. That is a rotate of a 0-step pattern, or a
  reversed range in a project's events. Such a rotate is now ignored. A
  unit's own projects are not expected to contain either.

### Storage and project bugs

- A save cut short by a power loss could brick the unit. A partly written
  project slot hung its load: the journal reader aborted on erased flash,
  before any of the stock's own checks. Slot 0 loads at every power-on, so
  the unit hung at every power-on. A slot like that now fails the stock's
  checks. At power-on the unit then starts with an empty project and leaves
  slot 0 as it is until you save. A later load of such a slot shows the
  stock's error code on SEQ 1–8.
- A paste during a save froze the unit.
- DELETE+RECORD (clear patch) also wiped a mod note's sound.
- The morph pattern's unlinked X-Env trigs fired even with the morph knob
  at 0.

### MIDI

- DIN out was throttled to 1 000 bytes/s. It now runs at wire speed.
- Bytes with framing or noise errors on DIN in were parsed as valid MIDI.
- USB out dropped every packet past 64 in a burst.
- DIN out could drop bytes in the middle of a message, so the receiver read
  the rest under the wrong status.
- The receive rings raced their producers.
- Polyphonic aftertouch was read with the key number as the pressure.

### Modulation and timing

- The synced LFO rate was never recomputed after a tempo change.
- A synced LFO's division index could read past its table.
- Quintuplet and 1/64 divisions retriggered on the wrong pulses under MIDI
  clock.
- A synced S&H never stepped.
- The main loop re-sorted every step of the pattern on every pass, which
  added step-timing jitter.
- The DSP evaluated all 19 modulation slots at 32 kHz. The audio interrupt
  dropped from ~193 µs to ~108 µs per block on the unit.

### Robustness

- Fault handlers looped forever. They now record the fault and reset.
- There was no watchdog. An IWDG now resets a hung unit after 16 s.
- `_sbrk` executed `bkpt` on a full heap instead of failing.
- Interrupts allocated from the locked heap. They now use fixed pools.
- The instruction cache was never enabled.

---

## Using the new features

### Mod-note mode

Press **UP+DOWN** to enter mod-note mode. UP/DOWN scroll the five pages of
eight mod notes.

| Gesture | Result |
|---|---|
| TEMPO + FUNC + white key | any instrument on that mod note |
| hold black key(s) + white key | an instrument from that group; plays at once, records no step |
| TEMPO + FUNC + C# | drum kit over the page (C kick, D snare, E clap, F/G hats, A tom, B perc/cowbell/rim/conga, C' cymbal or shaker) |
| TEMPO + FUNC + D# | bass kit over the page: keys are scale degrees, a different bass on each |
| TEMPO + FUNC + F# | every mod note's pitch back to C4; the sounds stay |
| TEMPO + FUNC + G# | all 40 mod notes in the style of the current page: 1 techno, 2 dub, 3 acid, 4 electro, 5 ambient |
| TEMPO + FUNC + A# | all 40 mod notes, any instruments |

Kits built after G# use its style. A# clears the style.

**Black-key groups:**

| Keys | Group |
|---|---|
| C# | kick |
| C# + D# | hats, clap, snare |
| D# | toms and other drums |
| F# | deep basses |
| D# + F# | FM bass only |
| F# + G# | acid, wobble, growl |
| G# | everything tonal |
| G# + A# | plucked and struck tonal |
| A# | zaps, bleeps, glitch |
| F# + A# | riser, siren, wind, crackle |

Any other combination picks the union of its keys' groups.

### Normal mode

**TEMPO + FUNC + key** builds an instrument into the pattern's patch. A black
key uses its group, and a second black key held with it uses the pair's
group.

### Scale setting

| Gesture | Result |
|---|---|
| SEQ 1–16 | built-in scale |
| PAGE A–D | custom scale 1–4 (per project) |
| hold PAGE + note key | toggle a note in that custom scale (stock) |
| **hold PAGE + SEQ 1–16** | load a library scale into that custom slot |

### X-Mod RATIO and FM

Use VCO2 as the carrier and silence VCO1. On VCO2 FREQ set the X-Mod type to
RATIO with a sine wave. **FUNC + TYPE + WAVE** routes the X-Env to the X-Mod
amount, which gives an FM index envelope. The 1:1 ratio is at ~3 % of the
knob, 2:1 at ~6 % and 4:1 at ~12 %.

---

## Building

### Requirements

- `arm-none-eabi-gcc` / `binutils`. 8.5 is known to work, and the image is
  built with `-Os`/`-O3`, so other versions may change its size.
- Python 3.
- For the tests: `unicorn` and `capstone`. The bench (`tools/bench`) also
  needs `numpy`. Install them with `pip install -r requirements.txt`.
- A C compiler for `tests/test_container.c`.

### Steps

1. Download the official **Mono firmware V2.2.0r** update from Norand and put
   it in the repository root as `mono_V2.2.0r.syx`.
2. For the bootloader checks, also add the official secondary bootloader as
   `mono_secondary_bootloader.syx`.
3. Build:

   ```bash
   make extract        # unpack the official update into build/firmware.bin
   make patch          # build/mono_V2.2.0r-patched.syx
   ```

The patch builder prints every rewritten site and the image size. The
secondary bootloader rejects images over **512 KiB**, and the build fails
before producing one.

Features can be switched off individually:

```bash
python3 tools/patch.py build --help
python3 tools/patch.py build --no-patch-random --no-sync-load
```

`--telemetry` builds a diagnostic image that streams the boot record, the
PC sampler and heartbeats as MIDI CCs on channel 16. Decode them with
`tools/diagmidi.py`.

---

## Flashing

1. Hold **TEMPO + FUNC** and power the unit on. It enters the bootloader.
   (The manual's PATTERN + RUN is wrong for this bootloader.)
2. Connect over **USB**; DIN does not work for updates.
3. Send the file. Progress shows on the SEQ LEDs, then the unit restarts.

   **Windows** works. Use a SysEx utility such as MIDI-OX with a delay
   between messages (1 ms or more).

   **macOS does not work yet.** On macOS 15 the stock bootloader stops
   part-way through the update. The bootloader's USB class has no Setup
   handler, and its USB stack calls that null pointer on any interface
   request or endpoint HALT request, which faults the CPU. In both attempts
   on a Mac, a control request to the unit was left pending at the moment
   it hung. Apple's own USB/MIDI services hold the device open, so the
   request most likely comes from macOS itself. Windows does not send
   these requests during a transfer.

   `tools/flash.py` is the macOS sender written for this. It paces the
   messages, watches the kernel's USB log and waits for the unit to
   restart. Its `--check` option validates a .syx on any OS and refuses
   anything the bootloader would choke on: a broken container, missing
   messages, or an image over 512 KiB.

   ```bash
   python3 tools/flash.py --check build/mono_V2.2.0r-patched.syx
   ```

Rules for any host:

- **Quit DAWs and editors first.** The bootloader garbles the update on a
  MIDI clock byte or a long SysEx from another app on the same port.
- **Don't burst.** More than about 380 bytes inside one millisecond
  overflows the bootloader's receive ring without any error.
- **If a send is interrupted or fails, power-cycle into the bootloader
  before sending again.** It keeps its write position until then.

[docs/protocol.md](docs/protocol.md#8-the-usb-receive-path) has the details.
Nothing is erased before the last message arrives. A send that stalls or
stops earlier leaves the current firmware in place. A garbled stream that
does complete erases the application, so the unit then needs a good file.
The bootloader is never touched, so TEMPO + FUNC at power-on always gets
back into it.

To go back to stock, flash the official `mono_V2.2.0r.syx` the same way.

Projects stay compatible with stock. The one exception is an X-Mod set to
the RATIO type: stock firmware has no such type, so that X-Mod is silent
there until its type is changed. A custom scale loaded from the library is an
ordinary custom scale.

---

## Testing

```bash
make test-patch-static   # image, branch targets, SysEx round trip (no emulator)
build/venv/bin/python tests/test_bootloader_accepts.py   # the bootloader takes the image
build/venv/bin/python tests/test_bootloader_usb_rx.py    # its USB receive path, paced and not
build/venv/bin/python tests/test_bootloader_usb_setup.py # the control requests that crash it
make test-flash          # tools/flash.py through CoreMIDI to a virtual port (macOS)
make test-patch          # every feature under the whole-firmware emulator (slow)
```

`tests/sysemu.py` boots the real image under Unicorn with modelled
peripherals: QSPI flash, LED driver, key/pot scanner, MIDI and the
sequencer timers. See [docs/sysemu.md](docs/sysemu.md). The sound tests
(`test_modnote_random.py`, `test_patch_random.py`) run for tens of minutes.

`tools/bench` renders what the firmware asks of the voice through a digital
stand-in for the analog circuit, for listening and loudness measurements.

---

## Repository layout

```
patch/            the injected code
  np_patch.S        header, vectors, fault handlers, watchdog, hooks
  np_lfo.cpp        X-Mod: synced LFO, ratio type, band-limited modulator
  np_xenv.S         RC envelopes
  np_runtime.cpp    ISR pools, profiling, USB/MIDI deferral, project load, power-on fallback
  np_ui.cpp         UI event ordering, scale library hook
  np_midi.cpp       MIDI out fan-out and receive fixes
  np_random.cpp     scale-aware automation randomizer
  np_mnrand.cpp     the instrument generator, kits, styles, scale library
tools/
  patch.py          applies the patches and builds the .syx
  flash.py          checks a .syx and sends it over USB from macOS (CoreMIDI)
  norand_syx.py     SysEx container and LZSS codec
  bench/            voice render bench
  diagmidi.py       decoder for the telemetry build
  rtti.py, scan.py, mkelf.py, xref.py, featuremap.py, periph.py   analysis
  ghidra/           Ghidra scripts used during the analysis
src/norand/       reconstructed C: 7-bit transport, SysEx receiver, LZSS
tests/            emulator and static tests
docs/
  patches.md        every change: what, why, where, how it is verified
  protocol.md       the update protocol
  architecture.md   target, memory map, class and driver model
  findings.md       engine internals (parameters, modulation matrix, DSP)
  audit.md          the original audit
  sysemu.md         the whole-firmware emulator
  instruments.md, scale-library.md, ratio-table.md, sync-table.md
```

---

## Status and limitations

- Every change is verified in the emulator, and the image is checked
  against the bootloader's limits. Many features have also been used on a
  real unit. Some fixes are verified in emulation only, notably the MIDI
  flow changes and the main-loop sequencer speedup.
- A rare reboot after DELETE+RECORD, reported on the unit, has not been
  reproduced. The telemetry build exists to capture it.
- Not fixed, from the stock: a project's events are trusted. A journal
  damaged in the middle (not just cut short), or Norand's project SysEx
  (`F0 00 21 50 00 10 00 01 …`) carrying a bad event, can make a handler
  write outside its object. On the unit that is a fault, which the fault
  handler turns into a reset. An unknown event id in that SysEx aborts,
  and the watchdog resets the unit.
- The image is close to the bootloader's 512 KiB limit. About 1.2 KiB is
  left.
- Analog behaviour (filter, drive, levels) differs from the bench's digital
  model. Instrument levels were set from the model.
