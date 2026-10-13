#!/usr/bin/env python3
"""
featuremap.py -- map the firmware's command/event types onto code.

Every user visible operation on the Mono is expressed as a `...Data` command
carried by a `ProjectEvent<T>` / `ControllerEvent<T>` / `SequencerEvent<T>` /
`UiEvent<T>`.  GCC left both the vtable and the RTTI record for each
instantiation in the image, so the functions that reference them are exactly
the sites that build or dispatch that command.

Output: a table of command -> constructing/handling functions.
"""

import bisect
import json
import re
import struct
import sys

BASE = 0x08100000


def main():
    d = open("build/firmware.bin", "rb").read()
    rt = json.load(open("build/rtti.json"))
    sc = json.load(open("build/firmware.symbols.json"))
    funcs = sc["functions"]
    code_bytes = {int(k, 16): v for k, v in sc.get("code_bytes", {}).items()}

    def owner(a):
        i = bisect.bisect_right(funcs, a) - 1
        return funcs[i] if i >= 0 else None

    # address -> label
    label = {}
    for a, v in rt["vtables"].items():
        label[int(a, 16)] = ("vtable", v["demangled"])
    for a, t in rt["typeinfo"].items():
        label[int(a, 16)] = ("typeinfo", t["demangled"])

    refs = {}
    for off in range(0, len(d) - 3, 4):
        w = struct.unpack_from("<I", d, off)[0]
        if w in label:
            f = owner(BASE + off)
            if f is not None and code_bytes.get(f, 0) >= 16:
                kind, name = label[w]
                refs.setdefault(name, {}).setdefault(kind, set()).add(f)

    # group by the payload type inside the <>
    groups = {}
    for name, kinds in refs.items():
        m = re.search(r"<(.+)>$", name)
        key = m.group(1) if m else name
        key = key.replace("monosynth::", "")
        outer = name.split("<")[0].replace("monosynth::", "").replace("synthpp::", "")
        groups.setdefault(key, {}).setdefault(outer, set())
        for s in kinds.values():
            groups[key][outer] |= s

    print("# Command / event type map\n")
    print("Functions listed are those whose literal pool names the vtable or")
    print("RTTI record of that instantiation: the sites that construct, clone")
    print("or dynamic_cast the event.\n")
    print("%-38s %-22s %s" % ("PAYLOAD TYPE", "CARRIER", "FUNCTIONS"))
    for key in sorted(groups):
        for outer in sorted(groups[key]):
            fs = sorted(groups[key][outer])
            print("%-38s %-22s %s%s" %
                  (key[:38], outer[:22],
                   " ".join("0x%08X" % f for f in fs[:6]),
                   " ..." if len(fs) > 6 else ""))


if __name__ == "__main__":
    sys.exit(main())
