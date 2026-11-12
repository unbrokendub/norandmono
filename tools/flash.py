#!/usr/bin/env python3
"""flash.py -- send a Norand Mono firmware .syx over USB from macOS.

    python3 tools/flash.py build/mono_V2.2.0r-patched.syx
    python3 tools/flash.py --list                  # MIDI destinations
    python3 tools/flash.py --check file.syx        # validate only, any OS

Needs only the system Python (CoreMIDI through ctypes, no packages).

STATUS: on macOS 15 the stock bootloader hung part-way in both attempts so
far.  Each time a USB control request to the unit was left pending, and the
bootloader faults on interface requests and endpoint HALT requests (its
class has no Setup handler, see docs/protocol.md section 8).  The requests
come from macOS's own USB/MIDI stack, which this tool cannot stop.  Flash
from Windows until that is solved; the checks below still help anywhere.

Why not any SysEx utility: the Mono's bootloader takes USB-MIDI packets
into a 128-entry ring that its main loop drains once per millisecond, with
no overflow check, no gap check on the message indexes and no checksum on
the payload (docs/protocol.md, section 8).  A host that pushes more than
127 packets (about 380 bytes) between two drains, or another application
that sends a long SysEx or a clock byte to the same port, garbles the
update silently.  macOS lets every application share a port and sends as
fast as the USB bus allows; Windows gives a port to one application at a
time.  This tool sends one 123-byte message at a time, at least --gap ms
apart (default 15: under a minute for a whole image), and nothing else.
While sending it watches the kernel's USB log and the port, and afterwards
waits for the unit to drop off USB and come back, which it does when the
bootloader starts the new firmware.

It also refuses a file the bootloader would choke on: a broken container,
missing or reordered messages, or an image over the 512 KiB the
bootloader accepts (it erases the application before it checks that).
"""
import argparse
import ctypes
import os
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import norand_syx                                  # noqa: E402

IMAGE_CAP = 0x80000          # bootloader 0x08026746 / 0x080264B2 / 0x08026C00
APP_BASE = 0x08100000
PORT_HINTS = ('mono', 'norand')


# ---------------------------------------------------------------------------
# the file
# ---------------------------------------------------------------------------

def validate(raw):
    """-> (messages, summary lines); raises ValueError on anything the
    bootloader would not take as a clean update"""
    try:
        msgs = norand_syx.split_messages(raw)
    except ValueError as e:
        raise ValueError('not a SysEx file: %s' % e)
    if not msgs:
        raise ValueError('empty file')
    for n, m in enumerate(msgs):
        if not m.startswith(norand_syx.SYSEX_PREFIX):
            raise ValueError('message %d is not a Norand Mono message' % n)
        if any(b & 0x80 for b in m[1:-1]):
            raise ValueError('message %d has a status byte inside it' % n)
    if msgs[0][6] in (0x7E, 0x7F):
        raise ValueError('this is a bootloader container (the primary bootloader '
                         'flashes it); this tool sends firmware updates only')
    sizes = set(len(m) for m in msgs)
    if sizes != {123}:
        raise ValueError('firmware messages are 123 bytes; this file has %s' % sorted(sizes))
    idx = [struct.unpack('>H', norand_syx.unpack7(m[6:10])[:2])[0] for m in msgs]
    total = idx[0]
    if total != len(msgs) - 1:
        raise ValueError('the first message announces %d more, the file has %d'
                         % (total, len(msgs) - 1))
    for n in range(1, len(msgs)):
        if idx[n] != n:
            raise ValueError('message %d carries index %d' % (n, idx[n]))
    packed = norand_syx.fw_unpack_packed(raw)
    if total * 112 + 0x16 >= IMAGE_CAP or len(packed) >= IMAGE_CAP:
        raise ValueError('the packed stream is over the bootloader\'s 512 KiB')
    try:
        image = norand_syx.lzss_decompress(packed)
    except Exception as e:                       # noqa: BLE001 -- any decode failure
        raise ValueError('the payload does not decompress: %s' % e)
    if len(image) > IMAGE_CAP:
        raise ValueError('the image is %d bytes, %d over the 524,288 the bootloader accepts '
                         '(it would erase the application, then refuse)'
                         % (len(image), len(image) - IMAGE_CAP))
    sp, reset = struct.unpack_from('<II', image, 0)
    if not (0x20000000 <= sp <= 0x20020000 and reset & 1
            and APP_BASE <= reset & ~1 < APP_BASE + len(image)):
        raise ValueError('the image does not start with a vector table '
                         '(SP 0x%08X, reset 0x%08X)' % (sp, reset))
    return msgs, ['%d messages, image %d bytes (%d under the cap), reset 0x%08X'
                  % (len(msgs), len(image), IMAGE_CAP - len(image), reset)]


