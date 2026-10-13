#!/usr/bin/env python3
"""
diagmidi.py -- decode the patched firmware's MIDI telemetry.

The patched firmware sends its diagnostic block as control changes on MIDI
channel 16 (see patch/np_runtime.cpp, "Telemetry over MIDI"):

    CC 119 = record tag      1 boot record, 2 PC ring, 3 heartbeat
    CC 102..106              one 32-bit word, MSB first: 4 + 7 + 7 + 7 + 7 bits

Record a session with a MIDI monitor (MIDI Monitor on macOS: File > Save,
or copy the table), then:

    python3 tools/diagmidi.py capture.mmon         # a MIDI Monitor document
    python3 tools/diagmidi.py capture.txt          # or its saved text
    python3 tools/diagmidi.py capture.mid          # or a Standard MIDI File

Lines are matched loosely: any line that mentions a control change on
channel 16 with a controller number and a value is used; the rest is ignored.
PC samples are symbolised against build/firmware.symbols.json and the blob's
symbol table (build/np_patch.elf via arm-none-eabi-nm when available).
"""
import bisect
import collections
import json
import os
import re
import struct
import subprocess
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

TAG, DATA0 = 119, 102
BOOT, RING, HEARTBEAT, LEDS = 1, 2, 3, 4
P6 = 5
TR = 6                             # the one-second transition record
EV = 7                             # X-Env event log (2026-10-02)
PM = 8                             # pattern map, every 5 s
FULL_INDEX = (P6, TR, EV, PM)      # records whose words carry their full index
EXPECTED = {BOOT: 28, RING: 256, HEARTBEAT: 59, LEDS: 18, P6: 128, TR: 18, EV: 52, PM: 52}
SLOT_NAMES = ["VCO1Tune", "VCO1Fine", "VCO1Wave", "VCO1Vol", "VCO2Tune", "VCO2Fine",
              "VCO2Wave", "VCO2Vol", "FM", "Cutoff", "Reso", "Color", "FiltEnv",
              "EnvAttack", "EnvDecay", "EnvSust", "EnvRel", "OutVol", "Sync"]
EV_TYPES = {1: "TRIG from NOTE ", 2: "TRIG from PARAM", 3: "TRIG other     ",
            4: "RC attack      ", 5: "RC decay-restart"}
P6_WORDS = {1: 95, 2: 99, 3: 106, 4: 108, 5: 120, 6: 123, 7: 128, 8: 128}  # CRC last
P6_FIELDS = ['magic', 'schema', 'build_id', 'seq', 'uptime_ms', 'snapshot_cycles',
    'priorities_irq8_irq11_irq50_systick', 'main_iterations', 'main_last_period',
    'main_period_max', 'audio_frames', 'audio_last_cycles', 'audio_max_cycles',
    'audio_overruns', 'systick_calls', 'systick_max_cycles', 'scanner_calls',
    'scanner_max_cycles', 'ui_enqueue_attempts', 'ui_dispatch_enter',
    'ui_dispatch_return', 'pot_enqueue_attempts', 'switch_enqueue_attempts',
    'last_pot_id_value', 'last_switch_group_kind_key', 'ui_render_calls',
    'ui_mode_page_view_ovr', 'ui_modifier_mask', 'spi4_cr2', 'shadow_keys_0_23',
    'shadow_keys_24_47', 'led_index_flags_cur', 'ui_queue_depth', 'ui_deque_map',
    'spi4_sr', 'spi4_cr1', 'spi4_cfg1', 'spi4_cfg2', 'led_frames_started',
    'led_eot_observed', 'led_busy_deferrals', 'led_spacing_deferrals',
    'led_fault_deferrals', 'led_observation_age_max_cycles', 'led_last_fault_sr',
    'led_value_changes', 'pool_small_used', 'pool_failures', 'malloc_failures',
    'heap_remaining', 'irq_off_max_cycles', 'heap_largest_free']
assert len(P6_FIELDS) == 52

RSR_BITS = ((17, "CPU"), (19, "D1"), (20, "D2"), (21, "BOR"), (22, "PIN"),
            (23, "POR"), (24, "SFT"), (26, "IWDG1"), (28, "WWDG1"), (30, "LPWR"))
FAULTS = {1: "HardFault", 2: "MemManage", 3: "BusFault", 4: "UsageFault"}
IRQ_NAMES = {0: "thread", 15: "SysTick", 24: "EXTI2 audio", 27: "DMA1_S0 keys",
             53: "USART1 MIDI", 56: "EXTI15_10 clock-in", 66: "TIM5 transport",
             108: "QUADSPI"}

