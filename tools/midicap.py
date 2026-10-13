#!/usr/bin/env python3
"""
midicap.py -- record the unit's MIDI telemetry without a message limit, and
decode the one-second transition records as they arrive.

MIDI Monitor keeps only its last ~1000 messages, and one full P6 snapshot is
616 of them, so a capture saved after a freeze never held the seconds before
it.  This records everything to a file (in a form tools/diagmidi.py reads)
and prints every transition record live, so the moment the panel freezes is
on screen with the seconds that led to it.

    build/venv/bin/python tools/midicap.py            # picks the "Mono" port
    build/venv/bin/python tools/midicap.py --list
    build/venv/bin/python tools/midicap.py --port "MiniFuse 4"
    build/venv/bin/python tools/midicap.py --out mycapture.txt

Stop with Ctrl-C.  Afterwards:

    build/venv/bin/python tools/diagmidi.py capture-<stamp>.txt
"""
import argparse
import datetime
import os
import sys
import time

import rtmidi

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import diagmidi  # noqa: E402


class Decoder:
    """Groups channel-16 CCs into records as they arrive; prints each one."""
    def __init__(self):
        self.tag = None
        self.parts = []
        self.count = 0

    def feed(self, cc, value):
        if cc == diagmidi.TAG:
            self.flush()
            self.tag, self.parts = value, []
        elif self.tag in diagmidi.FULL_INDEX:
            self.parts.append((cc, value))
        elif diagmidi.DATA0 <= cc < diagmidi.DATA0 + 5 and self.tag is not None:
            self.parts.append((cc - diagmidi.DATA0, value))

    def flush(self):
        if self.tag is None:
            return
        self.count += 1
        if self.tag == diagmidi.TR:
            words = diagmidi.p6_words(self.parts, diagmidi.EXPECTED[diagmidi.TR])
            diagmidi.show_tr(words)
        elif self.tag == diagmidi.EV:
            diagmidi.show_ev(diagmidi.p6_words(self.parts, diagmidi.EXPECTED[diagmidi.EV]))
        elif self.tag == diagmidi.PM:
            diagmidi.show_pm(diagmidi.p6_words(self.parts, diagmidi.EXPECTED[diagmidi.PM]))
        elif self.tag == diagmidi.P6:
            words = diagmidi.p6_words(self.parts, diagmidi.EXPECTED[diagmidi.P6])
            ok = diagmidi.p6_valid(words)
            up = words[4] if len(words) > 4 and words[4] is not None else '?'
            print('  P6 snapshot at %s ms %s' % (up, 'CRC OK' if ok else 'incomplete'))
        else:
            print('  record tag %d, %d fragments' % (self.tag, len(self.parts)))
        sys.stdout.flush()


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--list', action='store_true', help='list MIDI input ports and exit')
    ap.add_argument('--port', default='Mono', help='substring of the input port name (default: Mono)')
    ap.add_argument('--out', default=None, help='output file (default: capture-<stamp>.txt)')
    args = ap.parse_args(argv)

    midi = rtmidi.MidiIn()
    ports = midi.get_ports()
    if args.list:
        for i, p in enumerate(ports):
            print('%d: %s' % (i, p))
        return 0
    matches = [i for i, p in enumerate(ports) if args.port.lower() in p.lower()]
    if not matches:
        print('no input port matching %r; available: %s' % (args.port, ports))
        print('(plug the Mono in over USB, or pass --port "MiniFuse 4" for DIN)')
        return 1
    index = matches[0]
    out = args.out or 'capture-%s.txt' % datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    f = open(out, 'w')
    f.write('# midicap %s port=%s\n' % (datetime.datetime.now().isoformat(), ports[index]))
    dec = Decoder()
    t0 = time.monotonic()
    n = 0

    def on_message(event, data=None):
        nonlocal n
        if f.closed:                     # stopping: the port is still open
            return
        msg, _delta = event
        t = time.monotonic() - t0
        f.write('t=%.3f %s\n' % (t, ' '.join('%02X' % b for b in msg)))
        n += 1
        if len(msg) == 3 and msg[0] == 0xBF:
            dec.feed(msg[1], msg[2])

    midi.ignore_types(sysex=False, timing=False, active_sense=True)
    midi.set_callback(on_message)
    midi.open_port(index)
    print('recording %s -> %s   (Ctrl-C to stop)' % (ports[index], out))
    print('every second a T line; the freeze is the second the numbers change')
    try:
        while True:
            time.sleep(0.5)
            f.flush()
    except KeyboardInterrupt:
        pass
    # Save first, then let go of the port.  rtmidi's close_port() can wait
    # forever for its callback thread on macOS while a busy stream is still
    # arriving (it hung through thirty Ctrl-Cs with the EV log running), so
    # it gets two seconds in a daemon thread and the process exits anyway.
    f.flush()
    f.close()
    dec.flush()
    print('\nstopped: %d messages, %d records -> %s' % (n, dec.count, out))
    print('decode:  build/venv/bin/python tools/diagmidi.py %s' % out)
    sys.stdout.flush()
    import threading

    def release():
        try:
            midi.cancel_callback()
            midi.close_port()
        except Exception:
            pass
    t = threading.Thread(target=release, daemon=True)
    t.start()
    t.join(2.0)
    os._exit(0)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
