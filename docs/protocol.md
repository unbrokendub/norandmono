# Norand Mono SysEx update protocol

Everything below was recovered from `mono_secondary_bootloader.syx` itself.
Addresses refer to the secondary bootloader image, which runs from
`0x08020000`.

## 1. Manufacturer prefix

Both containers use

```
F0 00 23 50 00 02 ... F7
```

`00 23 50` is the three byte manufacturer ID, `00 02` a product/protocol
selector. The bootloader compares only the **first five** bytes strictly
(table at `0x08027720`, comparison at `0x08026B48`); bytes 5..9 are payload.

## 2. Bootloader container (`mono_secondary_bootloader.syx`)

Command byte `0x7E`. One message carries 32 image bytes:

```
F0 00 23 50 00 02 7E 00 <64 nibbles> <2 nibbles> F7
       |                    |            |
       |                    |            +-- checksum: sum(32 bytes) & 0xFF
       |                    +--------------- 32 bytes, high nibble first
       +-------------------------------- 0x7E = write, 0x7F = end of transfer
```

The file ends with a bare `F0 00 23 50 00 02 7F 00 F7`. Nibble coding wastes
half the bandwidth but keeps every byte below 0x80 as MIDI requires.

`mono_secondary_bootloader.syx` = 965 data messages = **30880 bytes**, loaded
at `0x08020000`. This image is written by the *primary* bootloader, which is
not part of either file.

## 3. Firmware container (`mono_V2.2.0r.syx`)

```
F0 00 23 50 00 02 <h0 h1 h2 h3> <112 septets> F7      (123 bytes total)
```

### 3.1 Seven bit transport

Groups of 8 transmitted bytes carry 7 payload bytes. The first byte of a
group holds the high bits of the seven that follow, **most significant
first**: bit 6 belongs to the first byte, bit 0 to the seventh.
Decoder: `0x08026F24` (reconstructed in `src/norand/midi7.c`).

`h0..h3` decode to three payload bytes and the 112 septets to 98 payload
bytes.

### 3.2 Message header

| byte | meaning |
|------|---------|
| 0..1 | packet index, big endian |
| 2    | trailing length field, `0x16` in every stock file |

The **first** message carries the total packet count in the index field
instead of an index, and its payload is already the first data chunk.
Messages `1..total` follow in order. When a message whose index equals the
total is terminated with `F7`, the accumulated buffer is decompressed and
flashed.

`mono_V2.2.0r.syx`: 2859 messages, total = 2858, 280182 packed bytes.

### 3.3 A stale size estimate in the stock bootloader

At `0x08026BF8` the bootloader computes

```
declared_size = tail + total * 7 * 16          /* = total * 112 */
```

i.e. it assumes 112 payload bytes per message, while the V2.2.0 updater
sends 98. The value only feeds the progress bar and a `>= 0x80000` sanity
check, and the decompressor ignores the `size` argument entirely
(`0x08026464` overwrites it immediately), so the mismatch is harmless.
Worth keeping in mind before changing the packet size.

### 3.4 The image may not exceed 512 KB — and bank 2 is erased first

**Corrected 2026-10-08, after p43 left a unit without an application.**
The decompress-and-flash routine `0x08026460` erases all eight sectors of
bank 2 at its entry, *then* parses the stream.  Three limits follow, all
`0x80000`:

| where | what | over it |
|---|---|---|
| `0x08026746` | the `CF 00 <uleb128>` uncompressed size | the size reads as 0 → refused (`0x080266AA`) |
| `0x080264B2` / `0x0802675A` | the token stream's length | refused |
| `0x08026C00` | messages × 112 (receive path) | refused |

A refusal after the erase leaves the unit in the bootloader with bank 2
blank; it returns to its loop (`0x080266AA` frees the decoder and returns,
no reset), so sending a good file recovers it.  **The image itself is
capped at 524,288 bytes**, not at the settings sector.
`tools/patch.py` refuses to build past it; `tests/test_bootloader_accepts.py`
runs the bootloader's own routine in Unicorn on a .syx and checks it
reaches the jump to the application with bank 2 holding exactly the image.

## 4. Payload compression

The buffer accumulated in AXI SRAM (`0x24000000`) is an LZSS stream.
Decoder: `0x08026460` plus the node methods at `0x08026010..0x080260C0`;
reconstructed in `src/norand/lzss.c`.