BOOT_FIELDS = ["magic", "boots", "rcc_rsr", "fault", "fault_pc", "fault_lr",
               "cfsr", "faults_total", "heap_max_brk", "prev_pool_failures",
               "prev_pool_small_high", "prev_pool_big_high", "prev_malloc_failures",
               "prev_heap_remaining", "prev_audio_max_cycles", "prev_audio_overruns",
               "prev_audio_frames", "prev_main_period_max", "prev_main_iterations",
               "prev_systick_max", "hist_thread", "hist_audio", "hist_unused",
               "hist_other", "pc_samples", "fault_psr", "hfsr", "fault_sp"]
HB_FIELDS = ["magic", "seq", "uptime_ms", "hist_thread", "hist_audio", "hist_other",
             "audio_max_cycles", "audio_overruns", "audio_last_cycles",
             "main_period_max", "main_iterations", "pool_small_used",
             "pool_small_high", "pool_failures", "malloc_failures",
             "heap_remaining", "led_frames_delayed", "ui_mode_page_view_ovr",
             "ui_modifier_mask", "ui_held_keys", "shadow_keys_0_23",
             "shadow_keys_24_47", "led_index_flags_cur", "pc_samples",
             "led_frames_started", "spi4_sr", "spi4_cr1",
             "led_brightness_sum", "led_dirty_and_smoothed",
             "ui_queue_depth", "ui_deque_map", "main_last_period",
             "systick_max_cycles", "scanner_calls", "scanner_max_cycles"]

SPI_SR_BITS = ["RXP", "TXP", "DXP", "EOT", "TXTF", "UDR", "OVR", "CRCE",
               "TIFRE", "MODF", "TSERF", "SUSP", "TXC"]


class Symbols:
    def __init__(self):
        self.funcs = []
        try:
            syms = json.load(open(os.path.join(ROOT, "build/firmware.symbols.json")))
            self.funcs = sorted(syms["functions"])
        except (OSError, KeyError, ValueError):
            pass
        self.blob = []
        elf = os.path.join(ROOT, "build/np_patch.elf")
        if os.path.exists(elf):
            try:
                out = subprocess.run(["arm-none-eabi-nm", "-n", elf], capture_output=True,
                                     text=True, check=True).stdout
                for line in out.splitlines():
                    parts = line.split()
                    if len(parts) == 3 and parts[1] in "tTwW":
                        self.blob.append((int(parts[0], 16), parts[2]))
            except (OSError, subprocess.CalledProcessError):
                pass

    def name(self, pc):
        if pc >= 0x08175200 and self.blob:
            i = bisect.bisect_right([a for a, _ in self.blob], pc) - 1
            if i >= 0:
                a, n = self.blob[i]
                return "%s+0x%x" % (n, pc - a)
        if self.funcs:
            i = bisect.bisect_right(self.funcs, pc) - 1
            if i >= 0:
                return "sub_%08X+0x%x" % (self.funcs[i], pc - self.funcs[i])
        return "0x%08X" % pc


def cc_stream_from_text(text):
    """Yield (controller, value) for channel-16 control changes in a monitor log."""
    # MIDI Monitor: "12:00:01.234  Control  16  102  5" / "... Controller 102 ... value 5"
    pat_tabular = re.compile(r"\b(?:Control(?:\s+Change)?|CC)\b\D*?\b16\b\D*?(\d{1,3})\D+(\d{1,3})\b", re.I)
    pat_named = re.compile(r"Controller\s+(\d{1,3}).*?(?:Value|val)\s*[:=]?\s*(\d{1,3})", re.I)
    pat_hex = re.compile(r"\bBF\s+([0-9A-F]{2})\s+([0-9A-F]{2})\b", re.I)
    for line in text.splitlines():
        m = pat_hex.search(line)
        if m:
            yield int(m.group(1), 16), int(m.group(2), 16)
            continue
        m = pat_tabular.search(line) or pat_named.search(line)
        if m:
            yield int(m.group(1)), int(m.group(2))


def cc_stream_from_smf(data):
    """Channel-16 control changes from a Standard MIDI File, in file order."""
    if data[:4] != b"MThd":
        return []
    out = []
    pos = 14
    while pos + 8 <= len(data):
        if data[pos:pos + 4] != b"MTrk":
            break
        length = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        track = data[pos + 8:pos + 8 + length]
        pos += 8 + length
        i = 0
        status = 0
        while i < len(track):
            while track[i] & 0x80:      # delta time
                i += 1
            i += 1
            b = track[i]
            if b == 0xFF:
                ln, i = 0, i + 2
                while track[i] & 0x80:
                    ln = (ln << 7) | (track[i] & 0x7F); i += 1
                ln = (ln << 7) | track[i]; i += 1 + ln
                continue
            if b in (0xF0, 0xF7):
                ln, i = 0, i + 1
                while track[i] & 0x80:
                    ln = (ln << 7) | (track[i] & 0x7F); i += 1
                ln = (ln << 7) | track[i]; i += 1 + ln
                continue
            if b & 0x80:
                status = b
                i += 1
            n = 1 if status & 0xF0 in (0xC0, 0xD0) else 2
            args = track[i:i + n]
            i += n
            if status == 0xBF and n == 2:
                out.append((args[0], args[1]))
    return out


