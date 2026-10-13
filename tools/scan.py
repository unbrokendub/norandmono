#!/usr/bin/env python3
"""
scan.py -- recursive-descent code discovery for the Norand Mono images.

Starts from the vector table, every vtable slot and every RTTI-derived entry
point, follows direct branches and BL targets, and records:

  * function entry points and their extent
  * literal pools (so they are not disassembled as code)
  * call graph edges

Writes build/<name>.symbols.json for the ELF builder.
"""

import json
import struct
import sys

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_LITTLE_ENDIAN
from capstone.arm import ARM_OP_IMM, ARM_OP_MEM, ARM_REG_PC

IMAGES = {
    "firmware": ("build/firmware.bin", 0x08100000),
    "bootloader": ("build/bootloader.bin", 0x08020000),
}


class Image:
    def __init__(self, path, base):
        self.data = open(path, "rb").read()
        self.base = base
        self.end = base + len(self.data)
        self.md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)
        self.md.detail = True

    def has(self, a):
        return self.base <= a < self.end

    def u32(self, a):
        return struct.unpack_from("<I", self.data, a - self.base)[0]

    def u16(self, a):
        return struct.unpack_from("<H", self.data, a - self.base)[0]


def scan(img, seeds):
    funcs = {}            # entry -> set of instruction addresses
    calls = {}            # entry -> set of callees
    data_words = set()    # literal pool addresses
    queue = list(seeds)
    seen_entry = set()

    while queue:
        entry = queue.pop()
        entry &= ~1
        if entry in seen_entry or not img.has(entry):
            continue
        seen_entry.add(entry)
        body = set()
        edges = set()
        work = [entry]
        visited = set()
        limits = {}
        while work:
            pc = work.pop()
            while True:
                if pc in visited or not img.has(pc) or pc in data_words:
                    break
                visited.add(pc)
                off = pc - img.base
                ins = next(img.md.disasm(img.data[off:off + 4], pc, 1), None)
                if ins is None:
                    break
                body.add(pc)
                mn = ins.mnemonic
                nxt = pc + ins.size

                # remember "cmp rN, #imm" so a following tbb/tbh knows how
                # many entries its table has
                if mn == "cmp" and len(ins.operands) == 2 and \
                        ins.operands[1].type == ARM_OP_IMM:
                    limits[ins.operands[0].reg] = ins.operands[1].imm

                # literal pool references
                for op in ins.operands:
                    if op.type == ARM_OP_MEM and op.mem.base == ARM_REG_PC:
                        a = ((pc + 4) & ~3) + op.mem.disp
                        for k in range(4):
                            data_words.add(a + k)

                if mn.startswith("bl") and not mn.startswith("blo") and \
                        not mn.startswith("bls") and not mn.startswith("blt") and \
                        not mn.startswith("ble"):
                    if mn in ("bl", "blx") and ins.operands and \
                            ins.operands[0].type == ARM_OP_IMM:
                        t = ins.operands[0].imm & ~1
                        edges.add(t)
                        if t not in seen_entry:
                            queue.append(t)
                    pc = nxt
                    continue

                if mn in ("b", "b.w") or (mn.startswith("b") and ins.cc not in (0, 15)):
                    if ins.operands and ins.operands[0].type == ARM_OP_IMM:
                        t = ins.operands[0].imm
                        if img.has(t):
                            work.append(t)
                    # unconditional branch ends the linear run
                    if mn in ("b", "b.w"):
                        break
                    pc = nxt
                    continue

                if mn in ("cbz", "cbnz"):
                    for op in ins.operands:
                        if op.type == ARM_OP_IMM and img.has(op.imm):
                            work.append(op.imm)
                    pc = nxt
                    continue

                if mn in ("bx", "pop", "ldm", "ldmia") and "pc" in ins.op_str:
                    break
                if mn == "bx":
                    break
                if mn in ("tbb", "tbh"):
                    # inline jump table: entries live right after the
                    # instruction and are scaled offsets from pc + 4.  The
                    # entry count comes from the "cmp rN, #limit / bhi
                    # default" guard the compiler emits just before.
                    tbl = pc + 4
                    n = limits.get(ins.operands[0].mem.index)
                    if n is not None and n < 512:
                        wide = mn == "tbh"
                        for k in range(n + 1):
                            a = tbl + (2 * k if wide else k)
                            if not img.has(a + (1 if wide else 0)):
                                break
                            v = img.u16(a) if wide else img.data[a - img.base]
                            work.append(tbl + 2 * v)
                        span = (n + 1) * (2 if wide else 1)
                        for k in range(span + 2):
                            data_words.add(tbl + k)
                    break
                if mn == "udf":
                    break
                pc = nxt

        funcs[entry] = body
        calls[entry] = edges

    return funcs, calls, data_words


