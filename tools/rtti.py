#!/usr/bin/env python3
"""
rtti.py -- recover the C++ class layout of the Norand Mono firmware.

Walks the Itanium ABI RTTI structures left in the image by GCC:

  * type_info name strings          (mangled, "N9monosynth6DriverE" style)
  * __class_type_info objects       (vptr, name, [base...])
  * vtables                         (offset-to-top, typeinfo ptr, slots...)

Emits a report and a JSON symbol table usable by the ELF builder.
"""

import json
import re
import struct
import subprocess
import sys

BASE = 0x08100000


def load(path):
    return open(path, "rb").read()


def rd32(d, off):
    return struct.unpack_from("<I", d, off)[0]


def in_image(d, addr):
    return BASE <= addr < BASE + len(d)


def main(argv):
    path = argv[1] if len(argv) > 1 else "build/firmware.bin"
    d = load(path)
    end = BASE + len(d)

    # ---- 1. mangled type_info name strings -------------------------------
    names = {}
    for m in re.finditer(rb"[A-Za-z0-9_.$]{3,250}\x00", d):
        s = m.group(0)[:-1]
        if s.startswith(b"N") and s.endswith(b"E"):
            names[BASE + m.start()] = s.decode()
        elif re.fullmatch(rb"\d+[A-Za-z_][A-Za-z0-9_]*", s):
            names[BASE + m.start()] = s.decode()

    # ---- 2. type_info objects: a word pointing at a name string ----------
    # layout: { const void *vptr; const char *name; ... }
    typeinfos = {}
    for off in range(0, len(d) - 7, 4):
        name_ptr = rd32(d, off + 4)
        if name_ptr in names:
            vptr = rd32(d, off)
            # the vptr points into the abi's __class_type_info vtable which is
            # itself inside the image; use that as a weak sanity check
            if in_image(d, vptr):
                typeinfos[BASE + off] = {
                    "name": names[name_ptr],
                    "ti_vptr": vptr,
                }

    # group type_info kinds by their vptr (one per abi class)
    kinds = {}
    for ti in typeinfos.values():
        kinds[ti["ti_vptr"]] = kinds.get(ti["ti_vptr"], 0) + 1

    # ---- 3. base classes -------------------------------------------------
    # __si_class_type_info: { vptr, name, base }
    for addr, ti in typeinfos.items():
        off = addr - BASE
        if off + 12 <= len(d):
            b = rd32(d, off + 8)
            if b in typeinfos:
                ti["base"] = typeinfos[b]["name"]

    # ---- 4. vtables ------------------------------------------------------
    # ...[ptrdiff_t offset_to_top][type_info *][slot0][slot1]...
    vtables = {}
    for off in range(4, len(d) - 7, 4):
        ti = rd32(d, off)
        if ti not in typeinfos:
            continue
        top = rd32(d, off - 4)
        if top not in (0,) and not (0xFFFF0000 <= top <= 0xFFFFFFFF):
            continue
        slots = []
        p = off + 4
        while p + 4 <= len(d):
            w = rd32(d, p)
            if not (in_image(d, w) and w & 1):     # thumb function pointer
                break
            slots.append(w)
            p += 4
        if slots:
            vtables[BASE + off + 4] = {
                "typeinfo": ti,
                "class": typeinfos[ti]["name"],
                "offset_to_top": struct.unpack("<i", struct.pack("<I", top))[0],
                "slots": slots,
            }

    # ---- demangle --------------------------------------------------------
    raw = "\n".join(sorted({t["name"] for t in typeinfos.values()}))
    try:
        dem = subprocess.run(["c++filt", "-t"], input=raw, capture_output=True,
                             text=True).stdout.split("\n")
    except FileNotFoundError:
        dem = raw.split("\n")
    demangled = dict(zip(sorted({t["name"] for t in typeinfos.values()}), dem))

    print("image           : %s (%d bytes @ 0x%08X-0x%08X)" % (path, len(d), BASE, end))
    print("name strings    : %d" % len(names))
    print("type_info objs  : %d" % len(typeinfos))
    print("abi ti kinds    : %s" % ", ".join("0x%08X x%d" % kv for kv in sorted(kinds.items())))
    print("vtables         : %d" % len(vtables))
    print()
    print("%-10s %-6s %s" % ("VTABLE", "SLOTS", "CLASS"))
    for addr in sorted(vtables):
        v = vtables[addr]
        nm = demangled.get(v["class"], v["class"])
        print("0x%08X %-6d %s%s" % (addr, len(v["slots"]), nm,
                                    "" if v["offset_to_top"] == 0 else
                                    "  [thunk %d]" % v["offset_to_top"]))

    out = {
        "base": BASE,
        "size": len(d),
        "typeinfo": {("0x%08X" % a): {**t, "demangled": demangled.get(t["name"], t["name"])}
                     for a, t in typeinfos.items()},
        "vtables": {("0x%08X" % a): {**v, "demangled": demangled.get(v["class"], v["class"])}
                    for a, v in vtables.items()},
    }
    with open("build/rtti.json", "w") as f:
        json.dump(out, f, indent=1)
    print("\nwrote build/rtti.json")


if __name__ == "__main__":
    main(sys.argv)
