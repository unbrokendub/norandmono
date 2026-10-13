#!/usr/bin/env python3
"""S1/S2: the pattern store re-sorts a step only when it is out of order,
and main() skips its pass over the store when that pass would find nothing.

main() commits pending step edits through 0x0815888C on every iteration,
and 0x081585C0 ends each step with std::list::sort (0x081584AC) whether or
not anything was committed, so an unchanged pattern is re-sorted on every
pass.  np_list_sort walks the list once and calls the stock sort only when
a key is below its predecessor.

1. Unit: random lists (sizes 0..40, keys with many ties, sorted, nearly
   sorted and shuffled) are built twice at the same addresses; one copy goes
   through the stock sort, the other through np_list_sort.  Every byte of
   the head and of every node must come out the same.
2. S2 unit: on a real locked pattern an idle np_seq_worker writes nothing
   at all, and each condition that gives the stock pass work -- a clear, a
   rotation, a pending edit, a step out of order -- reaches 0x0815888C.
3. System: the same UI session -- twelve notes, then four parameter locks
   per step swept in with the pots -- run on sysemu with S1+S2, with S1
   alone and with the stock calls restored in the same image.  Every step's
   events, in order, must be identical, and the main loop's own instruction
   count per pass is reported for each.
"""
import random
import struct
import sys

sys.path.insert(0, 'tests')
import sysemu
from unicorn import UC_HOOK_CODE, UC_HOOK_BLOCK
from unicorn.arm_const import UC_ARM_REG_IPSR
from test_modnote_ui import booted

IMAGE = 'build/firmware-patched.bin'
SORT_SITE = 0x08158708
STOCK_SORT = 0x081584AC
WORKER_SITES = ((0x08146E76, 'bl'), (0x08146E82, 'b'))
STOCK_WORKER = 0x0815888C
SEQ = 0x2000CFF4
ARENA = 0x70000000                      # unmapped in the image: ours alone
ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print("  %-60s ok" % label)
    else:
        fail += 1
        print("  %-60s FAIL got %r want %r" % (label, got, want))


def thumb_bl(site, target):
    off = target - (site + 4)
    s = (off >> 24) & 1
    i1 = (off >> 23) & 1
    i2 = (off >> 22) & 1
    j1 = (~i1 ^ s) & 1
    j2 = (~i2 ^ s) & 1
    hw1 = 0xF000 | (s << 10) | ((off >> 12) & 0x3FF)
    hw2 = 0xD000 | (j1 << 13) | (j2 << 11) | ((off >> 1) & 0x7FF)
    return struct.pack('<HH', hw1, hw2)


def thumb_b_w(site, target):
    hw1, hw2 = struct.unpack('<HH', thumb_bl(site, target))
    return struct.pack('<HH', hw1, hw2 & ~0x4000)       # BL -> B.W: clear bit 14


def restore_stock(m, sort=True, worker=True):
    if sort:
        m.uc.mem_write(SORT_SITE, thumb_bl(SORT_SITE, STOCK_SORT))
    if worker:
        for site, kind in WORKER_SITES:
            enc = thumb_bl if kind == 'bl' else thumb_b_w
            m.uc.mem_write(site, enc(site, STOCK_WORKER))
    m.uc.ctl_flush_tb()


def bl_target(uc, site):
    hw1, hw2 = struct.unpack('<HH', bytes(uc.mem_read(site, 4)))
    s = (hw1 >> 10) & 1
    j1, j2 = (hw2 >> 13) & 1, (hw2 >> 11) & 1
    i1, i2 = 1 - (j1 ^ s), 1 - (j2 ^ s)
    off = (s << 24) | (i1 << 23) | (i2 << 22) | ((hw1 & 0x3FF) << 12) | ((hw2 & 0x7FF) << 1)
    if s:
        off -= 1 << 25
    return site + 4 + off