# ---------------------------------------------------------------------------
# CoreMIDI
# ---------------------------------------------------------------------------

class CoreMidi:
    UTF8 = 0x08000100

    def __init__(self):
        c = ctypes
        self.cm = cm = c.CDLL('/System/Library/Frameworks/CoreMIDI.framework/CoreMIDI')
        self.cf = cf = c.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        cf.CFStringCreateWithCString.restype = c.c_void_p
        cf.CFStringCreateWithCString.argtypes = [c.c_void_p, c.c_char_p, c.c_uint32]
        cf.CFStringGetCString.argtypes = [c.c_void_p, c.c_char_p, c.c_long, c.c_uint32]
        cf.CFRelease.argtypes = [c.c_void_p]
        cf.CFRunLoopRunInMode.argtypes = [c.c_void_p, c.c_double, c.c_ubyte]
        cm.MIDIClientCreate.argtypes = [c.c_void_p, c.c_void_p, c.c_void_p, c.POINTER(c.c_uint32)]
        cm.MIDIOutputPortCreate.argtypes = [c.c_uint32, c.c_void_p, c.POINTER(c.c_uint32)]
        cm.MIDIGetNumberOfDestinations.restype = c.c_ulong
        cm.MIDIGetDestination.restype = c.c_uint32
        cm.MIDIGetDestination.argtypes = [c.c_ulong]
        cm.MIDIObjectGetStringProperty.argtypes = [c.c_uint32, c.c_void_p, c.POINTER(c.c_void_p)]
        cm.MIDIObjectGetIntegerProperty.argtypes = [c.c_uint32, c.c_void_p, c.POINTER(c.c_int32)]
        cm.MIDIEndpointGetEntity.argtypes = [c.c_uint32, c.POINTER(c.c_uint32)]
        cm.MIDIPacketListInit.restype = c.c_void_p
        cm.MIDIPacketListInit.argtypes = [c.c_void_p]
        cm.MIDIPacketListAdd.restype = c.c_void_p
        cm.MIDIPacketListAdd.argtypes = [c.c_void_p, c.c_ulong, c.c_void_p, c.c_uint64,
                                         c.c_ulong, c.c_char_p]
        cm.MIDISend.argtypes = [c.c_uint32, c.c_uint32, c.c_void_p]
        self.display_name = c.c_void_p.in_dll(cm, 'kMIDIPropertyDisplayName')
        self.offline_key = c.c_void_p.in_dll(cm, 'kMIDIPropertyOffline')
        self.default_mode = c.c_void_p.in_dll(cf, 'kCFRunLoopDefaultMode')
        self.client, self.port = c.c_uint32(), c.c_uint32()
        name = self.cfstr('norand-flash')
        self.check(cm.MIDIClientCreate(name, None, None, c.byref(self.client)), 'MIDIClientCreate')
        self.check(cm.MIDIOutputPortCreate(self.client, name, c.byref(self.port)),
                   'MIDIOutputPortCreate')
        self.buf = c.create_string_buffer(1024)

    @staticmethod
    def check(status, what):
        if status:
            raise OSError('%s failed: OSStatus %d' % (what, status))

    def cfstr(self, s):
        return self.cf.CFStringCreateWithCString(None, s.encode(), self.UTF8)

    def name(self, endpoint):
        s = ctypes.c_void_p()
        if self.cm.MIDIObjectGetStringProperty(endpoint, self.display_name, ctypes.byref(s)) or not s:
            return '?'
        out = ctypes.create_string_buffer(512)
        self.cf.CFStringGetCString(s, out, 512, self.UTF8)
        self.cf.CFRelease(s)
        return out.value.decode('utf-8', 'replace')

    def is_device(self, endpoint):
        """a port of a device (USB here), not a virtual one: only those
        belong to an entity"""
        entity = ctypes.c_uint32(0)
        return self.cm.MIDIEndpointGetEntity(endpoint, ctypes.byref(entity)) == 0 and entity.value != 0

    def gone(self, endpoint):
        """the destination left the list or went offline (its device was
        unplugged, reset or switched off)"""
        if endpoint not in [e for _, e in self.destinations(pump=0)]:
            return True
        v = ctypes.c_int32(0)
        if self.cm.MIDIObjectGetIntegerProperty(endpoint, self.offline_key, ctypes.byref(v)):
            return False                             # no such property (a virtual port)
        return bool(v.value)

    def destinations(self, pump=0.05):
        self.cf.CFRunLoopRunInMode(self.default_mode, pump, 1)   # take device add/remove notices
        n = self.cm.MIDIGetNumberOfDestinations()
        return [(self.name(self.cm.MIDIGetDestination(i)), self.cm.MIDIGetDestination(i))
                for i in range(n)]

    def send(self, endpoint, data):
        pl = ctypes.addressof(self.buf)
        pkt = self.cm.MIDIPacketListInit(pl)
        pkt = self.cm.MIDIPacketListAdd(pl, len(self.buf), pkt, 0, len(data), data)
        if not pkt:
            raise OSError('MIDIPacketListAdd: message too long')
        self.check(self.cm.MIDISend(self.port, endpoint, pl), 'MIDISend')