def main(argv):
    which = argv[1] if len(argv) > 1 else "firmware"
    path, base = IMAGES[which]
    img = Image(path, base)

    # seeds carry an origin: "strong" ones are unambiguous entry points,
    # "weak" ones are any word that merely looks like a Thumb pointer and are
    # dropped later if they land inside a function we already know
    seeds = {}

    def seed(a, strong):
        a &= ~1
        if strong or a not in seeds:
            seeds[a] = strong or seeds.get(a, False)

    nvec = 0x188
    for a in range(4, min(nvec, len(img.data)), 4):
        w = img.u32(base + a)
        if img.has(w) and w & 1:
            seed(w, True)

    try:
        rtti = json.load(open("build/rtti.json"))
        if rtti["base"] == base:
            for v in rtti["vtables"].values():
                for s in v["slots"]:
                    seed(s, True)
    except (OSError, ValueError):
        pass

    for off in range(0, len(img.data) - 3, 4):
        w = struct.unpack_from("<I", img.data, off)[0]
        if img.has(w) and w & 1:
            seed(w, False)

    funcs, calls, data = scan(img, seeds)

    # anything reached by a BL is a real entry point too
    strong = {a for a, s in seeds.items() if s}
    for cs in calls.values():
        strong |= {c & ~1 for c in cs}

    # a real entry point either is reached explicitly, or opens with a
    # recognisable prologue
    def prologue(a):
        off = a - img.base
        ins = next(img.md.disasm(img.data[off:off + 4], a, 1), None)
        if ins is None:
            return False
        if ins.mnemonic in ("push", "stmdb") and "lr" in ins.op_str:
            return True
        return ins.mnemonic == "sub" and ins.op_str.startswith("sp")

    keep = {a for a in funcs if a in strong or prologue(a)}
    inner = set()
    for f in keep:
        body = funcs[f]
        for a in funcs:
            if a > f and a not in strong and a in body:
                inner.add(a)
    for a in inner:
        del funcs[a]
        calls.pop(a, None)

    covered = set()
    for b in funcs.values():
        covered |= b

    print("%s: %d bytes" % (which, len(img.data)))
    print("  seeds        : %d" % len(seeds))
    print("  functions    : %d" % len(funcs))
    print("  code bytes   : %d (%.1f%%)" %
          (len(covered) * 2, 200.0 * len(covered) / len(img.data)))
    print("  literal words: %d" % (len(data) // 4))

    # merge literal-pool bytes into ranges
    dr = []
    for a in sorted(data):
        if dr and a == dr[-1][1]:
            dr[-1][1] = a + 1
        else:
            dr.append([a, a + 1])

    out = {
        "base": base,
        "functions": sorted(funcs),
        # bytes actually decoded as instructions inside each function; a
        # near-zero count means the "function" is really data that happened
        # to contain a word looking like a Thumb pointer
        "code_bytes": {("0x%08X" % f): len(b) * 2 for f, b in sorted(funcs.items())},
        "code": sorted(covered),
        "data_ranges": dr,
        "calls": {("0x%08X" % k): sorted(v) for k, v in calls.items() if v},
    }
    json.dump(out, open("build/%s.symbols.json" % which, "w"))
    print("  wrote build/%s.symbols.json" % which)


if __name__ == "__main__":
    main(sys.argv)
