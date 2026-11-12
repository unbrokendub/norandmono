#!/usr/bin/env python3
"""tools/flash.py through the real CoreMIDI, no hardware (macOS only).

Creates a virtual MIDI destination, runs tools/flash.py against it as a
separate process, and checks what arrived:

  1. the bytes are the file's, byte for byte, in the file's messages;
  2. no two messages arrived closer than the gap allows (a little slack for
     the receive thread), so the bootloader's 128-packet ring never holds
     more than one message per 1 ms drain;
  3. nothing else arrived (no clock, no stray bytes);
  4. a file the bootloader would refuse is refused before anything is sent;
  5. the port going away mid-send stops flash.py with the message number;
  6. the restart watch reads kernel lines from a real session right.

    /usr/bin/python3 tests/test_flash_coremidi.py [file.syx]
"""
import ctypes
import os
import platform
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SINK = 'norand-flash-test-sink'
GAP_MS = 4.0
ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print('  %-72s ok' % label)
    else:
        fail += 1
        print('  %-72s FAIL got %r want %r' % (label, got, want))


class Sink:
    """a virtual destination that records (arrival time, bytes) per packet"""

    def __init__(self):
        c = ctypes
        cm = c.CDLL('/System/Library/Frameworks/CoreMIDI.framework/CoreMIDI')
        cf = c.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        cf.CFStringCreateWithCString.restype = c.c_void_p
        cf.CFStringCreateWithCString.argtypes = [c.c_void_p, c.c_char_p, c.c_uint32]
        cm.MIDIClientCreate.argtypes = [c.c_void_p, c.c_void_p, c.c_void_p, c.POINTER(c.c_uint32)]
        self.READ = c.CFUNCTYPE(None, c.c_void_p, c.c_void_p, c.c_void_p)
        cm.MIDIDestinationCreate.argtypes = [c.c_uint32, c.c_void_p, self.READ, c.c_void_p,
                                             c.POINTER(c.c_uint32)]
        cm.MIDIEndpointDispose.argtypes = [c.c_uint32]
        self.cm = cm
        self.got = []
        self.lock = threading.Lock()
        self.align = platform.machine() in ('arm64', 'aarch64')
        self.cb = self.READ(self.read)
        self.client, self.dest = c.c_uint32(), c.c_uint32()
        name = cf.CFStringCreateWithCString(None, SINK.encode(), 0x08000100)
        assert cm.MIDIClientCreate(name, None, None, c.byref(self.client)) == 0
        assert cm.MIDIDestinationCreate(self.client, name, self.cb, None, c.byref(self.dest)) == 0

    def read(self, pktlist, ref, src):
        t = time.monotonic()
        n = ctypes.c_uint32.from_address(pktlist).value
        p = pktlist + 4                              # MIDIPacketList is pack(4)
        out = []
        for _ in range(n):
            length = ctypes.c_uint16.from_address(p + 8).value
            out.append(ctypes.string_at(p + 10, length))
            p += 10 + length
            if self.align:                           # MIDIPacketNext on ARM
                p = (p + 3) & ~3
        with self.lock:
            self.got.extend((t, d) for d in out)

    def close(self):
        self.cm.MIDIEndpointDispose(self.dest)


def messages(chunks):
    """reassemble SysEx messages with the arrival time of each one's end"""
    msgs, cur, stray = [], bytearray(), bytearray()
    for t, d in chunks:
        for b in d:
            if b == 0xF0:
                cur = bytearray([b])
            elif cur:
                cur.append(b)
                if b == 0xF7:
                    msgs.append((t, bytes(cur)))
                    cur = bytearray()
            else:
                stray.append(b)
    return msgs, bytes(stray)


def flash(*args):
    return subprocess.run([sys.executable, os.path.join(ROOT, 'tools/flash.py')] + list(args),
                          capture_output=True, text=True, timeout=600)


