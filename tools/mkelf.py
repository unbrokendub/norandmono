#!/usr/bin/env python3
"""
mkelf.py -- wrap a raw Norand Mono image in an ELF32/ARM file with symbols.

The result loads directly in objdump, radare2, Ghidra or IDA with the right
base address, Thumb mapping symbols ($t/$d) and every name we managed to
recover (RTTI classes, vtables, functions named after the assert/log strings
they reference).

    mkelf.py firmware    -> build/firmware.elf
    mkelf.py bootloader  -> build/bootloader.elf
"""

import json
import re
import struct
import sys

IMAGES = {
    "firmware": ("build/firmware.bin", 0x08100000),
    "bootloader": ("build/bootloader.bin", 0x08020000),
}

STT_NOTYPE, STT_OBJECT, STT_FUNC = 0, 1, 2
STB_GLOBAL = 1
SHN_TEXT = 1


def sanitize(s):
    s = re.sub(r"[^A-Za-z0-9_]+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")[:120]


def collect_strings(data, base):
    """offset -> printable string"""
    out = {}
    for m in re.finditer(rb"[\x20-\x7e]{5,220}\x00", data):
        out[base + m.start()] = m.group(0)[:-1].decode()
    return out


def name_from_string(s):
    """Derive a function name from an assert / log string."""
    m = re.search(r"([A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_~][A-Za-z0-9_]*)+)", s)
    if not m:
        return None
    name = sanitize(m.group(1))
    # template instantiations: keep the argument so the names stay unique
    t = re.search(r"\bT = ([A-Za-z_][A-Za-z0-9_:<>, ]*)", s)
    if t:
        name += "__" + sanitize(t.group(1))
    return name


