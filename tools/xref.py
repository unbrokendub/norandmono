#!/usr/bin/env python3
"""
xref.py -- cross reference helper for the symbolised images.

    xref.py fn 0x0810A65C          what a function calls, is called by,
                                   and which strings/vtables it touches
    xref.py str "Out of range"     which functions reference a string
    xref.py addr 0x0815A758        which functions reference an address
    xref.py top [n]                the n largest functions with their
                                   string references (a quick feature map)
    xref.py leaks                  functions that reference a panic/assert
"""

import bisect
import json
import re
import struct
import sys

IMAGES = {
    "firmware": ("build/firmware.bin", 0x08100000),
    "bootloader": ("build/bootloader.bin", 0x08020000),
}


class Db:
    def __init__(self, which="firmware"):
        path, self.base = IMAGES[which]
        self.data = open(path, "rb").read()
        self.end = self.base + len(self.data)
        sc = json.load(open("build/%s.symbols.json" % which))
        self.funcs = sc["functions"]
        self.code_bytes = {int(k, 16): v for k, v in sc.get("code_bytes", {}).items()}
        self.calls = {int(k, 16): [c & ~1 for c in v] for k, v in sc["calls"].items()}
        self.callers = {}
        for f, cs in self.calls.items():
            for c in cs:
                self.callers.setdefault(c, set()).add(f)

        self.strings = {}
        for m in re.finditer(rb"[\x20-\x7e]{4,240}\x00", self.data):
            self.strings[self.base + m.start()] = m.group(0)[:-1].decode()

        self.names = {}
        try:
            rt = json.load(open("build/rtti.json"))
            for a, v in rt["vtables"].items():
                self.names[int(a, 16)] = "vtable " + v["demangled"]
            for a, t in rt["typeinfo"].items():
                self.names[int(a, 16)] = "typeinfo " + t["demangled"]
        except OSError:
            pass

        # word -> list of offsets, for reverse lookups
        self.words = {}
        for off in range(0, len(self.data) - 3, 4):
            w = struct.unpack_from("<I", self.data, off)[0]
            self.words.setdefault(w, []).append(self.base + off)

    def owner(self, addr):
        i = bisect.bisect_right(self.funcs, addr) - 1
        return self.funcs[i] if i >= 0 else None

    def size(self, f):
        i = bisect.bisect_right(self.funcs, f) - 1
        nxt = self.funcs[i + 1] if i + 1 < len(self.funcs) else self.end
        return nxt - f

    def refs_in(self, f):
        """literal-pool words inside f that name something we know"""
        lo, hi = f, f + self.size(f)
        out = []
        for off in range(lo - self.base, min(hi, self.end) - self.base - 3, 4):
            w = struct.unpack_from("<I", self.data, off)[0]
            if w in self.strings:
                out.append(("str", w, self.strings[w]))
            elif w in self.names:
                out.append(("rtti", w, self.names[w]))
        return out

    def refs_to(self, addr):
        return sorted({self.owner(a) for a in self.words.get(addr, []) if self.owner(a)})


def cmd_fn(db, arg):
    f = int(arg, 16) & ~1
    f = db.owner(f)
    print("function 0x%08X  size %d" % (f, db.size(f)))
    cs = db.callers.get(f, set())
    print("  called by (%d): %s" % (len(cs), " ".join("0x%08X" % c for c in sorted(cs)[:16])))
    ce = db.calls.get(f, [])
    print("  calls     (%d): %s" % (len(ce), " ".join("0x%08X" % c for c in sorted(set(ce))[:16])))
    for kind, w, txt in db.refs_in(f):
        print("  %-5s 0x%08X  %s" % (kind, w, txt[:110]))


def cmd_str(db, arg):
    hits = [(a, s) for a, s in db.strings.items() if arg.lower() in s.lower()]
    for a, s in sorted(hits)[:40]:
        fns = db.refs_to(a)
        print("0x%08X  %-70s  <- %s" % (a, s[:70],
                                        " ".join("0x%08X" % f for f in fns[:8]) or "(no direct ref)"))


def cmd_addr(db, arg):
    a = int(arg, 16)
    print(" ".join("0x%08X" % f for f in db.refs_to(a)) or "(none)")


def cmd_top(db, n):
    real = [f for f in db.funcs if db.code_bytes.get(f, 0) >= 32]
    rows = sorted(real, key=lambda f: db.code_bytes.get(f, 0), reverse=True)[:int(n)]
    for f in rows:
        rs = [t for k, w, t in db.refs_in(f) if k == "str"]
        rt = [t for k, w, t in db.refs_in(f) if k == "rtti"]
        tag = "; ".join((rs + rt)[:3])
        print("0x%08X %6d  callers=%-3d %s" %
              (f, db.code_bytes.get(f, 0), len(db.callers.get(f, ())), tag[:100]))


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    which = "bootloader" if "--bootloader" in argv else "firmware"
    argv = [a for a in argv if a != "--bootloader"]
    db = Db(which)
    cmd = argv[1]
    if cmd == "fn":
        cmd_fn(db, argv[2])
    elif cmd == "str":
        cmd_str(db, argv[2])
    elif cmd == "addr":
        cmd_addr(db, argv[2])
    elif cmd == "top":
        cmd_top(db, argv[2] if len(argv) > 2 else 40)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