def main():
    if sys.platform != 'darwin':
        print('SKIP -- needs macOS')
        return 0
    syx = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'build/mono_V2.2.0r-patched.syx')
    raw = open(syx, 'rb').read()
    sink = Sink()
    time.sleep(0.3)

    print('1. sending %s to a virtual port, --gap %g' % (os.path.basename(syx), GAP_MS))
    lst = flash('--list')
    check('flash.py --list shows the sink', SINK in lst.stdout, True)
    t0 = time.monotonic()
    r = flash('--port', SINK, '--gap', str(GAP_MS), '--yes', syx)
    took = time.monotonic() - t0
    time.sleep(0.5)
    print('    ' + '\n    '.join(r.stdout.strip().splitlines()[-2:]))
    check('flash.py exit status', r.returncode, 0)
    with sink.lock:
        chunks = list(sink.got)
    msgs, stray = messages(chunks)
    want = []
    i = 0
    while i < len(raw):
        j = raw.index(0xF7, i)
        want.append(raw[i:j + 1])
        i = j + 1
    check('messages arrived: %d of %d' % (len(msgs), len(want)), len(msgs), len(want))
    check('the bytes are the file\'s', b''.join(m for _, m in msgs) == raw, True)
    check('nothing else arrived (%d stray bytes)' % len(stray), len(stray), 0)

    print('2. pacing')
    times = [t for t, _ in msgs]
    gaps = sorted((b - a) * 1000 for a, b in zip(times, times[1:]))
    if gaps:
        pct = gaps[len(gaps) // 100]
        print('    gap between arrivals: min %.2f, 1st percentile %.2f, median %.2f ms; %.1f s in all'
              % (gaps[0], pct, gaps[len(gaps) // 2], took))
        # four messages inside one 1 ms drain overflow the ring: no 1 ms window may hold more than 3
        worst = max(sum(1 for u in times[k:k + 8] if u - times[k] < 0.001) for k in range(len(times)))
        check('most messages inside any 1 ms window: %d (the ring takes 3)' % worst, worst <= 3, True)
        check('median gap at least --gap (%.2f ms)' % gaps[len(gaps) // 2],
              gaps[len(gaps) // 2] >= GAP_MS * 0.95, True)

    print('3. refusals before sending')
    broken = os.path.join(ROOT, 'build/BROKEN-do-not-flash-p43-525KB.syx')
    if os.path.exists(broken):
        n0 = len(sink.got)
        r = flash('--port', SINK, '--yes', broken)
        time.sleep(0.3)
        check('the 525 KB p43 image: exit %d, %d bytes sent' % (r.returncode, len(sink.got) - n0),
              (r.returncode, len(sink.got) - n0), (2, 0))
    n0 = len(sink.got)
    cut = raw[:raw.index(0xF7, len(raw) // 2) + 1]           # the first half of the file
    tmp = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'norand-flash-test-half.syx')
    open(tmp, 'wb').write(cut)
    r = flash('--port', SINK, '--yes', tmp)
    os.unlink(tmp)
    time.sleep(0.3)
    check('half a file: exit %d, %d bytes sent' % (r.returncode, len(sink.got) - n0),
          (r.returncode, len(sink.got) - n0), (2, 0))
    sink.close()

    print('4. the port going away mid-send')
    gone = Sink()
    time.sleep(0.3)
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, 'tools/flash.py'), '--port', SINK,
                             '--gap', str(GAP_MS), '--yes', syx], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    time.sleep(2.5)
    gone.close()                                             # as an unplugged or crashed unit
    out = proc.communicate(timeout=120)[0]
    got = len(messages(gone.got)[0])
    check('flash.py stops: exit %d, "went away" reported, %d of %d sent'
          % (proc.returncode, got, len(want)),
          (proc.returncode, 'went away' in out, got < len(want)), (1, True, True))

    print('5. the restart watch, on kernel lines from a real session')
    sys.path.insert(0, os.path.join(ROOT, 'tools'))
    import flash as tool
    term = ('AppleUSB20HubPort@01140000: AppleUSBHostPort::terminateDevice: destroying '
            '0x1209/df00/0100 (Mono): hardware connection lost')
    enum = ('AppleUSB20HubPort@01140000: AppleUSBHostPort::enumerateDeviceComplete_block_invoke: '
            'enumerated 0x1209/df00/0100 (Mono / 4) at 12 Mbps')
    stall = ('AppleUSBIORequest::complete: device 4 (Mono@01140000) endpoint 0x01: status '
             '0xe0005000 (pipe stalled): 0 bytes transferred')
    check('dropped off and came back', tool.restarted([stall, term, enum]), (True, True))
    check('dropped off only', tool.restarted([stall, term]), (True, False))
    check('came back without dropping off does not count', tool.restarted([enum]), (False, False))
    check('every kernel line above matches the log filter',
          all(any(k in x for k in tool.USB_KEYS) for x in (term, enum, stall)), True)
    print('%s -- flash.py over CoreMIDI (%d checks)' % ('PASS' if fail == 0 else 'FAIL', ok + fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
