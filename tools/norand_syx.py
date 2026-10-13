#!/usr/bin/env python3
"""
norand_syx.py -- Norand Mono SysEx firmware container codec.

Reverse engineered from mono_secondary_bootloader.syx (secondary bootloader,
runs from 0x08020000 on an STM32H7) -- see docs/protocol.md.

Two container types share the same SysEx prefix  F0 00 23 50 00 02 :

  * "bootloader" images  (command byte 0x7E, terminated by a 0x7F message)
      payload is 4-bit nibble coded, 32 bytes per message + 8-bit checksum.
      Flashed by the *primary* bootloader.

  * "firmware" images    (command byte is part of the packed header)
      payload is MIDI 7-bit coded (1 MSB byte + 7 data bytes), 98 bytes of
      LZSS-compressed image per message.  Flashed by the secondary bootloader
      to flash bank 2 @ 0x08100000.

Usage:
    norand_syx.py unpack  <in.syx> <out.bin>      # -> raw image
    norand_syx.py pack    <in.bin> <out.syx>      # raw image -> .syx
    norand_syx.py info    <in.syx>
    norand_syx.py selftest <fw.syx> <bl.syx>
"""

import sys
import struct

SYSEX_PREFIX = bytes([0xF0, 0x00, 0x23, 0x50, 0x00, 0x02])

# ---------------------------------------------------------------------------
# MIDI 7-bit transport (bootloader routine @ 0x08026F24)
# ---------------------------------------------------------------------------
# Groups of 8 septets -> 7 octets.  The first byte of each group carries the
# MSBs of the following 7 bytes, bit6 -> first byte ... bit0 -> seventh byte.


def unpack7(data: bytes) -> bytes:
    out = bytearray()
    for g in range(0, len(data), 8):
        grp = data[g:g + 8]
        msb = grp[0]
        for n, b in enumerate(grp[1:]):
            out.append(b | (0x80 if msb & (1 << (6 - n)) else 0))
    return bytes(out)


def pack7(data: bytes) -> bytes:
    out = bytearray()
    for g in range(0, len(data), 7):
        grp = data[g:g + 7]
        msb = 0
        for n, b in enumerate(grp):
            if b & 0x80:
                msb |= 1 << (6 - n)
        out.append(msb)
        out.extend(b & 0x7F for b in grp)
    return bytes(out)


# ---------------------------------------------------------------------------
# LZSS codec (bootloader decoder @ 0x08026460, node methods @ 0x08026010..)
# ---------------------------------------------------------------------------
# Optional header:  CF 00 <uleb128 uncompressed size>
# Then a token stream terminated by a 0x00 byte, followed by a literal pool.
#
#   token & 0x80 == 0x80 : 2 bytes.  D = ((t & 0x7F) << 3) | (t1 >> 5)
#                          L = (t1 & 0x1F) + 3
#                          copy L bytes from out[len(out) - L - D]
#   token & 0xC0 == 0x40 : literal run of (t & 0x3F) + 1 bytes from the pool
#   token != 0           : RLE, (t + 2) copies of one byte from the pool
#   token == 0           : end of stream
#
# The decoder keeps a 1057 byte ring buffer, so L + D <= 1057.

WINDOW = 1057
MIN_MATCH = 3
MAX_MATCH = 34
MAX_D = 1023


class LzssError(Exception):
    pass


def lzss_decompress(src: bytes) -> bytes:
    p = 0
    declared = None
    if len(src) >= 2 and src[0] == 0xCF and src[1] == 0x00:
        p = 2
        declared = 0
        shift = 0
        while True:
            b = src[p]
            p += 1
            declared |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break

    # locate the literal pool: walk the tokens until a zero first byte
    i = p
    while src[i]:
        i += 2 if src[i] & 0x80 else 1
    pool = i + 1

    out = bytearray()
    c = p
    while True:
        t = src[c]
        if t == 0:
            break
        if t & 0x80:
            t1 = src[c + 1]
            c += 2
            dist = ((t & 0x7F) << 3) | (t1 >> 5)
            ln = (t1 & 0x1F) + 3
            s = len(out) - ln - dist
            if s < 0:
                raise LzssError("back reference before start of output")
            for k in range(ln):
                out.append(out[s + k])
        elif t & 0x40:
            c += 1
            n = (t & 0x3F) + 1
            out += src[pool:pool + n]
            pool += n
        else:
            c += 1
            n = t + 2
            out += bytes([src[pool]]) * n
            pool += 1

    if declared is not None and declared != len(out):
        raise LzssError("size mismatch: header %d, decoded %d" % (declared, len(out)))
    return bytes(out)