def write_list(uc, keys, order):
    """std::list at ARENA: head {next, prev, size}, nodes of 20 bytes
    {next, prev, key, tag, pad, payload}; `order` places the nodes at
    scattered addresses so links, not addresses, carry the order."""
    head = ARENA
    nodes = [ARENA + 0x100 + 0x20 * slot for slot in order]
    uc.mem_write(ARENA, b'\0' * 0x2000)
    chain = [head] + nodes + [head]
    for i, n in enumerate(nodes):
        uc.mem_write(n, struct.pack('<IIBBHI', chain[i + 2], chain[i], keys[i], i & 0xFF, 0, 0xC0DE0000 | i))
    first = nodes[0] if nodes else head
    last = nodes[-1] if nodes else head
    uc.mem_write(head, struct.pack('<III', first, last, len(nodes)))
    return head


def unit(m, rng):
    uc = m.uc
    uc.mem_map(ARENA, 0x2000)
    patched = bl_target(uc, SORT_SITE)
    check("0x08158708 calls the replacement, not the stock sort",
          patched != STOCK_SORT, True)
    cases = 0
    skipped_cost = []
    stock_cost = []
    for size in list(range(0, 12)) + [16, 24, 40]:
        for kind in ('sorted', 'nearly', 'shuffled', 'ties'):
            for rep in range(6):
                if kind == 'ties':
                    keys = [rng.randrange(2) for _ in range(size)]
                else:
                    keys = sorted(rng.randrange(8) for _ in range(size))
                    if kind == 'nearly' and size >= 2:
                        i = rng.randrange(size - 1)
                        keys[i], keys[i + 1] = keys[i + 1], keys[i]
                    elif kind == 'shuffled':
                        rng.shuffle(keys)
                order = list(range(size))
                rng.shuffle(order)
                results = []
                for fn in (STOCK_SORT, patched):
                    write_list(uc, keys, order)
                    n0 = [0]
                    h = uc.hook_add(UC_HOOK_CODE, lambda *a: n0.__setitem__(0, n0[0] + 1))
                    uc.ctl_flush_tb()
                    m.call_preserving(fn, ARENA)
                    uc.hook_del(h)
                    results.append(bytes(uc.mem_read(ARENA, 0x2000)))
                    if kind == 'sorted' and size == 8:
                        (stock_cost if fn == STOCK_SORT else skipped_cost).append(n0[0])
                cases += 1
                if results[0] != results[1]:
                    check("%s list of %d (rep %d) identical to the stock sort" % (kind, size, rep),
                          False, True)
                    return
    check("%d random lists: every byte identical to the stock sort" % cases, True, True)
    print("  an 8-event step already in order: stock sort %d instructions, "
          "np_list_sort %d" % (stock_cost[0], skipped_cost[0]))
    uc.mem_unmap(ARENA, 0x2000)


def listing(m):
    """Every step of both tracks, in list order: each 16-byte node's key
    byte (+8) and the event it owns (+0xC) by vtable, parameter word and
    value.  Left out, because they depend on allocation order and so on
    main-loop timing rather than on what was recorded: the owning pointer
    itself, the node's three padding bytes after the key, and the event's
    byte +9 -- uninitialised, it carries whatever the reused heap block
    held (0x60, 0xB1, ... in one run, a stale parameter index in another)."""
    uc = m.uc
    u32 = lambda a: struct.unpack('<I', bytes(uc.mem_read(a, 4)))[0]
    out = []
    for track in (SEQ + 0x8C, SEQ + 0x6C8):
        for step in range(64):
            head = track + step * 0xC
            p, items = u32(head), []
            while p != head and len(items) < 1000:
                ev = u32(p + 0xC)
                body = bytes(uc.mem_read(ev, 12)) if 0x20000000 <= ev < 0x38000000 else b''
                body = body[:9] + body[10:]
                items.append((bytes(uc.mem_read(p + 8, 1)), body))
                p = u32(p)
            out.append(items)
    return out