```
[CF 00 <uleb128 uncompressed size>]     optional header
<token> ... <token> 00                  token stream
<literal pool>                          bytes consumed by literal/RLE tokens
```

| token | length | meaning |
|-------|--------|---------|
| `00` | 1 | end of stream |
| `1ttttttt tttlllll` (`t & 0x80`) | 2 | match: `D = ((t & 0x7F) << 3) \| (t1 >> 5)`, `L = (t1 & 0x1F) + 3`, copy `L` bytes from `out[n - L - D]` |
| `01xxxxxx` | 1 | literal run of `x + 1` bytes taken from the pool |
| `00xxxxxx`, non zero | 1 | run of `t + 2` copies of one pool byte |

Distances are `0..1023` and lengths `3..34`, so the furthest back reference
is 1057 bytes — exactly the size of the ring buffer the device allocates
(`memset` of 1060 bytes at `0x080264DE`, modulo 1057 at `0x080265B0`).

Splitting literals into a separate pool is what makes the format work here:
the token stream can be scanned for its `0x00` terminator without ever
tripping over a literal byte that happens to be zero.

`mono_V2.2.0r.syx`: 280182 packed bytes -> **479704 bytes** (`0x751D8`).

## 5. Flashing

`0x08026460` erases bank 2 first:

```
FLASH_EraseInitTypeDef { TypeErase = SECTORS, Banks = 2,
                         Sector = 0, NbSectors = 8, VoltageRange = 3 }
```

8 sectors x 128 KiB = the full 1 MiB bank 2 of an STM32H743. Output is then
written in 32 byte flash words (the H7 native 256 bit programming unit):

```
addr = 0x08100000 + page * 32
HAL_FLASH_Program(FLASH_TYPEPROGRAM_FLASHWORD, addr, staging_buffer)
```

When the stream ends the bootloader tears down the peripherals, disables
interrupts, clears the NVIC enable/pending registers, loads
`*(uint32_t *)0x08100004` and jumps to it (`0x0802663A` and `0x080267C8`).

## 6. Memory map used during an update

| address | use |
|---------|-----|
| `0x08000000` | primary bootloader (not shipped in these files) |
| `0x08020000` | secondary bootloader, 30880 bytes |
| `0x08100000` | application image, 479704 bytes (flash bank 2) |
| `0x20000000` | bootloader `.data` / `.bss`, `_estack = 0x20020000` |
| `0x24000000` | AXI SRAM, receive buffer for the packed image |
| `0x30000000` | D2 SRAM, second buffer registered by `main` |

## 7. Tooling

`tools/norand_syx.py` implements all of the above and round trips both stock
files byte for byte:

```bash
python3 tools/norand_syx.py selftest mono_V2.2.0r.syx mono_secondary_bootloader.syx
python3 tools/norand_syx.py unpack mono_V2.2.0r.syx build/firmware.bin
python3 tools/norand_syx.py pack   build/firmware.bin patched.syx firmware
```

The bundled compressor is slightly denser than the stock one (262608 vs
280182 bytes for the same image), so a repacked stock image is smaller than
the original file and still decodes identically.

`tools/flash.py` checks a firmware .syx the same way and sends it from
macOS (section 8).

## 8. The USB receive path

The update arrives on the bulk OUT endpoint 0x01 (64 bytes, descriptor at
`0x08027780`). On its way to the parser in section 3 nothing checks it.

| step | where | what it does |
|---|---|---|
| USB OUT callback | `0x080250D4` | splits the transfer into 4-byte USB-MIDI event packets |
| ring write | `0x08026DD0` | stores each packet in a 128-entry ring at `0x20001AD0` (indexes `+0x204` write, `+0x200` read); **no full check** |
| main loop | `0x0802698C` | waits for the SysTick flag `0x200016C0` (1 kHz) and then drains the ring once: **one drain per millisecond** |
| packet consumer | `0x08026C80` | ignores the CIN byte and feeds all three data bytes, using only F0/F7 to tell where a message starts and ends |
| SysEx parser | `0x08026B00` | header match, 7-bit groups into the AXI SRAM buffer, flash on the last index |

What follows:

- **Overflow.** More than 127 packets between two drains laps the ring.
  The lost packets go unnoticed. A 123-byte firmware message is 41
  packets, so three messages inside one millisecond fit and four do not.
  That is about 380 bytes per millisecond, under half of what full-speed
  USB can carry.