def pick(dests, want):
    if want:
        hits = [d for d in dests if want.lower() in d[0].lower()]
        exact = [d for d in hits if d[0].lower() == want.lower()]
        return exact or hits
    return [d for d in dests if any(h in d[0].lower() for h in PORT_HINTS)]


# ---------------------------------------------------------------------------
# what macOS itself says about the USB link
# ---------------------------------------------------------------------------

USB_NAMES = ('Mono', 'Norand')
# how the kernel names the unit: "Mono@01140000", "destroying 0x1209/df00/0100
# (Mono): ...", "enumerated 0x1209/df00/0100 (Mono / 4) at 12 Mbps"
USB_KEYS = ('Mono@', '(Mono', 'Norand@', '(Norand', '1209/df00')


def usb_route():
    """the USB devices between the Mac and the Mono, as ioreg shows them:
    [] on a port of the Mac itself, None when ioreg does not list it"""
    try:
        out = subprocess.run(['/usr/sbin/ioreg', '-p', 'IOUSB', '-w0'], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    stack = []
    for line in out.splitlines():
        i = line.find('+-o ')
        if i < 0:
            continue
        name = line[i + 4:].split('  <')[0].split('@')[0]
        del stack[i // 2:]
        stack.append(name)
        if name in USB_NAMES:
            return stack[2:-1]                       # below Root and the controller
    return None


class UsbLog:
    """the kernel's USB messages about the Mono, tagged with the message
    being sent when each arrived (log stream; no root needed)"""

    def __init__(self):
        self.lines, self.at, self.p = [], 0, None
        pred = 'process == "kernel" AND (%s)' % ' OR '.join(
            'eventMessage CONTAINS "%s"' % k for k in USB_KEYS)
        try:
            self.p = subprocess.Popen(['/usr/bin/log', 'stream', '--style', 'compact',
                                       '--predicate', pred], stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True)
        except OSError:
            return
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            if line.startswith(('Filtering', 'Timestamp')):
                continue
            self.lines.append((self.at, time.monotonic(), line.rstrip()))

    def since(self, k):
        return self.lines[k:]

    def stop(self):
        if self.p:
            self.p.terminate()


def short(line):
    """a kernel line without the timestamp/process columns"""
    for key in ('AppleUSB', 'IOUSBHost', 'usbaudiod', 'MIDIServer'):
        i = line.find(key)
        if i >= 0:
            return line[i:][:150]
    return line[:150]


# ---------------------------------------------------------------------------

def bar(done, total, t0):
    w = 32
    k = w * done // total
    sys.stdout.write('\r  [%s%s] %4d/%d  %4.1f s' % ('#' * k, '.' * (w - k), done, total,
                                                     time.monotonic() - t0))
    sys.stdout.flush()


def main(argv=None):
    ap = argparse.ArgumentParser(description='Send a Norand Mono firmware .syx over USB (macOS).')
    ap.add_argument('file', nargs='?', help='the .syx to send')
    ap.add_argument('--list', action='store_true', help='list MIDI destinations and exit')
    ap.add_argument('--check', action='store_true', help='validate the file and exit (no MIDI)')
    ap.add_argument('--port', help='destination name or part of it (default: the one named '
                                   'Mono / Norand)')
    ap.add_argument('--gap', type=float, default=15.0,
                    help='milliseconds between messages, at least 2 (default 15)')
    ap.add_argument('--wait', type=float, default=120.0,
                    help='seconds to wait for the port to appear (default 120)')
    ap.add_argument('--yes', action='store_true', help='do not ask before sending')
    a = ap.parse_args(argv)

    if a.gap < 2.0:
        ap.error('--gap below 2 ms leaves no margin for the bootloader\'s 1 ms ring drain')
    if not a.list and not a.file:
        ap.error('give a .syx file (or --list)')

    msgs = None
    if a.file:
        try:
            msgs, summary = validate(open(a.file, 'rb').read())
        except (OSError, ValueError) as e:
            print('refused: %s' % e)
            return 2
        print('%s: %s' % (os.path.basename(a.file), summary[0]))
        if a.check:
            return 0

    if sys.platform != 'darwin':
        print('sending needs macOS (CoreMIDI); on Windows use MIDI-OX, see README')
        return 2
    midi = CoreMidi()
    if a.list:
        for name, _ in midi.destinations():
            print('  %s' % name)
        return 0

    deadline = time.monotonic() + a.wait
    hits = pick(midi.destinations(), a.port)
    if not hits:
        print('waiting for the Mono\'s USB port%s...' % (' "%s"' % a.port if a.port else ''))
        print('  (hold TEMPO + FUNC while powering it on; Ctrl-C to stop)')
        while not hits and time.monotonic() < deadline:
            time.sleep(0.25)
            hits = pick(midi.destinations(), a.port)
        if not hits:
            print('no port found. Destinations now: %s'
                  % (', '.join(n for n, _ in midi.destinations()) or 'none'))
            return 1
    if len(hits) > 1:
        print('several ports match: %s -- pick one with --port' % ', '.join(n for n, _ in hits))
        return 1
    name, endpoint = hits[0]

    print('port: %s' % name)
    route = usb_route() if midi.is_device(endpoint) else None
    if route:
        print('  USB: through %s. A hub or dock is the first suspect if the update stops;'
              % ' > '.join(route))
        print('       a cable straight into the Mac is safer.')
    print('  NOTE: on macOS 15 the stock bootloader has hung part-way, faulting on a USB')
    print('  control request from macOS itself (docs/protocol.md, section 8).  Windows is')
    print('  the reliable way to flash for now.')
    print('  The Mono must be in its bootloader: hold TEMPO + FUNC while powering it on.')
    print('  Quit DAWs, editors and vendor agents that talk to MIDI gear: the bootloader')
    print('  takes anything another app sends to this port as part of the update.  If a')
    print('  send stops or fails, power-cycle into the bootloader before resending (it')
    print('  keeps its write position).')
    if not a.yes:
        try:
            input('press Enter to send, Ctrl-C to cancel ')
        except (KeyboardInterrupt, EOFError):
            print('\ncancelled, nothing sent')
            return 1

    # the kernel log only helps when ioreg sees the unit (not for a virtual port)
    log = UsbLog() if route is not None else None
    if log:
        time.sleep(1.0)                          # log stream takes a moment to attach
    gap = a.gap / 1000.0
    t0 = last = time.monotonic()
    worst = 0.0
    try:
        for n, m in enumerate(msgs):
            now = time.monotonic()
            if n and now < last + gap:
                time.sleep(last + gap - now)        # never early (PEP 475)
            now = time.monotonic()
            if n:
                worst = max(worst, now - last)
            last = now
            if log:
                log.at = n
            midi.send(endpoint, m)
            if n % 16 == 0 or n == len(msgs) - 1:
                bar(n + 1, len(msgs), t0)
            if n % 64 == 63 and midi.gone(endpoint):
                print('\nthe Mono\'s USB port went away at message %d of %d (unplugged, reset or '
                      'power).' % (n + 1, len(msgs)))
                if log:
                    report(log, len(msgs))
                return 1
    except KeyboardInterrupt:
        print('\nstopped after %d of %d messages: nothing was flashed. Power-cycle into the '
              'bootloader before sending again.' % (n, len(msgs)))
        if log:
            log.stop()
        return 1
    print('\nsent in %.1f s (longest pause between messages %.0f ms).'
          % (time.monotonic() - t0, worst * 1000))
    if not log or log.p is None:
        time.sleep(0.5)                          # let the MIDI server hand the tail on
        print('The SEQ LEDs fill, the Mono flashes and restarts.')
        print('If the LEDs stopped filling early, power-cycle into the bootloader and send again.')
        return 0
    sent_lines = len(log.lines)
    report(log, len(msgs), stop=False)
    # a good update ends with the bootloader erasing bank 2, writing it and
    # jumping to the new firmware, which drops off USB and comes back
    print('waiting for the Mono to flash and restart (up to %d s)...' % RESTART_S)
    deadline = time.monotonic() + RESTART_S
    gone = back = False
    try:
        while time.monotonic() < deadline and not back:
            time.sleep(0.25)
            gone, back = restarted(line for _, _, line in log.since(sent_lines))
    except KeyboardInterrupt:
        print()
    log.stop()
    if back:
        print('it dropped off USB and came back, as it does when the bootloader starts the new')
        print('firmware.  Done.')
        return 0
    if gone:
        print('it dropped off USB and has not come back within %d s; check the unit.' % RESTART_S)
        return 1
    print('no restart within %d s.' % RESTART_S if time.monotonic() >= deadline else 'no restart seen.')
    print('  SEQ LEDs stopped part-way: the bootloader stopped taking data.  Nothing was')
    print('  erased; a normal power-on starts the old firmware.')
    print('  All 16 lit: it took the whole file but did not start it (the application is')
    print('  erased by then).')
    print('  Either way: power-cycle into the bootloader and send again, with the cable')
    print('  straight into the Mac and --gap 30.')
    return 1


RESTART_S = 60     # bank 2 erase (8 x 128 KiB sectors) + 16k flash words + decode


def restarted(lines):
    """(dropped off USB, and came back after that) from kernel lines"""
    gone = back = False
    for line in lines:
        gone = gone or 'terminateDevice' in line
        back = back or (gone and 'enumerated' in line)
    return gone, back


def report(log, total, stop=True):
    """the kernel's USB messages during the send, by message number"""
    during = [(at, line) for at, _, line in log.lines]
    if stop:
        log.stop()
    if not during:
        return
    print('kernel USB messages during the send:')
    for at, line in during[:12]:
        print('  at message %4d/%d: %s' % (at + 1, total, short(line)))
    if len(during) > 12:
        print('  ... %d more' % (len(during) - 12))


if __name__ == '__main__':
    sys.exit(main())
