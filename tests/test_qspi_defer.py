#!/usr/bin/env python3
"""Q1: a flash-touching paste waits until the QSPI queue is idle.

FUNC+RECORD saves asynchronously: a coroutine on the QSPI driver's queue
erases (interrupt-driven status poll, holding the driver's Borrowable
read-modify-write buffer across the wait) and then programs.  A project
paste (or a patch / pattern / mod-note / patch-parameter paste from another
project) goes to the flash synchronously, from the project service.  If it
arrives while the save is still in flight, the stock's erase finds the
buffer borrowed: "Borrowable already borrowed", abort(), `b .` in main --
the panel freezes for good (the stock has no watchdog; this firmware's
16 s watchdog turns it into a reboot).

sysemu with the asynchronous QSPI model (tests/sysemu.py), the erase made
to last 50 ms so the paste lands inside it:

  1. stock: FUNC+RECORD, then a project paste -> main ends in abort's `b .`;
  2. this image, the same -> the paste is held back while the queue is
     busy, then runs: both slots hold the project, nothing aborts;
  3. this image, a paste with the queue idle -> not held back at all;
  4. this image with the stock loop head restored -> the hang is back
     (the check in 2 is the patch, not the scenario).
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sysemu
from unicorn import UC_HOOK_CODE
from test_modnote_ui import booted

IMAGE = 'build/firmware-patched.bin'
STOCK = 'build/firmware.bin'
D = 0x2000E400
D_QSPI_DEFER = 0xFA4
ABORT_LOOP = 0x0815A6E0               # _exit: `b .`, reached from abort()
COPY_PASTE_PROJECT = 0x08170320       # ProjectEvent<CopyPasteProjectData> vptr
SLOT_DEST = 5
LOOP_HEAD = 0x08157672
ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print('  %-66s ok' % label)
    else:
        fail += 1
        print('  %-66s FAIL got %r want %r' % (label, got, want))


def scenario(image, save_first, stock_head=False):
    m = booted(image)
    uc = m.uc
    if stock_head:
        uc.mem_write(LOOP_HEAD, bytes.fromhex('e3699a68'))
        uc.ctl_flush_tb()
    m.enable_qspi_async(delay_slices=200)          # a 50 ms erase
    hits = {'abort': 0}

    def aborted(u, a, s, d):
        hits['abort'] += 1
        u.emu_stop()
    uc.hook_add(UC_HOOK_CODE, aborted, begin=ABORT_LOOP, end=ABORT_LOOP)
    uc.ctl_flush_tb()
    mgr = m.call_preserving(0x0811F854)
    m.tap(4); m.tap(8); m.settle(40)               # some content
    uc.mem_write(D + D_QSPI_DEFER, b'\0' * 4)
    if save_first:
        m.press(26); m.settle(20); m.tap(32, hold_ms=40, gap_ms=10); m.release(26)
    busy_at_paste = m.qspi_queue_len()
    m.submit_event(COPY_PASTE_PROJECT, struct.pack('<HBB', 0, 1, SLOT_DEST))
    for _ in range(40):
        m.settle(10)
        if hits['abort']:
            break
    deferred = struct.unpack('<I', bytes(uc.mem_read(D + D_QSPI_DEFER, 4)))[0]
    n = struct.unpack('<I', bytes(uc.mem_read(mgr + 0x728, 4)))[0] + 0x100
    store = bytes(uc.mem_read(0x24000000, n))
    dest = bytes(uc.mem_read(0x90000000 + (SLOT_DEST << 19), n))
    return dict(abort=hits['abort'] > 0, busy=busy_at_paste, deferred=deferred,
                queue=m.qspi_queue_len() if not hits['abort'] else None,
                dest_ok=dest == store, errors=len(m.errors))


def main():
    print('1. stock image: FUNC+RECORD, then a project paste mid-erase')
    r = scenario(STOCK, save_first=True)
    check('the save is still in flight when the paste arrives', r['busy'], 1)
    check('main ends in abort() ("Borrowable already borrowed")', r['abort'], True)

    print('2. this image, the same')
    r = scenario(IMAGE, save_first=True)
    check('the save is still in flight when the paste arrives', r['busy'], 1)
    check('no abort', r['abort'], False)
    check('the paste was held back while the queue was busy', r['deferred'] > 0, True)
    check('the queue drained', r['queue'], 0)
    check('the destination slot holds the project', r['dest_ok'], True)
    check('no Error_Handler', r['errors'], 0)

    print('3. this image, a paste with the queue idle')
    r = scenario(IMAGE, save_first=False)
    check('the queue is idle', r['busy'], 0)
    check('not held back', r['deferred'], 0)
    check('the destination slot holds the project', r['dest_ok'], True)

    print('4. this image, stock loop head restored')
    r = scenario(IMAGE, save_first=True, stock_head=True)
    check('the hang is back', r['abort'], True)
    print('%s -- a flash-touching paste waits for the QSPI queue (%d checks)'
          % ('PASS' if fail == 0 else 'FAIL', ok + fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
