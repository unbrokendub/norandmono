#!/usr/bin/env python3
"""tools/holes.py -- find stock code the patches leave unreachable (HL/HM).

    build/venv/bin/python tools/holes.py [out.json]

Run after `patch.py build`; reads build/firmware.bin, the patched image and
build/firmware.symbols.json.  The holes patch.py fills (HOLES there) came
from this scan; a new patch that replaces a stock function may free more.

Dead stock code in the patched image.

Every reference anywhere in the patched image (stock part and our blob) is
collected by decoding at every halfword -- an over-approximation: data read
as an instruction can only add references, so it can only hide a hole, never
invent one.  Kinds: 32-bit b/bl/bcond, 16-bit b/bcond/cbz, pc-relative ldr,
ldr.w, ldrd, vldr, adr, and every halfword-aligned word whose value points
into the image.  A stock function (scan.py's list) is
  dead       if nothing outside it refers into it at all, and nothing can
             fall into it from the function before;
  body dead  if the only references are to its first instruction and that is
             now a b.w into the blob (R1-style: the rest is unreachable).
"""
import json, struct, sys
import capstone
BASE = 0x08100000
stock = open('build/firmware.bin', 'rb').read()
img = open('build/firmware-patched.bin', 'rb').read()
sym = json.load(open('build/firmware.symbols.json'))
fns = sorted(set(sym['functions']))
STOCK_END = BASE + len(stock)
IMG_END = BASE + len(img)
code = set(sym['code'])

def sx(v, bits):
    return v - (1 << bits) if v & (1 << (bits - 1)) else v

refs = []          # (src, tgt, kind)
for off in range(0, len(img) - 1, 2):
    a = BASE + off
    hw = struct.unpack_from('<H', img, off)[0]
    # 16-bit
    if hw & 0xF800 == 0xE000:
        refs.append((a, a + 4 + sx((hw & 0x7FF) << 1, 12), 'b16'))
    elif hw & 0xF000 == 0xD000 and (hw >> 8 & 0xF) < 0xE:
        refs.append((a, a + 4 + sx((hw & 0xFF) << 1, 9), 'bc16'))
    elif hw & 0xF500 == 0xB100:
        refs.append((a, a + 4 + ((hw >> 9 & 1) << 6 | (hw >> 3 & 0x1F) << 1), 'cbz'))
    elif hw & 0xF800 == 0x4800:
        refs.append((a, ((a + 4) & ~3) + (hw & 0xFF) * 4, 'ldr16'))
    elif hw & 0xF800 == 0xA000:
        refs.append((a, ((a + 4) & ~3) + (hw & 0xFF) * 4, 'adr16'))
    if off + 4 <= len(img):
        hw2 = struct.unpack_from('<H', img, off + 2)[0]
        if hw & 0xF800 == 0xF000 and hw2 & 0x8000:
            s = hw >> 10 & 1; j1 = hw2 >> 13 & 1; j2 = hw2 >> 11 & 1
            if hw2 & 0x1000:      # b.w / bl (and blx imm when bit 12 clear -- skip)
                i1, i2 = 1 - (j1 ^ s), 1 - (j2 ^ s)
                imm = s << 24 | i1 << 23 | i2 << 22 | (hw & 0x3FF) << 12 | (hw2 & 0x7FF) << 1
                refs.append((a, a + 4 + sx(imm, 25), 'bl' if hw2 & 0x4000 else 'bw'))
            elif not hw2 & 0x4000 and (hw >> 6 & 0xF) < 0xE:
                imm = s << 20 | j2 << 19 | j1 << 18 | (hw & 0x3F) << 12 | (hw2 & 0x7FF) << 1
                refs.append((a, a + 4 + sx(imm, 21), 'bcw'))
        # ldr.w / ldrb.w / ldrh.w / ldrsb / ldrsh / pld  pc-relative: 1111 1000 x0x1 1111
        if hw & 0xFF7F == 0xF85F or hw & 0xFF7F == 0xF81F or hw & 0xFF7F == 0xF83F or hw & 0xFF7F == 0xF91F or hw & 0xFF7F == 0xF93F:
            imm = hw2 & 0xFFF
            u = hw >> 7 & 1
            refs.append((a, ((a + 4) & ~3) + (imm if u else -imm), 'ldrw'))
        # ldrd pc-relative: 1110 100P U1W1 1111
        if hw & 0xFE5F == 0xE85F:
            imm = (hw2 & 0xFF) * 4
            refs.append((a, ((a + 4) & ~3) + (imm if hw >> 7 & 1 else -imm), 'ldrd'))
        # vldr pc-relative: 1110 1101 UD01 1111
        if hw & 0xFF3F == 0xED1F:
            imm = (hw2 & 0xFF) * 4
            refs.append((a, ((a + 4) & ~3) + (imm if hw >> 7 & 1 else -imm), 'vldr'))
        # adr.w: 1111 0i10 1010 1111 / 1111 0i10 0000 1111
        if hw & 0xFBFF == 0xF2AF or hw & 0xFBFF == 0xF20F:
            imm = (hw >> 10 & 1) << 11 | (hw2 >> 12 & 7) << 8 | (hw2 & 0xFF)
            refs.append((a, ((a + 4) & ~3) + (-imm if hw & 0x00A0 == 0x00A0 else imm), 'adrw'))
        v = struct.unpack_from('<I', img, off)[0]
        if BASE <= (v & ~1) < IMG_END:
            refs.append((a, v & ~1, 'word'))
