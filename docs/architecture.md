# Norand Mono — firmware architecture

Derived from `mono_V2.2.0r.syx` (application) and
`mono_secondary_bootloader.syx` (updater). Everything here is recovered from
the binaries; nothing is guessed from marketing material.

## 1. Target

**STM32H743** (or a pin/die compatible H75x), Cortex-M7, Thumb-2, hard float.

Evidence: 32 byte (256 bit) flash programming words, two 1 MiB flash banks
with 8 sectors each, AXI SRAM at `0x24000000`, D2 SRAM at `0x30000000`,
D3 SRAM at `0x38000000`, GPIO block at `0x58020000`, RCC at `0x58024400`.

### Flash layout

| region | contents |
|--------|----------|
| `0x08000000` | primary bootloader — **not contained in either .syx** |
| `0x08020000` | secondary bootloader, 30880 bytes, `_estack = 0x20020000` |
| `0x08100000` | application, 479704 bytes (bank 2, erased as 8 sectors) |

### Application RAM

| region | contents |
|--------|----------|
| `0x08175080` | `.data` load address (344 bytes, last part of the image) |
| `0x20000000 .. 0x20000158` | `.data` (DTCM) |
| `0x20000158 .. 0x2000E2DC` | `.bss` (DTCM, 57732 bytes) |
| `0x24000000` | AXI SRAM — image staging during an update |
| `0x30000000` | D2 SRAM — DMA buffers |
| `0x38000000` | D3 SRAM |

Entry points: `Reset_Handler = 0x08155FF8`, `SystemInit = 0x081093F4`,
`__libc_init_array = 0x08159FB0`, `main = 0x08119938`,
`SysTick_Handler = 0x081196D8`.

## 2. Language and build

The firmware is **C++**, built with GCC/`arm-none-eabi` against libstdc++
with RTTI and exceptions machinery present:

* Itanium ABI `type_info` objects and vtables are intact — 272 type_info
  records and 150 vtables were recovered (`docs/rtti.txt`, `docs/classes.txt`)
* `std::vector`, `std::deque`, `std::bitset` are all instantiated
  (`vector::_M_realloc_insert`, `cannot create std::deque larger than
  max_size()`, `bitset::set`)
* `__PRETTY_FUNCTION__` strings survive, e.g.
  `static constexpr const char* Enki::Type::get_index() [with T =
  monosynth::RecSeqData<monosynth::NoteData>; index_t = const char*]`

Three namespaces:

| namespace | role |
|-----------|------|
| `monosynth` | the product: drivers, sequencer, UI, project model |
| `synthpp` | in-house framework: `Event`, `EventCloneable<T>`, `StampedEvent` |
| `Enki` | compile time reflection helpers (`Enki::Type::get_index<T>()`) |

The secondary bootloader is built from the same codebase (same `System` /
driver vtable pattern) but without RTTI, so its classes have no names.

## 3. Driver layer

`System` is a singleton (`0x08109614` returns it; `0x0810977C` is
`driver(index)`), constructed at `0x0810F80C`, initialised at `0x0810961C`.
It embeds eight driver sub-objects and publishes them through a pointer
array at `this + 0x1A44`:

| index | offset in `System` | class | constructor |
|-------|-------------------|-------|-------------|
| 0 | `+0x0000` | `monosynth::System` | `0x0810F80C` |
| 1 | `+0x01B4` | `monosynth::PotsADC` | `0x0810F060` |
| 2 | `+0x04E0` | `monosynth::MemoryQSPI` | `0x0810A65C` |
| 3 | `+0x0F18` | `monosynth::LedSPI` | `0x0810987C` |
| 4 | `+0x1190` | `monosynth::Midi` | `0x0810EE90` |
| 5 | `+0x17A8` | `monosynth::CVDac` | `0x08109548` |
| 6 | `+0x17E8` | `monosynth::SwitchManager` | `0x0810F59C` |
| 7 | `+0x187C` | `monosynth::Usb` | `0x0810FFD4` |

(`SpiDriver` at `0x0810F440` and `RandGen` at `0x0810F2CC` are helpers rather
than entries in the driver array.) Every driver derives from
`monosynth::Driver`; slot 2 of each vtable is the `init()` the bootloader
calls in a loop (`0x08025404` in the bootloader, `0x0810961C` here).

The bootloader registers only five of them, which is why an update can run
with the audio path completely uninitialised.

### Peripherals

Full reference lists are in `docs/peripherals-firmware.txt` and
`docs/peripherals-bootloader.txt`. Summary:

