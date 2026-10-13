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