# movw/movt pairs that build an image address (the blob's -mslow-flash-data
# code does): the movt's high half with the nearest earlier movw to the same
# register, up to 64 bytes back; an unmatched movt counts its whole 64 KB
movt = 0
for off in range(0, len(img) - 3, 2):
    hw, hw2 = struct.unpack_from('<HH', img, off)
    if hw & 0xFBF0 == 0xF2C0 and not hw2 & 0x8000:
        imm = (hw & 0xF) << 12 | (hw >> 10 & 1) << 11 | (hw2 >> 12 & 7) << 8 | (hw2 & 0xFF)
        if not 0x0810 <= imm <= 0x0817:
            continue
        movt += 1
        rd = hw2 >> 8 & 0xF
        lo = None
        for back in range(2, 66, 2):
            if off - back < 0:
                break
            h1, h2 = struct.unpack_from('<HH', img, off - back)
            if h1 & 0xFBF0 == 0xF240 and not h2 & 0x8000 and (h2 >> 8 & 0xF) == rd:
                lo = (h1 & 0xF) << 12 | (h1 >> 10 & 1) << 11 | (h2 >> 12 & 7) << 8 | (h2 & 0xFF)
                break
        if lo is None:
            for t in range(imm << 16, (imm + 1) << 16, 2):
                refs.append((BASE + off, t, 'movt?'))
        else:
            refs.append((BASE + off, (imm << 16 | lo) & ~1, 'movw/movt'))
print('references: %d; movt with an image address high half: %d' % (len(refs), movt))

import bisect
tg = sorted((t, s, k) for s, t, k in refs)
tkeys = [t for t, s, k in tg]
def into(lo, hi):
    i = bisect.bisect_left(tkeys, lo); j = bisect.bisect_left(tkeys, hi)
    return [(s, t, k) for t, s, k in tg[i:j] if not (lo <= s < hi)]

exits16 = lambda hw: hw == 0x4770 or hw & 0xFF00 == 0xBD00 or hw & 0xF800 == 0xE000 or hw & 0xFF87 == 0x4700
def can_fall_in(s):
    """could execution fall from the bytes before s into s? (conservative: yes
    unless the halfword before is an exit, padding 0x0000/0xBF00/0xFFFF in a
    pool, or s-4 is a b.w/pop.w pc)"""
    if s - BASE < 4:
        return True
    hw = struct.unpack_from('<H', img, s - BASE - 2)[0]
    hw32 = struct.unpack_from('<HH', img, s - BASE - 4)
    if exits16(hw):
        return False
    if hw32[0] & 0xF800 == 0xF000 and hw32[1] & 0xD000 == 0x9000:     # b.w
        return False
    if hw32[0] == 0xE8BD and hw32[1] & 0x8000:                           # pop.w {..pc}
        return False
    if (s - 2 - BASE) // 1 >= 0 and (s - 2) not in code:                 # data/pool before
        return False
    return True

dead, body = [], []
for i, s in enumerate(fns):
    if not (BASE <= s < STOCK_END):
        continue
    e = fns[i + 1] if i + 1 < len(fns) else STOCK_END
    e = min(e, STOCK_END)
    if e - s < 8:
        continue
    r = into(s, e)
    patched_entry = img[s - BASE:s - BASE + 4] != stock[s - BASE:s - BASE + 4]
    if not r and not can_fall_in(s):
        dead.append((e - s, s, e))
    elif r and all(t == s for _, t, _ in r) and patched_entry:
        hw, hw2 = struct.unpack_from('<HH', img, s - BASE)
        if hw & 0xF800 == 0xF000 and hw2 & 0xD000 == 0x9000:
            imm_tgt = [t for src, t, k in refs if src == s and k == 'bw']
            if imm_tgt and imm_tgt[0] >= STOCK_END:
                body.append((e - s - 4, s, e, len(r)))
dead.sort(reverse=True); body.sort(reverse=True)
print('\nunreferenced stock functions: %d, %d bytes' % (len(dead), sum(d[0] for d in dead)))
for n, s, e in dead[:40]:
    print('  %5d  %08x-%08x' % (n, s, e))
print('\nbodies behind a redirected entry: %d, %d bytes' % (len(body), sum(b[0] for b in body)))
for n, s, e, c in body:
    print('  %5d  %08x-%08x  (%d refs to the entry)' % (n, s, e, c))
if len(sys.argv) > 1:
    json.dump({'dead': dead, 'body': body}, open(sys.argv[1], 'w'))
