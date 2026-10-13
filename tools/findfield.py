#!/usr/bin/env python3
"""
findfield.py -- find every instruction that computes an offset into a
structure, so a field reached as `base + imm` can be traced to its writers.

The readers of the audio-rate pitch term do `movw r3, #0x2F20; add r3, r0`,
which leaves no literal to grep for.  This walks the whole image with a
disassembler and reports every movw / add.w / addw whose immediate lands in
a requested window, together with the instruction that follows.

    findfield.py 0x2E00 0x3100
"""

import bisect
import json
import sys

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_LITTLE_ENDIAN
from capstone.arm import ARM_OP_IMM

BASE = 0x08100000


def main():
    lo = int(sys.argv[1], 16) if len(sys.argv) > 1 else 0x2E00
    hi = int(sys.argv[2], 16) if len(sys.argv) > 2 else 0x3100

    img = open("build/firmware.bin", "rb").read()
    sc = json.load(open("build/firmware.symbols.json"))
    funcs = sc["functions"]
    code = set(sc["code"])

    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)
    md.detail = True

    def owner(a):
        i = bisect.bisect_right(funcs, a) - 1
        return funcs[i] if i >= 0 else 0

    hits = []
    for addr in sorted(code):
        off = addr - BASE
        ins = next(md.disasm(img[off:off + 4], addr, 1), None)
        if ins is None:
            continue
        if ins.mnemonic not in ("movw", "add.w", "addw", "add"):
            continue
        imms = [o.imm for o in ins.operands if o.type == ARM_OP_IMM]
        if not imms or not (lo <= imms[-1] <= hi):
            continue
        nxt = next(md.disasm(img[off + ins.size:off + ins.size + 8],
                             addr + ins.size, 2), None)
        follow = []
        p = addr + ins.size
        for _ in range(3):
            n = next(md.disasm(img[p - BASE:p - BASE + 4], p, 1), None)
            if n is None:
                break
            follow.append("%s %s" % (n.mnemonic, n.op_str))
            p += n.size
        hits.append((addr, owner(addr), imms[-1],
                     "%s %s" % (ins.mnemonic, ins.op_str), follow))

    print("instructions computing base+0x%04X..0x%04X : %d\n" % (lo, hi, len(hits)))
    for addr, fn, imm, txt, follow in hits:
        print("0x%08X  in 0x%08X  +0x%04X  %-24s | %s" %
              (addr, fn, imm, txt, " ; ".join(follow)))


if __name__ == "__main__":
    main()