- **Foreign traffic.** A real-time packet such as `0F F8 00 00` (clock)
  inside a message feeds its two padding zeros as data. A long SysEx that
  fails the header match keeps writing header bytes: the header index at
  `+0x54C` has no bound (`0x08026B48`), and bytes 20..27 land on the write
  offset (`+0x540`) and the buffer pointer (`+0x544`). The next update then
  writes wherever those point. Short SysEx (a 6-byte device inquiry) and
  real-time packets between messages are harmless.
- **No integrity check.** Message indexes are parsed but never compared
  with a running count, and the payload carries no checksum. A garbled
  stream either stalls (the last index never arrives), faults, or reaches
  the flash routine, which erases bank 2 before it parses anything.
- **State survives a failed send.** The write offset and the announced
  total are cleared only at bootloader entry (`0x08026E10`). A second send
  into the same session appends to the first. Power-cycle into the
  bootloader before resending.

`src/norand/sysex_rx.c` bounds the header index; the stock code does not.

### 8.1 Control requests that crash it

The MIDI class table at `0x080277E8` holds Init, DeInit, a **null Setup**,
DataIn and DataOut. The device stack is an old ST USB device library, and
it calls `pClass->Setup` without a NULL check:

| request | handler | result |
|---|---|---|
| any request to interface 0 or 1 once configured (standard, class or vendor: GET_STATUS, GET/SET_INTERFACE, GET_CUR...) | `0x08024D30` | `blx 0` |
| SET_FEATURE / CLEAR_FEATURE(ENDPOINT_HALT) on endpoint 0x01 or 0x81 | `0x08024D6C` | `blx 0` |
| class request to an endpoint | `0x08024D6C` | `blx 0` |
| device requests (GET_DESCRIPTOR, GET_STATUS, SET_CONFIGURATION...) | `0x08024A9C` | fine |

`blx` to address 0 on a Cortex-M faults (INVSTATE) into the HardFault loop
at `0x0802534C`. The USB interrupt never runs again, the OUT endpoint is
never re-armed, and the control request never completes.
`tests/test_bootloader_usb_setup.py` runs the setup stage `0x08024864` on
each request.

On macOS 15.8 the update hung part-way twice: the SEQ LEDs stopped at 2/16
with 8 ms pacing and at 3/16 with 30 ms pacing. Both times the kernel log
showed, when the unit was switched off, a pending OUT write, a pending IN
read and a **pending control request on endpoint 0**. Earlier power-offs of
the running application left only the IN read. No USB error was logged
during either send.

The device was held open by Apple services, MIDIServer in one session and
usbaudiod in the other, so third-party software could not have sent the
request. It most likely comes from macOS's own USB/MIDI stack. Which
request it was would take a USB capture, and on macOS that needs SIP
disabled.

Windows sends none of these requests during a transfer, which is why
updates work there.

Two other macOS quirks were checked in emulation and ruled out:

- CoreMIDI's single-byte "escape" packets (`0F xx 00 00`) garble this
  bootloader's buffer but do not stall it: all 16 LEDs light, then the
  image is bad.
- CoreMIDI's known drop/duplicate of 512-byte blocks needs a backlog of
  large SysEx, which paced 123-byte messages never build.

On Windows, an MME MIDI port belongs to one application at a time and the
usual utilities pause between messages, so the data-path hazards above
rarely show there. On macOS, CoreMIDI shares a port among all applications
and sends as fast as the bus allows. `tools/flash.py` sends one message per
`MIDISend`, at least `--gap` ms apart (default 15), and nothing else. That
handles the data path but not the control requests in 8.1.

The receive loop also drains a second ring (`+0x410`, parsed by
`0x08026A10`) meant for DIN MIDI. The bootloader sets up no UART, though,
so nothing ever fills it and DIN cannot interfere with an update. That is
also why DIN does not work for updates.

A bootloader that faults mid-transfer stops re-arming the OUT endpoint, and
the host's write then stays pending. macOS logs nothing until the unit is
switched off, at which point the pending request completes as
`pipe stalled`. A successful update ends with the bootloader jumping to the
application. The unit drops off USB and enumerates again (VID:PID
1209:DF00, product "Mono"). `tools/flash.py` waits for that.

`tests/test_bootloader_usb_rx.py` runs the stock code from the callback to
the flash routine in Unicorn and checks the cases above.
`tests/test_flash_coremidi.py` sends a file through the real CoreMIDI to a
virtual port and checks the bytes and the spacing.