def cc_stream_from_mmon(data):
    """Channel-16 control changes from a MIDI Monitor document (.mmon): a
    binary plist whose messageData is an NSKeyedArchiver archive of
    SMVoiceMessage objects, each with statusByte and dataBytes."""
    import plistlib
    outer = plistlib.loads(data)
    archive = plistlib.loads(outer["messageData"])
    msgs = [o for o in archive["$objects"]
            if isinstance(o, dict) and "statusByte" in o and "dataBytes" in o]
    msgs.sort(key=lambda o: o.get("clockTimeStamp", 0))
    return [(o["dataBytes"][0], o["dataBytes"][1]) for o in msgs
            if o["statusByte"] == 0xBF and len(o["dataBytes"]) == 2]


def records(ccs):
    """Group the CC stream into (tag, [words]) records."""
    recs = []
    cur = None
    parts = []
    for n, v in ccs:
        if n == TAG:
            if cur is not None:
                recs.append((cur, p6_words(parts, EXPECTED[cur]) if cur in FULL_INDEX else words_of(parts)))
            cur, parts = v, []
        elif cur in FULL_INDEX:
            parts.append((n, v))
        elif DATA0 <= n < DATA0 + 5 and cur is not None:
            parts.append((n - DATA0, v))
    if cur is not None:
        recs.append((cur, p6_words(parts, EXPECTED[cur]) if cur in FULL_INDEX else words_of(parts)))
    return recs


def p6_words(parts, n=None):
    """Full word indices, no heuristic reconstruction or index wrapping.

    Invalid/duplicate words stay None. CRC catches cross-word corruption or
    lost tags; even unchecked partial records preserve exact field indices.
    """
    out, seen = [None] * (n or EXPECTED[P6]), set()
    wi, expect, acc = None, 0, 0
    for n, v in parts:
        if 32 <= n <= 47:
            wi, acc, expect = ((n - 32) << 3) | (v >> 4), v & 15, 103
        elif wi is not None and n == expect and 103 <= n <= 106:
            acc = (acc << 7) | v
            expect += 1
            if n == 106:
                if wi < len(out):
                    out[wi] = None if wi in seen else acc
                    seen.add(wi)
                wi = None
        else:
            wi = None
    return out


def p6_length(words):
    """The record length this snapshot's schema defines, or None."""
    return P6_WORDS.get(words[1]) if len(words) > 1 and words[0] == 0x4E505436 else None


def tr_valid(words):
    return (len(words) >= 18 and None not in words[:18] and words[0] == 0x4E505452 and
            zlib.crc32(struct.pack('<17I', *words[:17])) == words[17])


def show_tr(words):
    v = tr_valid(words)
    if not v:
        print('  T  %-8s  INCOMPLETE (%d/18)' % (words[1] if len(words) > 1 else '?',
                                                 sum(w is not None for w in words)))
        return
    up, seq = words[1], words[2]
    pot, span, ringd = words[3] & 0xFF, (words[3] >> 8) & 0xFFFF, words[3] >> 24
    cur, pub = words[4] & 0xFFFF, words[4] >> 16
    dpots, ddisp = words[5] & 0xFFFF, words[5] >> 16
    isr, dskip, row, ovr = words[6] & 0xFFF, (words[6] >> 12) & 0xFF, (words[6] >> 20) & 0xF, words[6] >> 24
    imax = words[7:12]
    pused, pfail = words[12] & 0xFFFF, words[12] >> 16
    mode, page, view, ovrd = words[13] & 0xFF, (words[13] >> 8) & 0xFF, (words[13] >> 16) & 0xFF, words[13] >> 24
    slot = words[14] & 0xFF
    leds = [(words[14] >> sh) & 63 for sh in (8, 14, 20, 26)]
    l1 = struct.unpack('<f', struct.pack('<I', words[15]))[0] if words[15] != 0xFFFFFFFF else None
    qd, pend = words[16] & 0xFFFF, words[16] >> 16
    print('  T %6d ms  pot%-2d moved %5d (row done x%-3d cur %5d pub %5d) events +%-3d disp +%-3d | '
          'audio %3d us stk %3d scan %3d main %4d us | pool %3d/%-3d skip+%-2d ovr %d ADC %s | '
          'slot %2d L18-21 %s local1 %s ui %d/%d/%d/%d q %d mgr %d'
          % (up, pot, span, ringd, cur, pub, dpots, ddisp,
             imax[0] // 400, imax[1] // 400, imax[3] // 400, imax[4] // 400,
             pused, pfail, dskip, ovr, 'OVR' if isr & 0x10 else 'ok',
             slot, leds, ('%.3f' % l1) if l1 is not None else '-', mode, page, view, ovrd, qd, pend))