def build(which):
    path, base = IMAGES[which]
    data = open(path, "rb").read()
    end = base + len(data)
    syms = {}                      # addr -> (name, type, size)

    def add(addr, name, typ=STT_FUNC, size=0):
        if addr in syms and syms[addr][1] == STT_FUNC and typ != STT_FUNC:
            return
        syms[addr] = (name, typ, size)

    # ---- functions from the scanner --------------------------------------
    try:
        sc = json.load(open("build/%s.symbols.json" % which))
        funcs = sc["functions"]
    except OSError:
        funcs = []
    for f in funcs:
        add(f, "sub_%08X" % f, STT_FUNC)

    # ---- RTTI ------------------------------------------------------------
    vtable_slot_owner = {}
    try:
        rtti = json.load(open("build/rtti.json"))
    except OSError:
        rtti = None
    if rtti and rtti["base"] == base:
        for a, t in rtti["typeinfo"].items():
            add(int(a, 16), "typeinfo_" + sanitize(t["demangled"]), STT_OBJECT, 8)
        for a, v in rtti["vtables"].items():
            cls = sanitize(v["demangled"])
            add(int(a, 16), "vtable_" + cls, STT_OBJECT, 4 * len(v["slots"]))
            if v["offset_to_top"] == 0:
                for i, s in enumerate(v["slots"]):
                    vtable_slot_owner.setdefault(s & ~1, (cls, i))

    # ---- names from referenced strings -----------------------------------
    strings = collect_strings(data, base)
    for a, s in strings.items():
        add(a, "str_" + (sanitize(s)[:60] or "%08X" % a), STT_OBJECT, len(s) + 1)

    # a literal-pool word inside a function that points at a string tells us
    # what that function is called
    func_set = set(funcs)
    fstarts = sorted(func_set)
    import bisect

    def owner(addr):
        i = bisect.bisect_right(fstarts, addr) - 1
        return fstarts[i] if i >= 0 else None

    named = {}
    for off in range(0, len(data) - 3, 4):
        w = struct.unpack_from("<I", data, off)[0]
        if w in strings:
            f = owner(base + off)
            if f is None or base + off - f > 0x600:
                continue
            nm = name_from_string(strings[w])
            if nm and f not in named:
                named[f] = nm
    for f, nm in named.items():
        add(f, nm, STT_FUNC)

    # vtable slots keep their class prefix when still unnamed
    for a, (cls, i) in vtable_slot_owner.items():
        if a in syms and syms[a][0].startswith("sub_"):
            add(a, "%s__vfn%d_%08X" % (cls, i, a), STT_FUNC)

    # ---- vector table ----------------------------------------------------
    vec = ["_estack", "Reset_Handler", "NMI_Handler", "HardFault_Handler",
           "MemManage_Handler", "BusFault_Handler", "UsageFault_Handler",
           "res7", "res8", "res9", "res10", "SVC_Handler", "DebugMon_Handler",
           "res13", "PendSV_Handler", "SysTick_Handler"]
    for i, nm in enumerate(vec):
        if 4 * i + 4 > len(data):
            break
        w = struct.unpack_from("<I", data, 4 * i)[0]
        if i and base <= w < end:
            add(w & ~1, nm, STT_FUNC)

    # ---- mapping symbols -------------------------------------------------
    # $t marks Thumb code, $d marks data; without them every disassembler
    # decodes the vector table and the literal pools as ARM instructions.
    marks = {}
    code = set(sc.get("code", [])) if funcs else set()
    for f in funcs:
        marks[f] = "$t"
    for lo, hi in sc.get("data_ranges", []) if funcs else []:
        marks[lo] = "$d"
        if hi in code:
            marks[hi] = "$t"
    # vector table and everything past the last function is data
    marks[base] = "$d"
    if funcs:
        marks[min(funcs)] = "$t"
        last = max(funcs)
        tail = max((a for a in code if a >= last), default=last)
        marks[tail + 2] = "$d"
    mapping = sorted(marks.items())

    # ---- emit ------------------------------------------------------------
    strtab = bytearray(b"\x00")
    symtab = bytearray(16)         # index 0 reserved

    def strid(s):
        i = strtab.find(s.encode() + b"\x00")
        if i < 0:
            i = len(strtab)
            strtab.extend(s.encode() + b"\x00")
        return i

    # make every name unique
    used = {}
    for addr in sorted(syms):
        name, typ, size = syms[addr]
        if name in used:
            name = "%s_%08X" % (name, addr)
            syms[addr] = (name, typ, size)
        used[name] = addr

    entries = []
    for addr, (name, typ, size) in syms.items():
        val = addr | 1 if typ == STT_FUNC else addr
        entries.append((strid(name), val, size, (STB_GLOBAL << 4) | typ))
    for addr, kind in mapping:
        entries.append((strid(kind), addr, 0, (0 << 4) | STT_NOTYPE))

    for nameoff, val, size, info in entries:
        symtab.extend(struct.pack("<IIIBBH", nameoff, val, size, info, 0, SHN_TEXT))

    shstr = bytearray(b"\x00")

    def shid(s):
        i = len(shstr)
        shstr.extend(s.encode() + b"\x00")
        return i

    n_text = shid(".text")
    n_symtab = shid(".symtab")
    n_strtab = shid(".strtab")
    n_shstrtab = shid(".shstrtab")

    ehsize = 52
    phsize = 32
    off_text = ehsize + phsize
    off_symtab = off_text + len(data)
    off_strtab = off_symtab + len(symtab)
    off_shstr = off_strtab + len(strtab)
    off_sh = (off_shstr + len(shstr) + 3) & ~3

    eh = struct.pack(
        "<16sHHIIIIIHHHHHH",
        b"\x7fELF\x01\x01\x01\x00" + b"\x00" * 8,
        2,              # ET_EXEC
        40,             # EM_ARM
        1,
        base | 1,       # entry
        ehsize,         # phoff
        off_sh,         # shoff
        0x05000200,     # flags: EF_ARM_EABI_VER5 | hard float-ish
        ehsize, phsize, 1,
        40, 5, 4,
    )
    ph = struct.pack("<IIIIIIII", 1, off_text, base, base, len(data), len(data), 5, 4)

    def sh(name, typ, flags, addr, off, size, link=0, info=0, align=1, entsize=0):
        return struct.pack("<IIIIIIIIII", name, typ, flags, addr, off, size,
                           link, info, align, entsize)

    shs = b"".join([
        sh(0, 0, 0, 0, 0, 0),
        sh(n_text, 1, 0x6, base, off_text, len(data), align=4),
        sh(n_symtab, 2, 0, 0, off_symtab, len(symtab), link=3, info=1, align=4, entsize=16),
        sh(n_strtab, 3, 0, 0, off_strtab, len(strtab), align=1),
        sh(n_shstrtab, 3, 0, 0, off_shstr, len(shstr), align=1),
    ])

    out = bytearray()
    out += eh + ph + data + symtab + strtab + shstr
    out += b"\x00" * (off_sh - len(out))
    out += shs

    dst = "build/%s.elf" % which
    open(dst, "wb").write(out)
    print("%s: %d symbols (%d functions, %d named) -> %s" %
          (which, len(syms), len(funcs), len(named), dst))


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "firmware")
