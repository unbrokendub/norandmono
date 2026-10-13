#!/usr/bin/env python3
"""Print adjacent shortcut results; not a pass/fail regression test."""
from probe_ui_chord import UiProbe, UI

for stock in (True, False):
    for keys in ((26, 2, 3), (2, 3, 26), (24, 4)):
        print('stock', stock, 'press', keys, flush=True)
        p = UiProbe(stock=stock, verbose=True)
        for key in keys:
            p.key(key, 0)
        for key in reversed(keys):
            p.key(key, 2)
        print('result', p.mode(), [e.hex() for e in p.project_events], flush=True)