| block | use |
|-------|-----|
| QUADSPI | external NOR flash — projects/patterns (`MemoryQSPI`, JEDEC ID read at `0x0810BC10`) |
| SPI4 | LED drivers (`LedSPI`) |
| SPI3 | CV/gate DAC (`CVDac`) |
| ADC1 + ADC3 | pots and CV inputs (`PotsADC`) |
| USART1 | DIN MIDI |
| USB OTG HS + FS | USB MIDI (`Usb`) |
| TIM1/3/5/8, TIM12, TIM15..17 | clocking, gate timing |
| DMA1/DMA2/MDMA | ADC, SPI and QSPI transfers |

## 4. Object model

`docs/classes.txt` has the full list. The interesting families:

**Events** — `synthpp::Event` is the base, `synthpp::EventCloneable<T>` the
CRTP wrapper, `synthpp::StampedEvent` adds a timestamp. Three event
channels exist:

* `monosynth::UiEvent<PotEventData | SwitchEventData>`
* `monosynth::ControllerEvent<CNoteData | CExpressivityData |
  CPatternParamData | CXoXData | CParamTrigData | ExpressionLevelData |
  MorphData>`
* `monosynth::ProjectEvent<...>` and `monosynth::SequencerEvent<...>`

**Project/pattern model** — the `...Data` types enumerated by
`Enki::Type::get_index` are the serialised commands of the sequencer.
They map one to one onto user-visible operations, which makes them the best
index into the feature set:

`NoteData`, `ExpressivityData`, `ParamAutomationData`, `ParamTrigData`,
`XoXData`, `Pitch`, `NormalizedFloat16`, `NormalizedBipolarFloat16`,
`RecSeqData<T>` (live record), `PSeqData<T>` (pattern sequence),
`CopyPaste*RangeData`, `Clear*RangeData`, `Rand*Data`/`Rand*RangeData`
(randomisers), `TransposeRangeData`, `TransposeRangeToScaleData`,
`RotatePatternData`, `SetPatternMorphData`, `SaveMorphResultData`,
`ModNote*`, `PatchParamData`, `PatternParamData`, `ProjectParamData`,
`SaveProjectData` / `LoadProjectData`, `SetCustomScaleNoteData`.

**Synth parameters** — `ControllerParameter` with concrete subclasses
`MedianValueCParam`, `LfoWaveformCParam`, `LFOLevelCParam`,
`LFOSpeedCParam`, `LFOTimeCParam`, `XEnvDestCParam`, `EnvAttackCParam`,
`EnvDecayCParam`, `EnvLevelCParam`; and the value mapping classes
`ExpADSynthParameter`, `LinADSynthParameter`,
`QuantizedLinADSynthParameter`, `NoADSynthParameter`,
`EnumSynthParameter`.

The 19 patch parameters, in order, from the table at `0x0815A758`:

```
VCO1Tune VCO1Fine VCO1Wave VCO1Vol
VCO2Tune VCO2Fine VCO2Wave VCO2Vol
VCOTZFM
FiltFreq FiltReso FiltColor FiltEnv
EnvAttack EnvDecay EnvSust EnvRel
OutVol Sync
```

Device identity strings live next to them: `Norand`, `Mono`,
`000000000020`, `MIDI Config`, `Midi Interface`.

## 5. Assertion strings as a symbol source

`MemoryQSPI` logs HAL failures with the function name spelled out, which
gives free symbols:

```
HAL error in MemoryQSPI::LL_Read (HAL_QSPI_Command)
HAL error in MemoryQSPI::LL_Read_Async (HAL_QSPI_Receive_DMA)
HAL error in MemoryQSPI::LL_WriteInPage (HAL_QSPI_Transmit)
HAL error in MemoryQSPI::LL_Erase
HAL error in MemoryQSPI::ReadJEDECID (HAL_QSPI_Receive)
Incorrect block type in MemoryQSPI::LL_Erase
Invalid Chip Descriptor
Smallest erasable size must be smaller or equal to bytes buffer size
Chip flash size must be divisible by all block sizes
Borrowable already borrowed
```

`tools/mkelf.py` walks literal pools and names a function after the first
such string it references — 296 functions get real names this way.

## 6. What the scan found

| image | bytes | functions | code coverage |
|-------|-------|-----------|---------------|
| bootloader | 30880 | 244 | 63.8 % |
| firmware | 479704 | 2591 | 51.0 % |

Firmware coverage is against the whole image; `.rodata` starts around
`0x0815A000`, so the ratio against `.text` alone is roughly two thirds. The
remainder is reached through jump tables and function pointers stored in
data, which the recursive scan does not follow.