def _uleb128(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def lzss_compress(data: bytes) -> bytes:
    """Greedy LZSS producing a stream the on-device decoder accepts."""
    tokens = bytearray()
    pool = bytearray()

    # hash table of 3 byte sequences -> list of positions
    table = {}
    i = 0
    n = len(data)
    pending = bytearray()          # literals waiting to be flushed

    def flush_literals():
        while pending:
            # an RLE token is cheaper for >= 3 equal bytes, but only run
            # lengths 3..65 are encodable and it must cover the whole run
            chunk = pending[:64]
            del pending[:len(chunk)]
            tokens.append(0x40 | (len(chunk) - 1))
            pool.extend(chunk)

    while i < n:
        # try a run first (RLE token, 3..65 identical bytes)
        run = 1
        while run < 65 and i + run < n and data[i + run] == data[i]:
            run += 1
        if run >= 3:
            flush_literals()
            tokens.append(run - 2)
            pool.append(data[i])
            for k in range(i, i + run):
                if k + MIN_MATCH <= n:
                    table.setdefault(data[k:k + MIN_MATCH], []).append(k)
            i += run
            continue

        best_len = 0
        best_dist = 0
        if i + MIN_MATCH <= n:
            key = data[i:i + MIN_MATCH]
            cand = table.get(key)
            if cand:
                lo = i - WINDOW
                # keep the candidate list short
                if len(cand) > 64:
                    del cand[:-64]
                for pos in reversed(cand):
                    if pos < lo:
                        break
                    ln = MIN_MATCH
                    limit = min(MAX_MATCH, n - i)
                    while ln < limit and data[pos + ln] == data[i + ln]:
                        ln += 1
                    # encoded distance D = (i - pos) - ln  must be 0..1023
                    d = (i - pos) - ln
                    while d < 0 and ln > MIN_MATCH:
                        ln -= 1
                        d = (i - pos) - ln
                    if d < 0 or d > MAX_D:
                        continue
                    if ln > best_len:
                        best_len = ln
                        best_dist = d
                        if ln == MAX_MATCH:
                            break

        if best_len >= MIN_MATCH:
            flush_literals()
            tokens.append(0x80 | (best_dist >> 3))
            tokens.append(((best_dist & 7) << 5) | (best_len - 3))
            for k in range(i, i + best_len):
                if k + MIN_MATCH <= n:
                    table.setdefault(data[k:k + MIN_MATCH], []).append(k)
            i += best_len
        else:
            pending.append(data[i])
            if i + MIN_MATCH <= n:
                table.setdefault(data[i:i + MIN_MATCH], []).append(i)
            i += 1
            if len(pending) == 64:
                flush_literals()

    flush_literals()
    tokens.append(0x00)
    return bytes([0xCF, 0x00]) + _uleb128(len(data)) + bytes(tokens) + bytes(pool)


# ---------------------------------------------------------------------------
# SysEx message framing
# ---------------------------------------------------------------------------

def split_messages(raw: bytes):
    msgs = []
    i = 0
    while i < len(raw):
        if raw[i] != 0xF0:
            raise ValueError("expected F0 at offset %d" % i)
        j = raw.index(0xF7, i)
        msgs.append(raw[i:j + 1])
        i = j + 1
    return msgs


def detect(raw: bytes) -> str:
    m = split_messages(raw)[0]
    if not m.startswith(SYSEX_PREFIX):
        raise ValueError("not a Norand SysEx file")
    return "bootloader" if m[6] == 0x7E else "firmware"


# ----- bootloader container ------------------------------------------------

BL_BYTES_PER_MSG = 32


def bl_unpack(raw: bytes) -> bytes:
    out = bytearray()
    for m in split_messages(raw):
        if m[6] == 0x7F:            # end-of-transfer message
            continue
        if m[6] != 0x7E:
            raise ValueError("unexpected bootloader command 0x%02X" % m[6])
        p = m[6:-1]
        data = bytes(((p[2 + 2 * k] << 4) | p[3 + 2 * k]) for k in range(BL_BYTES_PER_MSG))
        cks = (p[2 + 2 * BL_BYTES_PER_MSG] << 4) | p[3 + 2 * BL_BYTES_PER_MSG]
        if (sum(data) & 0xFF) != cks:
            raise ValueError("bootloader checksum mismatch")
        out += data
    return bytes(out)


def bl_pack(image: bytes) -> bytes:
    if len(image) % BL_BYTES_PER_MSG:
        image = image + b"\x00" * (BL_BYTES_PER_MSG - len(image) % BL_BYTES_PER_MSG)
    out = bytearray()
    for off in range(0, len(image), BL_BYTES_PER_MSG):
        chunk = image[off:off + BL_BYTES_PER_MSG]
        out += SYSEX_PREFIX + bytes([0x7E, 0x00])
        for b in chunk:
            out.append(b >> 4)
            out.append(b & 0x0F)
        cks = sum(chunk) & 0xFF
        out.append(cks >> 4)
        out.append(cks & 0x0F)
        out.append(0xF7)
    out += SYSEX_PREFIX + bytes([0x7F, 0x00, 0xF7])
    return bytes(out)


# ----- firmware container --------------------------------------------------

FW_BYTES_PER_MSG = 98           # decoded bytes carried by one message
FW_TAIL_FIELD = 0x16            # third header byte, constant in stock files


def fw_unpack(raw: bytes) -> bytes:
    msgs = split_messages(raw)
    packed = b"".join(unpack7(m[10:-1]) for m in msgs)
    return lzss_decompress(packed)


def fw_unpack_packed(raw: bytes) -> bytes:
    """Return the LZSS stream without decompressing it."""
    return b"".join(unpack7(m[10:-1]) for m in split_messages(raw))


def fw_pack_packed(packed: bytes) -> bytes:
    if len(packed) % FW_BYTES_PER_MSG:
        packed = packed + b"\x00" * (FW_BYTES_PER_MSG - len(packed) % FW_BYTES_PER_MSG)
    chunks = [packed[i:i + FW_BYTES_PER_MSG] for i in range(0, len(packed), FW_BYTES_PER_MSG)]
    total = len(chunks) - 1         # index carried by the very first message
    out = bytearray()
    for n, chunk in enumerate(chunks):
        idx = total if n == 0 else n
        hdr = bytes([(idx >> 8) & 0xFF, idx & 0xFF, FW_TAIL_FIELD])
        out += SYSEX_PREFIX + pack7(hdr) + pack7(chunk) + b"\xF7"
    return bytes(out)


def fw_pack(image: bytes) -> bytes:
    return fw_pack_packed(lzss_compress(image))


# ---------------------------------------------------------------------------

def cmd_info(path):
    raw = open(path, "rb").read()
    kind = detect(raw)
    msgs = split_messages(raw)
    print("file        : %s" % path)
    print("type        : %s" % kind)
    print("messages    : %d" % len(msgs))
    if kind == "bootloader":
        img = bl_unpack(raw)
        print("image size  : %d bytes (0x%X)" % (len(img), len(img)))
        print("load addr   : 0x08020000 (secondary bootloader)")
    else:
        packed = fw_unpack_packed(raw)
        img = lzss_decompress(packed)
        print("packed size : %d bytes" % len(packed))
        print("image size  : %d bytes (0x%X)" % (len(img), len(img)))
        print("load addr   : 0x08100000 (flash bank 2)")
        print("ratio       : %.1f%%" % (100.0 * len(packed) / len(img)))
    sp, reset = struct.unpack_from("<II", img, 0)
    print("init SP     : 0x%08X" % sp)
    print("reset       : 0x%08X" % reset)


def cmd_unpack(src, dst):
    raw = open(src, "rb").read()
    kind = detect(raw)
    img = bl_unpack(raw) if kind == "bootloader" else fw_unpack(raw)
    open(dst, "wb").write(img)
    print("%s: %s image, %d bytes -> %s" % (src, kind, len(img), dst))


def cmd_pack(src, dst, kind):
    img = open(src, "rb").read()
    raw = bl_pack(img) if kind == "bootloader" else fw_pack(img)
    open(dst, "wb").write(raw)
    print("%s: %d bytes -> %s (%d bytes of SysEx)" % (src, len(img), dst, len(raw)))


def cmd_selftest(fw, bl):
    ok = True

    raw = open(bl, "rb").read()
    img = bl_unpack(raw)
    re = bl_pack(img)
    print("bootloader round trip : %s (%d bytes)" %
          ("OK" if re == raw else "MISMATCH", len(img)))
    ok &= re == raw

    raw = open(fw, "rb").read()
    packed = fw_unpack_packed(raw)
    re = fw_pack_packed(packed)
    print("firmware  round trip  : %s (%d packed bytes)" %
          ("OK" if re == raw else "MISMATCH", len(packed)))
    ok &= re == raw

    img = lzss_decompress(packed)
    print("lzss decode           : %d bytes" % len(img))
    mine = lzss_compress(img)
    back = lzss_decompress(mine)
    print("lzss re-encode        : %s (%d bytes, stock %d, %+.1f%%)" %
          ("OK" if back == img else "MISMATCH", len(mine), len(packed),
           100.0 * (len(mine) - len(packed)) / len(packed)))
    ok &= back == img
    return 0 if ok else 1


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    if cmd == "info":
        cmd_info(argv[2])
    elif cmd == "unpack":
        cmd_unpack(argv[2], argv[3])
    elif cmd == "pack":
        kind = argv[4] if len(argv) > 4 else "firmware"
        cmd_pack(argv[2], argv[3], kind)
    elif cmd == "selftest":
        return cmd_selftest(argv[2], argv[3])
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