def crc_valid(words, n, magic):
    return (len(words) >= n and None not in words[:n] and words[0] == magic and
            zlib.crc32(struct.pack('<%dI' % (n - 1), *words[:n - 1])) == words[n - 1])


def slot_list(mask, entries=False):
    """Bit k is slot k; for RC entries bit 0 is the main ADSR, k is slot k-1."""
    out = []
    for k in range(24):
        if mask & (1 << k):
            if k == 23:
                out.append('?')
            elif entries:
                out.append('MAIN-ADSR' if k == 0 else SLOT_NAMES[k - 1] if k - 1 < len(SLOT_NAMES) else 'e%d' % k)
            elif k == 22:
                out.append('MAIN-ADSR')
            else:
                out.append(SLOT_NAMES[k] if k < len(SLOT_NAMES) else 's%d' % k)
    return out


PATTERN_LEN = {}                   # track -> steps, from the newest PM record


def show_ev(words):
    if not crc_valid(words, EXPECTED[EV], 0x4E504556):
        print('  EV  INCOMPLETE/CORRUPT (%d/%d words)' % (sum(w is not None for w in words), EXPECTED[EV]))
        return
    n, lost, first = words[2] & 0xFFFF, words[2] >> 16, words[3]
    if lost:
        print('  EV  !! %d events were overwritten before they could be sent' % lost)
    length = PATTERN_LEN.get(0) or 0
    for k in range(n):
        ms, pos, w = words[4 + 3 * k: 7 + 3 * k]
        typ, mask = w >> 24, w & 0xFFFFFF
        six = pos // 256
        where = 'step %2d/%d' % (six % length + 1, length) if length else ''
        names = slot_list(mask, entries=typ in (4, 5))
        print('  EV %8d ms  pos %7d  16th %5d %-11s %s %s'
              % (ms, pos, six, where, EV_TYPES.get(typ, 'type %d' % typ), ', '.join(names)))