def session(m):
    """Every pot move is given time to settle completely -- the scanner's
    32-sample average takes ~64 ms and main() must have drained every event
    before the step key is let go -- so the result does not depend on how
    fast main() runs, which is the one thing S1 changes.  (With 25 ms per
    move the slower stock-sort loop applied the tail of a sweep after the
    release, to the patch instead of the lock.)"""
    for k in range(4, 16):                      # a note on each of 12 steps
        m.tap(k, hold_ms=20, gap_ms=40)
    m.settle(40)
    for step in range(4, 16):                   # four parameter locks per step
        m.press(step)
        m.settle(40)
        for pot in (0, 1, 9, 12):
            m.pot_values[pot] = 0x2000 + (step * 0x0700 + pot * 0x1100) % 0xC000
            m.settle(110)
        m.settle(40)
        m.release(step)
        m.settle(40)
    m.settle(100)


def main_cost(m, ms=60):
    """Thread-mode instructions per main-loop pass (loop top 0x08119A02)."""
    uc = m.uc
    st = {'n': 0, 'passes': []}

    def blk(uc_, addr, size, data):
        if uc_.reg_read(UC_ARM_REG_IPSR) & 0x1FF:
            return
        st['n'] += size // 2 - (size // 8)      # cheap estimate, same for both runs

    def top(uc_, addr, size, data):
        if uc_.reg_read(UC_ARM_REG_IPSR) & 0x1FF:
            return
        st['passes'].append(st['n'])
        st['n'] = 0
    h1 = uc.hook_add(UC_HOOK_BLOCK, blk)
    h2 = uc.hook_add(UC_HOOK_CODE, top, begin=0x08119A02, end=0x08119A02)
    uc.ctl_flush_tb()
    m.settle(ms)
    uc.hook_del(h1)
    uc.hook_del(h2)
    passes = sorted(st['passes'][1:])
    return passes[len(passes) // 2]


def worker_unit(m):
    """S2 on the pattern a session left behind."""
    uc = m.uc
    worker = bl_target(uc, WORKER_SITES[0][0])
    check("0x08146E76 calls the idle-pass skip", worker != STOCK_WORKER, True)
    seq_lo, seq_hi = SEQ, SEQ + 0x6C8 + 0x640
    heap = (0x30000000, 0x3003C000)
    reached = [0]
    h = uc.hook_add(UC_HOOK_CODE, lambda *a: reached.__setitem__(0, reached[0] + 1),
                    begin=STOCK_WORKER, end=STOCK_WORKER)
    uc.ctl_flush_tb()
    m.run_unmasked()                    # so call_preserving runs no main code first
    for track in (SEQ + 0x8C, SEQ + 0x6C8):
        before = (bytes(uc.mem_read(seq_lo, seq_hi - seq_lo)), bytes(uc.mem_read(*heap_range(heap))))
        reached[0] = 0
        m.call_preserving(worker, track)
        after = (bytes(uc.mem_read(seq_lo, seq_hi - seq_lo)), bytes(uc.mem_read(*heap_range(heap))))
        check("idle track +0x%X: nothing written, stock pass not entered" % (track - SEQ),
              (before == after, reached[0]), (True, 0))
    snap = m.snapshot()
    track = SEQ + 0x8C
    u32 = lambda a: struct.unpack('<I', bytes(uc.mem_read(a, 4)))[0]
    def events(i):
        head, n, p = track + i * 0xC, 0, u32(track + i * 0xC)
        while p != head:
            n, p = n + 1, u32(p)
        return n
    first = next(i for i in range(64) if events(i) >= 2)
    head = track + first * 0xC
    node = u32(head)
    PEND = ARENA + 0x10000
    uc.mem_map(PEND, 0x1000)

    def clear(): uc.mem_write(track + 0x635, b'\x01')

    def rotate(): uc.mem_write(track + 0x632, bytes([uc.mem_read(track + 0x633, 1)[0] ^ 1]))

    def pending():                              # a 20-byte pending node on step `first`
        ph = head + 0x300
        pn = PEND
        uc.mem_write(pn, struct.pack('<IIBBBBI', ph, ph, 0, 0, 0, 0, 0) + b'\0' * 4)
        uc.mem_write(ph, struct.pack('<III', pn, pn, 1))

    def disorder():                             # the second event's key below the first
        second = u32(node)
        uc.mem_write(node + 8, b'\x81')
        uc.mem_write(second + 8, b'\x80')
    for label, poke in (('a clear', clear), ('a rotation', rotate),
                        ('a pending edit', pending), ('a step out of order', disorder)):
        m.restore(snap)
        uc = m.uc
        m.run_unmasked()
        poke()
        reached[0] = 0
        stop = uc.hook_add(UC_HOOK_CODE, lambda uc_, *a: uc_.emu_stop(),
                           begin=STOCK_WORKER, end=STOCK_WORKER)
        uc.ctl_flush_tb()
        try:
            m.call_preserving(worker, track, count=200000)
        except AssertionError:
            pass                                # stopped inside the stock pass
        uc.hook_del(stop)
        check("%s reaches the stock pass" % label, reached[0] >= 1, True)
    uc.hook_del(h)
    m.restore(snap)
    m.uc.mem_unmap(PEND, 0x1000)


def heap_range(heap):
    return heap[0], heap[1] - heap[0]


def playback(m, ms=1700):
    """PLAY (key 27) and record every note-on (0x081170E8) and note-off
    (0x08117594) by the transport position main() had reached when it fired,
    plus the CNoteData note value.  main() is faster with S1/S2, so the wall
    time of a step moves; its position in the pattern must not."""
    uc = m.uc
    log = []
    u32 = lambda a: struct.unpack('<I', bytes(uc.mem_read(a, 4)))[0]

    def hit(uc_, addr, size, data):
        ev = uc_.reg_read(sysemu.UC_ARM_REG_R1)
        note = bytes(uc_.mem_read(ev + 8, 4)) if 0x20000000 <= ev < 0x38000000 else b''
        log.append(('on' if addr == 0x081170E8 else 'off', u32(0x2000AF78 + 0xC), note))
    hs = [uc.hook_add(UC_HOOK_CODE, hit, begin=a, end=a) for a in (0x081170E8, 0x08117594)]
    uc.ctl_flush_tb()
    m.tap(27, hold_ms=20, gap_ms=10)
    m.settle(ms)
    for h in hs:
        uc.hook_del(h)
    start = next((pos for kind, pos, _ in log if kind == 'on'), 0)
    return [(kind, pos - start, note) for kind, pos, note in log]


def system():
    results = {}
    for label, sort, worker in (('S1+S2', False, False), ('S1', False, True),
                                ('stock', True, True)):
        m = booted(IMAGE)
        if sort or worker:
            restore_stock(m, sort=sort, worker=worker)
        session(m)
        results[label] = (listing(m), main_cost(m))
        if label == 'S1+S2':
            snap = m.snapshot()
            worker_unit(m)
            m.restore(snap)
        if label in ('S1+S2', 'stock'):
            results[label] += (playback(m),)
    a = results['S1+S2'][0]
    events = sum(len(s) for s in a)
    check("the session produced a locked pattern (%d events)" % events, events >= 12 * 4, True)
    for other in ('S1', 'stock'):
        check("every step of both tracks identical: S1+S2 vs %s" % other,
              a == results[other][0], True)
    pa, pb = results['S1+S2'][2], results['stock'][2]
    ons = sum(1 for kind, _, _ in pa if kind == 'on')
    check("the pattern played (%d note-ons, %d events)" % (ons, len(pa)), ons >= 12, True)
    check("every note-on/off at the same pattern position: S1+S2 vs stock", pa, pb)
    print("  main-loop pass, median (block-size estimate): stock %d, S1 %d, S1+S2 %d"
          % (results['stock'][1], results['S1'][1], results['S1+S2'][1]))
    check("each step makes the idle main-loop pass cheaper",
          results['S1+S2'][1] < results['S1'][1] < results['stock'][1], True)


def main():
    rng = random.Random(0x5EC5)
    m = booted(IMAGE)
    unit(m, rng)
    system()
    print("%s -- the pattern store sorts only what is out of order (%d checks)"
          % ("PASS" if fail == 0 else "FAIL", ok + fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