def show_pm(words):
    if not crc_valid(words, EXPECTED[PM], 0x4E50504D):
        print('  PM  INCOMPLETE/CORRUPT (%d/%d words)' % (sum(w is not None for w in words), EXPECTED[PM]))
        return
    lay0, lay1 = words[2] & 0xFF, (words[2] >> 8) & 0xFF
    len0, len1 = words[3] & 0xFFFF, words[3] >> 16
    PATTERN_LEN[0], PATTERN_LEN[1] = len0, len1
    print('\n== PM pattern map at %d ms: layer patterns %d / %d, lengths %d / %d, transport pos %d'
          % (words[1], lay0, lay1, len0, len1, words[4]))
    print('     N note  A lock/automation  T param trig (unlinked X-Env)  X XoX  E expressivity  ? other')
    for track in (0, 1):
        cells = []
        for step in range(64):
            f = (words[5 + track * 16 + step // 4] >> (8 * (step & 3))) & 0xFF
            c = ''.join(ch for bit, ch in ((1, 'N'), (2, 'A'), (4, 'T'), (8, 'X'), (16, 'E'), (32, '?')) if f & bit)
            cells.append(c or '.')
        print('   track %d: ' % track + ' '.join('%-2s' % c for c in cells[:32]))
        print('            ' + ' '.join('%-2s' % c for c in cells[32:]))
    for w in words[37:51]:
        if w:
            track, step, raw = w >> 31, (w >> 24) & 0x7F, w & 0xFFFFFF
            slot = raw >> 16
            print('   param trig: track %d step %2d  slot %d (%s)  raw %06X'
                  % (track, step + 1, slot, SLOT_NAMES[slot] if slot < len(SLOT_NAMES) else '?', raw))


def p6_valid(words):
    n = p6_length(words)
    return (n is not None and len(words) >= n and None not in words[:n] and
            zlib.crc32(struct.pack('<%dI' % (n - 1), *words[:n - 1])) == words[n - 1])


def words_of(parts):
    """Fragments -> words, with a None wherever a word was lost.

    The first fragment needs only four bits for the word's MSBs, so bits 4..6
    of it carry the word's own index mod 8 (firmware from 2026-09-16).  That
    is what lets a lossy capture keep the surviving words on their proper
    fields; in older captures those bits are zero and a dropped CC silently
    shifts everything after it, which is exactly how the first capture from
    the unit came out misaligned.
    """
    raw, acc, expect, widx = [], 0, 0, 0
    for i, v in parts:
        if i != expect:            # lost a CC: resync on the next MSB
            expect, acc = 0, 0
            if i != 0:
                continue
        if i == 0:
            widx, acc = (v >> 4) & 7, v & 0x0F
        else:
            acc = (acc << 7) | v
        expect = i + 1
        if expect == 5:
            raw.append((widx, acc & 0xFFFFFFFF))
            expect, acc = 0, 0
    # In the old format the first fragment was (w >> 28), never wider than
    # four bits, so bits 4..6 were always zero; a varying index means the
    # sender stamps one.  Detecting it this way survives lost words, which
    # is the whole point.
    indexed = len(set(w for w, _ in raw)) > 1
    if not indexed:
        return [v for _, v in raw]
    out, pos = [], 0
    for w, v in raw:
        out.extend([None] * ((w - pos) & 7))    # a gap of exactly 8 is invisible
        out.append(v)
        pos = (w + 1) & 7
    return out


def anchor_heartbeat(words):
    """A gap of exactly 8 words is invisible to a mod-8 index, so a short
    heartbeat can still be shifted.  The last 24 words are PC samples, which
    are recognisable (IPSR byte plus a flash address), so anchor them to the
    end of the record and put the unexplained gap in front of them.  Returns
    (words, inserted)."""
    missing = EXPECTED[HEARTBEAT] - len(words)
    if missing <= 0:
        return words, 0

    def sample_like(w):
        return w is not None and (w >> 24) < 0x20 and 0x100000 <= (w & 0xFFFFFF) < 0x180000

    tail = 0
    while tail < len(words) and tail < 24 and sample_like(words[len(words) - 1 - tail]):
        tail += 1
    if tail < 12:                     # not enough of a tail to trust
        return words, 0
    # The samples land correctly, but nothing says where in the head the burst
    # fell, so every named field from the first gap on stays suspect.
    cut = len(words) - tail
    return words[:cut] + [None] * missing + words[cut:], missing


def rsr_text(v):
    return ", ".join(n for b, n in RSR_BITS if v & (1 << b)) or "none"


# main() parked in a stock dead end: the watchdog reboot that follows
# leaves these in the retained ring
TRAP_LOOPS = {
    0x0815A6E0: "  <-- _exit `b .` after abort(): e.g. \"Borrowable already borrowed\" "
                "(a flash paste during a save, fixed by Q1 in p35)",
    0x0814340A: "  <-- internal-flash journal erase failed: `b .`",
}


def show_samples(words, sym, title):
    lost = sum(1 for w in words if w is None)
    words = [w for w in words if w is not None]
    print("  %s (%d samples, oldest first%s):" % (
        title, len(words), ", %d lost in transit" % lost if lost else ""))
    by_ctx = collections.Counter(w >> 24 for w in words)
    print("    contexts: " + ", ".join("%s %d" % (IRQ_NAMES.get(c, "IRQ%d" % (c - 16) if c >= 16 else "exc%d" % c), n)
                                       for c, n in by_ctx.most_common()))
    thread = collections.Counter(w & 0xFFFFFF for w in words if (w >> 24) == 0)
    if thread:
        print("    thread-mode hot spots:")
        for off, n in thread.most_common(8):
            pc = 0x08000000 | off
            print("      %5d  0x%08X  %s%s" % (n, pc, sym.name(pc), TRAP_LOOPS.get(pc, "")))
    tail = words[-24:]
    print("    last 24: " + " ".join("%06X/%d" % (w & 0xFFFFFF, w >> 24) for w in tail))
    markers = [w for w in words if (w >> 16) == 0xB007]
    if markers:
        print("    boot markers in ring: " + ", ".join("boot %d" % (w & 0xFFFF) for w in markers))


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    raw = open(argv[1], "rb").read()
    if raw[:6] == b"bplist":
        ccs = cc_stream_from_mmon(raw)
    elif raw[:4] == b"MThd":
        ccs = cc_stream_from_smf(raw)
    else:
        ccs = list(cc_stream_from_text(raw.decode("utf-8", "replace")))
    if not ccs:
        print("no channel-16 control changes found in %s" % argv[1])
        return 1
    sym = Symbols()
    recs = records(ccs)
    print("%d channel-16 CCs, %d records" % (len(ccs), len(recs)))
    for tag, words in recs:
        if tag == TR:
            show_tr(words)
            continue
        if tag == EV:
            show_ev(words)
            continue
        if tag == PM:
            show_pm(words)
            continue
        if tag == P6:
            valid = p6_valid(words)
            n = p6_length(words) or EXPECTED[P6]
            words = words[:n]
            print('\n== P6 snapshot schema %s: %s (%d/%d words)' %
                  (words[1], 'CRC OK' if valid else 'INCOMPLETE/CORRUPT — NOT VERIFIED',
                   sum(w is not None for w in words), n))
            fields = list(P6_FIELDS)
            if len(words) > 1 and words[1] is not None and words[1] >= 8:
                fields[36] = 'flash_journal_writes'      # was spi4_cfg1, a constant
                fields[43] = 'flash_write_max_cycles'    # was led_observation_age_max_cycles
            for name, w in zip(fields, words):
                if w is None:
                    print('  %-36s LOST' % name)
                else:
                    extra = ' (%.2f us at 400 MHz)' % (w / 400) if name.endswith('cycles') else ''
                    print('  %-36s %10d  0x%08X%s' % (name, w, w, extra))
            for start, label in ((52, 'submitted to SPI'), (70, 'EOT observed')):
                vals = [None if w is None else (w >> (8*n)) & 255
                        for w in words[start:start+18] for n in range(4)][:70]
                print('  LED %s (FF = not observed this boot):' % label)
                for i in range(0, 70, 10):
                    print('    %02d: ' % i + ' '.join('--' if v is None else '%02X' % v
                                                    for v in vals[i:i+10]))
            # Do not symbolize against whatever unrelated ELF happens to be
            # current: firmware build identity must be checked first.
            print('  recent PCs (raw; oldest first): ' + ' '.join(
                'LOST' if w is None else '%08X' % w for w in words[88:94]))
            if n >= 99:
                a, b, f0, f1 = words[94:98]
                print('  parameter indicators (the SysTick renderer\'s chain, one instant):')
                if a is None or b is None:
                    print('    LOST')
                else:
                    slot = a & 0xFF
                    comp = [(a >> 8) & 31, (a >> 13) & 31, (a >> 18) & 31, (a >> 23) & 31, b & 31]
                    held = [(b >> 5) & 63, (b >> 11) & 63, (b >> 17) & 63, (b >> 23) & 63]
                    print('    selected slot %d' % slot)
                    print('    renderer would send  LED18..22 = %s' % comp)
                    print('    LED entries hold     LED18..21 = %s' % held)
                    for k, f in ((0, f0), (1, f1)):
                        if f is None:
                            print('    local %d value: LOST' % k)
                        elif f == 0xFFFFFFFF:
                            print('    local %d: no CParam registered' % k)
                        else:
                            print('    local %d value = %.6f (0x%08X)' % (
                                k, struct.unpack('<f', struct.pack('<I', f))[0], f))
            if n >= 106:
                a, d, c18, r18, b01, b2, cfgr = words[98:105]
                print('  pot scanner input side:')
                if None in (a, d, c18, r18, b01, b2, cfgr):
                    print('    LOST')
                else:
                    isr, gate, cr, row = a & 0xFFF, (a >> 12) & 15, (a >> 16) & 0xFF, a >> 24
                    flags = [nm for bit, nm in enumerate(('ADRDY', 'EOSMP', 'EOC', 'EOS', 'OVR',
                             'JEOC', 'JEOS', 'AWD1', 'AWD2', 'AWD3', 'JQOVF')) if isr >> bit & 1]
                    print('    ADC1 ISR=%#05x [%s]%s  CR: ADEN=%d ADSTART=%d ADDIS=%d  mux row %d  event gate %d%s'
                          % (isr, '|'.join(flags) or '-',
                             '  <-- OVERRUN: DMA requests stop until it is cleared' if isr & 0x10 else '',
                             cr & 1, cr >> 2 & 1, cr >> 1 & 1, row, gate,
                             '' if gate == 0 else '  <-- pot events SUPPRESSED'))
                    lisr, s0cr, ndtr = d & 0xFFFF, (d >> 16) & 0x1F, d >> 24
                    print('    DMA1 LISR=%#06x (S0: FE=%d DME=%d TE=%d HT=%d TC=%d)  S0CR EN=%d TCIE=%d  S0NDTR=%d'
                          % (lisr, lisr & 1, lisr >> 2 & 1, lisr >> 3 & 1, lisr >> 4 & 1, lisr >> 5 & 1,
                             s0cr & 1, s0cr >> 4 & 1, ndtr))
                    print('    pot 18 (AMOUNT): current %5d  last published %5d  average %5d  ring writes %5d'
                          % (c18 & 0xFFFF, c18 >> 16, r18 & 0xFFFF, r18 >> 16))
                    print('    DMA buffer: %5d %5d %5d   pot 12 average %5d   ADC1 CFGR=%#010x (DMNGT=%d OVRMOD=%d CONT=%d)'
                          % (b01 & 0xFFFF, b01 >> 16, b2 & 0xFFFF, b2 >> 16, cfgr, cfgr & 3, cfgr >> 12 & 1, cfgr >> 13 & 1))
            if n >= 108:
                sk, ov = words[105:107]
                print('  scan re-arm (patch O): %s ticks skipped with the previous row unconsumed, '
                      '%s re-arms cleared a latched OVR' % (
                          'LOST' if sk is None else sk, 'LOST' if ov is None else ov))
            if n >= 120:
                tf, big = words[107], words[108]
                print('  small pool census: thread fallbacks %s, big pool used %s high %s' % (
                    'LOST' if tf is None else tf,
                    'LOST' if big is None else big & 0xFFFF, 'LOST' if big is None else big >> 16))
                for k in range(4):
                    key, cnt = words[109 + 2 * k], words[110 + 2 * k]
                    if key is None or cnt is None:
                        print('    LOST'); continue
                    if cnt == 0:
                        break
                    print('    %4d blocks whose first word is 0x%08X  %s' % (
                        cnt, key, sym.name(key) if 0x08100000 <= key < 0x08200000 else ''))
                if words[117]:
                    print('    %4s blocks with other first words' % words[117])
                sp = words[118]
                if sp is not None:
                    lo, hi = sp & 0xFFFF, sp >> 16
                    print('  pot 18 reading over the last interval: min %5d max %5d span %5d%s' % (
                        lo, hi, hi - lo,
                        '   <-- the ADC SEES the knob move' if hi - lo > 1000 else ''))
            if n >= 123:
                pend, budget, walked = words[119:122]
                print('  project event manager: %s pending (list walked: %s), budget %s us%s' % (
                    'LOST' if pend is None else pend, 'LOST' if walked is None else walked,
                    'LOST' if budget is None else budget,
                    '   <-- BACKLOG: applies and notes are queued behind this' if (walked or 0) > 50 else ''))
            if n >= 128:
                service, yields, progress, top, mask = words[122:127]
                print('  bounded UI service: max %s us, %s yields; project first items %s' % (
                    'LOST' if service is None else '%.2f' % (service / 400), yields, progress))
                print('  pot events since previous P6: busiest %s, active pots %s' % (
                    'LOST' if top is None else ('none' if top >> 24 == 255 else
                        '%d (%d events)' % (top >> 24, top & 0xFFFFFF)),
                    'LOST' if mask is None else [p for p in range(23) if mask & (1 << p)]))
            continue
        # A record shorter than its format, with no gap markers, came from a
        # firmware without the per-word index: every field after the first
        # lost word sits on the wrong name, so print the numbers bare rather
        # than decorate them with confident nonsense.
        anchored = 0
        if tag == HEARTBEAT and len(words) < EXPECTED[HEARTBEAT] and None in words:
            words, anchored = anchor_heartbeat(words)
            if anchored:
                print("\n.. %d words lost in one burst were invisible to the "
                      "mod-8 index (a gap of exactly 8 looks like none);\n"
                      ".. the trailing PC samples anchor the record, so the "
                      "gap is shown where it must be." % anchored)
        suspect_from = None
        if anchored:
            suspect_from = next(i for i, w in enumerate(words) if w is None)
            print(".. named fields from #%d on may sit on the wrong name; the "
                  "24 PC samples are correctly placed." % suspect_from)
        short = len(words) < EXPECTED.get(tag, 0) and None not in words
        if short:
            print("\n!! %d of %d words missing and this capture carries no "
                  "per-word index:\n!! field names past the first gap are "
                  "NOT reliable.  Reflash a build from 2026-09-16 or later\n"
                  "!! (the index costs no extra CCs) and capture again."
                  % (EXPECTED[tag] - len(words), EXPECTED[tag]))
        if tag == BOOT:
            print("\n== boot record (%d words)" % len(words))
            for name, w in zip(BOOT_FIELDS, words):
                if w is None:
                    print("  %-24s %10s" % (name, "LOST"))
                    continue
                extra = ""
                if short:
                    print("  %-24s %10u  0x%08X   (name unreliable)" % (name, w, w))
                    continue
                if name == "rcc_rsr":
                    extra = "  " + rsr_text(w)
                elif name == "fault":
                    extra = "  " + FAULTS.get(w, "pool panic (UDF)" if w == 5 else "none")
                elif name in ("fault_pc", "fault_lr") and w:
                    extra = "  " + sym.name(w & ~1)
                    # the stock's deliberate traps: BKPT with no debugger
                    # attached is a HardFault (HFSR.DEBUGEVT), so each of
                    # these is a reboot.  Builds before p31 decoded an FP
                    # frame's PC/LR from the wrong words -- distrust them.
                    if name == "fault_pc":
                        extra += {0x08119B08: "  <-- Error_Handler: a HAL call failed; "
                                              "fault_lr is the call site",
                                  0x08119A14: "  <-- main(): a QSPI (project flash) "
                                              "operation returned an error",
                                  0x0811968C: "  <-- stock _sbrk heap-full trap"
                                  }.get(w & ~1, "")
                elif name == "fault_psr" and w:
                    ipsr = w & 0x1FF
                    extra = "  faulted in %s" % (IRQ_NAMES.get(ipsr,
                        "IRQ%d" % (ipsr - 16) if ipsr >= 16 else "exc%d" % ipsr))
                elif name == "hfsr" and w:
                    extra = "  " + (", ".join(n for b, n in
                        ((1, "VECTTBL"), (30, "FORCED"), (31, "DEBUGEVT")) if w & (1 << b)) or "?")
                elif name == "fault_sp" and w:
                    extra = "  (DTCM)" if 0x20000000 <= w < 0x20020000 else "  <-- NOT in DTCM: bad stack"
                elif name == "heap_max_brk" and w:
                    extra = "  (%d bytes used at peak)" % (w - 0x30000000)
                elif name.endswith("cycles") or name.endswith("period_max") or name.endswith("systick_max"):
                    extra = "  (%.1f us at 400 MHz)" % (w / 400.0)
                print("  %-24s %10u  0x%08X%s" % (name, w, w, extra))
        elif tag == RING:
            print("\n== retained PC ring from the previous session")
            show_samples(words, sym, "ring")
        elif tag == LEDS:
            vals = []
            for w in words:
                vals.extend([None] * 4 if w is None
                            else [(w >> (8 * n)) & 0xFF for n in range(4)])
            vals = vals[:70]
            print("\n== LED panel image (%d of 70 requested brightnesses, 0..63)"
                  % sum(1 for v in vals if v is not None))
            for row in range(0, 70, 10):
                cells = " ".join("%3s" % ("--" if v is None else v)
                                 for v in vals[row:row + 10])
                print("  %2d..%2d  %s" % (row, min(row + 9, 69), cells))
            lit = [i for i, v in enumerate(vals) if v]
            print("  lit: %s" % (", ".join("%d=%d" % (i, vals[i]) for i in lit) or "none"))
        elif tag == HEARTBEAT:
            print("\n== heartbeat (%d words)" % len(words))
            fixed = words[:35]
            for i, (name, w) in enumerate(zip(HB_FIELDS, fixed)):
                if w is None:
                    print("  %-24s %10s" % (name, "LOST"))
                    continue
                extra = ""
                if short or (suspect_from is not None and i >= suspect_from):
                    print("  %-24s %10u  0x%08X   (name unreliable)" % (name, w, w))
                    continue
                if name == "ui_mode_page_view_ovr":
                    view = (w >> 16) & 0xFF
                    extra = "  mode=%d page=%d view=%d%s override=%d" % (
                        w & 0xFF, (w >> 8) & 0xFF, view,
                        "" if view == 1 else " (NOT 1: parameter LEDs are not refreshed)",
                        w >> 24)
                elif name.startswith("shadow_keys") and w:
                    base = 0 if name.endswith("0_23") else 24
                    extra = "  pressed: " + ",".join(str(base + i) for i in range(24) if w >> i & 1)
                elif name == "led_index_flags_cur":
                    flags = (w >> 16) & 0xFF
                    extra = "  led writer index=%d refresh flag=%d dirty[render=%d view=%d page=%d mode=%d] mod-note=%d" % (
                        w & 0xFF, (w >> 8) & 0xFF, flags & 1, flags >> 1 & 1, flags >> 2 & 1, flags >> 3 & 1, w >> 24)
                elif name == "led_frames_delayed" and w:
                    extra = "  (LED frames the guard held back: two SysTicks landed inside one frame)"
                elif name == "spi4_sr":
                    on = [b for i, b in enumerate(SPI_SR_BITS) if w >> i & 1]
                    extra = "  %s remaining=%d%s" % (
                        "|".join(on) or "-", w >> 16,
                        "" if w & (1 << 3) else "  (EOT clear: a frame is unfinished)")
                elif name == "ui_queue_depth":
                    extra = ("  iterators outside the block map"
                             if w == 0xFFFFFFFF else
                             "  events waiting for the UI consumer"
                             + ("" if w < 128 else "  (A STALLED CONSUMER: this should stay small)"))
                elif name == "ui_deque_map":
                    si, fi = (w >> 8) & 0xFF, (w >> 16) & 0xFF
                    extra = "  map holds %d blocks, start at %s, finish at %s" % (
                        w & 0xFF,
                        "OUTSIDE" if si == 0xFF else si,
                        "OUTSIDE" if fi == 0xFF else fi)
                elif name == "led_dirty_and_smoothed":
                    extra = "  %d LEDs dirty, smoothed sum %d" % (w & 0xFF, w >> 8)
                elif name == "spi4_cr1":
                    extra = "  SPE=%d CSTART=%d CSUSP=%d%s" % (
                        w & 1, w >> 9 & 1, w >> 10 & 1,
                        "" if w & 1 else "  (SPI4 DISABLED: the LED driver is deaf)")
                elif name.endswith("cycles") or name.endswith("period_max"):
                    extra = "  (%.1f us at 400 MHz)" % (w / 400.0)
                print("  %-24s %10u  0x%08X%s" % (name, w, w, extra))
            if len(words) > 35:
                show_samples(words[35:], sym, "recent samples")
        else:
            print("\n== unknown record tag %d, %d words" % (tag, len(words)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
