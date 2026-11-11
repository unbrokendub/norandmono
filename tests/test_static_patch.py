#!/usr/bin/env python3
"""Verify the completed Norand patch without running a CPU emulator.

This check is intentionally independent of ``tools/patch.py build``.  It
inspects the final raw image and its SysEx container, so it continues to work
on macOS ARM hosts where Unicorn currently raises SIGILL during ``uc_mem_map``.
"""

from pathlib import Path
import hashlib
import struct
import sys

from capstone import Cs, CS_ARCH_ARM, CS_MODE_LITTLE_ENDIAN, CS_MODE_THUMB

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import norand_syx  # noqa: E402


BASE = 0x08100000
BLOB = 0x08175200
STOCK_SIZE = 0x751D8


def at(image, address, size):
    return image[address - BASE:address - BASE + size]


def fail(message):
    raise SystemExit("FAIL: " + message)


def expect_bytes(image, address, want, label):
    got = at(image, address, len(want))
    if got != want:
        fail("0x%08X %s: got %s, want %s" %
             (address, label, got.hex(" "), want.hex(" ")))


def expect_branch(image, md, address, mnemonic, target):
    ins = next(md.disasm(at(image, address, 4), address, 1), None)
    if ins is None or ins.mnemonic not in (mnemonic, mnemonic + ".w"):
        fail("0x%08X: expected %s, got %s" %
             (address, mnemonic, "nothing" if ins is None
              else ins.mnemonic + " " + ins.op_str))
    got = int(ins.op_str.strip("# "), 0)
    if got != target:
        fail("0x%08X: %s reaches 0x%08X, want 0x%08X" %
             (address, ins.mnemonic, got, target))


def main():
    stock = (ROOT / "build/firmware.bin").read_bytes()
    image = (ROOT / "build/firmware-patched.bin").read_bytes()
    syx = (ROOT / "build/mono_V2.2.0r-patched.syx").read_bytes()

    if len(stock) != STOCK_SIZE:
        fail("stock image length is %d, want %d" % (len(stock), STOCK_SIZE))
    if len(image) % 32:
        fail("patched image is not a whole 32-byte flash word")
    if norand_syx.fw_unpack(syx) != image:
        fail("SysEx does not decode back to firmware-patched.bin")

    stack, reset = struct.unpack_from("<II", image)
    if (stack, reset) != (0x20020000, 0x08155FF9):
        fail("initial vector changed: SP=0x%08X reset=0x%08X" %
             (stack, reset))

    blob_off = BLOB - BASE
    if image[blob_off:blob_off + 4] != b"NPAT":
        fail("patch blob signature missing")
    (early, late, wdg_hook, hardfault, memmanage, busfault, usagefault,
     sbrk, _diag, _diag_size) = struct.unpack_from("<10I", image,
                                                    blob_off + 0x10)
    if (_diag, _diag_size) != (0x2000E400, 0x1C00):
        fail("diagnostic block is 0x%08X/%u, want 0x2000E400/7168" %
             (_diag, _diag_size))
    expect_bytes(image, early & ~1, bytes.fromhex("08b5"),
                 "np_early preserves 8-byte call alignment")
    mod_tick, _lfo_poll, xmod_time, xmod_speed, xmod_pos = struct.unpack_from(
        "<5I", image, blob_off + 0x44)
    sine, tri, square, saw = struct.unpack_from("<4I", image,
                                                blob_off + 0x68)
    (midi_tx, midi_irq, usb_defer, audio_irq, systick_irq, tim5_irq,
     dma_irq, new_entry, delete_entry, malloc_lock, malloc_unlock,
     _main_service, note_hook, note_addr) = struct.unpack_from(
         "<14I", image, blob_off + 0x78)
    wave_thunks = struct.unpack_from("<6I", image, blob_off + 0xB0)
    ui_switch, ui_begin = struct.unpack_from("<2I", image, blob_off + 0xCC)
    ui_helpers = struct.unpack_from("<6I", image, blob_off + 0xD4)
    ui_modifier = struct.unpack_from('<I', image, blob_off + 0xF0)[0]
    random_splice = struct.unpack_from('<I', image, blob_off + 0xF4)[0]
    xenv_thunks = struct.unpack_from('<7I', image, blob_off + 0xF8)
    env_main = struct.unpack_from('<I', image, blob_off + 0x118)[0]
    led_frame, led_done, ui_queue_pop = struct.unpack_from('<3I', image, blob_off + 0x12C)
    scan_rearm = struct.unpack_from('<I', image, blob_off + 0x14C)[0]
    ui_service, project_first = struct.unpack_from('<2I', image, blob_off + 0x150)
    led_configure, diag_render, diag_enqueue, diag_dispatch = struct.unpack_from('<4I', image, blob_off + 0x138)
    xenv_master, xenv_dtcm, env_state = struct.unpack_from('<3I', image, blob_off + 0x120)
    if xenv_dtcm != 0x2000E540 or env_state != 0x2000E580 or not (BLOB <= xenv_master < BLOB + 0x4000):
        fail("envelope block pointers 0x%08X / 0x%08X / 0x%08X" % (xenv_master, xenv_dtcm, env_state))
    # the RC model reads its nine constants from DTCM, never from flash;
    # ln 4 first, the two decay constants last (main 3.6, X-Env 4.32)
    expect_bytes(image, xenv_master, struct.pack('<f', 1.3862943611198906), 'attack constant ln 4')
    expect_bytes(image, xenv_master + 28, struct.pack('<2f', 3.6, 4.32), 'decay constants')
    expect_bytes(image, BLOB + 0xEC, struct.pack('<I', 0x3003C040), 'UI shadow address')

    # H5 takes the noise route inside sample & hold instead of adding a
    # sixth waveform, so the waveform list stays exactly as shipped.
    for a, n, what in ((0x08156904, 2, "setWaveform clamp"),
                       (0x0815E8D4, 2, "waveform wrap count"),
                       (0x08150100, 2, "WAVE UI cycle, direct"),
                       (0x08150552, 4, "WAVE UI cycle, alternate"),
                       (0x081506EE, 2, "WAVE UI cycle, batch")):
        if image[a - BASE:a - BASE + n] != stock[a - BASE:a - BASE + n]:
            fail("%s at 0x%08X was modified; the waveform list must stay "
                 "at five" % (what, a))

    if note_addr != 0x2000E4B4:
        fail("np_note is at 0x%08X, want the diagnostic block at 0x2000E4B4"
             % note_addr)

    for address, target in ((0x0810000C, hardfault),
                            (0x08100010, memmanage),
                            (0x08100014, busfault),
                            (0x08100018, usagefault)):
        got = struct.unpack_from("<I", image, address - BASE)[0]
        if got != target or not (got & 1):
            fail("0x%08X vector is 0x%08X, want Thumb 0x%08X" %
                 (address, got, target))

    for address, target in ((0x0810003C, systick_irq),
                            (0x08100060, audio_irq),
                            (0x0810006C, dma_irq),
                            (0x08100108, tim5_irq)):
        got = struct.unpack_from("<I", image, address - BASE)[0]
        if got != target or not (got & 1):
            fail("0x%08X profiled vector is 0x%08X, want Thumb 0x%08X" %
                 (address, got, target))

    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)
    for site, thunk in zip((0x0811C69E, 0x0811129A, 0x08110ABA,
                           0x0811271E, 0x08112952, 0x08112F34, 0x0811255A), xenv_thunks):
        expect_branch(image, md, site, 'b', thunk & ~1)
    expect_branch(image, md, 0x08112DDA, 'b', env_main & ~1)
    # L: the LED writer's dirty path defers to the frame guard, which replays
    # the displaced push, drives both CS transitions itself and rejoins the
    # stock writer at 0x0810A01C; its epilogue timestamps the frame start
    expect_branch(image, md, 0x0810A002, 'b', led_frame & ~1)
    expect_bytes(image, led_frame & ~1, bytes.fromhex('10b5'), 'LED guard replays the displaced push')
    expect_branch(image, md, 0x0810A11E, 'b', led_done & ~1)
    for site, target, op in ((0x08109DF4, led_configure, 'b'),
                             (0x0814F80C, diag_render, 'b'),
                             (0x08157324, diag_enqueue, 'b'),
                             (0x08157528, diag_dispatch, 'bl')):
        expect_branch(image, md, site, op, target & ~1)
    import zlib
    normalized = bytearray(image)
    build_id = struct.unpack_from('<I', image, blob_off + 0x148)[0]
    struct.pack_into('<I', normalized, blob_off + 0x148, 0)
    if zlib.crc32(normalized) != build_id:
        fail('P6 MIDI build identity does not match the actual image')
    # M: brightness store before the dirty store, as the overlay setter does
    expect_bytes(image, 0x0810A166, bytes.fromhex('80f8942080f89730'),
                 'LED brightness published before dirty')
    expect_bytes(image, 0x0810A182, bytes.fromhex('80f8962080f89730'),
                 'LED overlay setter order unchanged')
    # O: the SysTick pot-scan re-arm goes through np_scan_rearm and skips
    # the displaced stock sequence, which is left in place unreachable
    expect_branch(image, md, 0x0811972A, 'bl', scan_rearm & ~1)
    expect_branch(image, md, 0x0811972E, 'b', 0x0811973E)
    expect_bytes(image, 0x08119732, bytes.fromhex('01030b61936843f004039360'),
                 'displaced scan re-arm left in place')
    # N: the UI deque's start-iterator transition runs with interrupts masked
    expect_branch(image, md, 0x08157558, 'b', ui_queue_pop & ~1)
    expect_bytes(image, 0x0815755C, bytes.fromhex('2263'),
                 'the displaced deque run is left in place, unreachable')
    # the thunk replays the displaced addw r3,r5,#0xf2c and resumes after it
    # (searched for: the CH fade in np_env_main moved it from +60)
    body = at(image, env_main & ~1, 0x100)
    replay = body.find(bytes.fromhex('05f62c73'))
    if replay < 0 or replay & 1:
        fail('np_env_main: the displaced addw r3,r5,#0xf2c is not replayed')
    else:
        expect_branch(image, md, (env_main & ~1) + replay + 4, 'b', 0x08112DDE)
    expect_branch(image, md, 0x08143C14, 'bl', random_splice & ~1)
    expect_bytes(image, random_splice & ~1, bytes.fromhex('2246'), 'random request r4 -> r2')
    for address, mnemonic, target in (
            (0x08119678, "b", sbrk & ~1),
            (0x0811CC70, "b", mod_tick & ~1),
            (0x0811BD48, "b", xmod_time & ~1),
            (0x0811BBA8, "b", xmod_speed & ~1),
            (0x08112C88, "b", xmod_pos & ~1),
            (0x08156656, "b", sine & ~1),
            (0x0815669A, "b", tri & ~1),
            (0x081566D2, "b", square & ~1),
            (0x0815670E, "b", saw & ~1),
            (0x0810EEA0, "b", midi_irq & ~1),
            (0x081172B0, "bl", note_hook & ~1),
            (0x0811C6E0, "b", wave_thunks[0] & ~1),
            (0x0811C892, "b", wave_thunks[1] & ~1),
            (0x081112F8, "b", wave_thunks[2] & ~1),
            (0x08111508, "b", wave_thunks[3] & ~1),
            (0x08110B18, "b", wave_thunks[4] & ~1),
            (0x08110D34, "b", wave_thunks[5] & ~1),
            (0x0811AC90, "b", midi_tx & ~1),
            (0x08119720, "bl", usb_defer & ~1),
            (0x081196A0, "b", malloc_lock & ~1),
            (0x081196B8, "b", malloc_unlock & ~1),
            (0x0815906C, "b", delete_entry & ~1),
            (0x08159070, "b", delete_entry & ~1),
            (0x081591D4, "b", new_entry & ~1),
            (0x08156022, "bl", early & ~1),
            (0x0815602A, "bl", late & ~1),
            (0x08119A02, "bl", wdg_hook & ~1)):
        expect_branch(image, md, address, mnemonic, target)

    expect_branch(image, md, 0x08155C7C, "b", ui_switch & ~1)
    expect_branch(image, md, 0x08119A60, 'bl', ui_service & ~1)
    expect_branch(image, md, 0x0815766C, 'b', project_first & ~1)
    eval_linad, eval_expad, set_rate = struct.unpack_from('<3I', image, blob_off + 0x158)
    for site in (0x08111858, 0x08111D08, 0x081127D4, 0x08112BF4,
                 0x08112D16, 0x08112D28, 0x0811317E, 0x081131C8):
        expect_branch(image, md, site, 'bl', eval_linad & ~1)
    for site in (0x08112CEC, 0x08112CFC):
        expect_branch(image, md, site, 'bl', eval_expad & ~1)
    expect_branch(image, md, 0x08112CD8, 'bl', set_rate & ~1)
    slot_walk, after_walk = struct.unpack_from('<2I', image, blob_off + 0x164)
    expect_branch(image, md, 0x08112C4A, 'b', slot_walk & ~1)
    expect_bytes(image, 0x08112C4E, bytes.fromhex('00bf'), 'walk splice pad is a nop')
    expect_branch(image, md, 0x08112CE2, 'bl', after_walk & ~1)
    expect_bytes(image, slot_walk & ~1, bytes.fromhex('c3ed007a'), 'walk replays vstr s15,[r3]')
    flash_write = struct.unpack_from('<I', image, blob_off + 0x178)[0]
    expect_branch(image, md, 0x0812407C, 'bl', flash_write & ~1)
    led_smooth = struct.unpack_from('<I', image, blob_off + 0x17C)[0]
    expect_branch(image, md, 0x0810A098, 'bl', led_smooth & ~1)
    expect_bytes(image, 0x0810A09C, bytes.fromhex('00bf00bf'), 'M2 pad is two nops')
    list_sort, seq_worker = struct.unpack_from('<2I', image, blob_off + 0x180)
    expect_branch(image, md, 0x08158708, 'bl', list_sort & ~1)
    expect_branch(image, md, 0x08146E76, 'bl', seq_worker & ~1)
    expect_branch(image, md, 0x08146E82, 'b', seq_worker & ~1)
    expect_branch(image, md, 0x08158A54, 'bl', 0x0815888C)     # the flush keeps the stock pass
    ev_trig, morph_trig = struct.unpack_from('<2I', image, blob_off + 0x188)
    expect_branch(image, md, 0x08146804, 'b', morph_trig & ~1)
    ev_log = at(image, 0x08156074, 6) != at(stock, 0x08156074, 6)   # --ev-log builds only
    if ev_log:
        expect_branch(image, md, 0x08156074, 'b', ev_trig & ~1)
        expect_bytes(image, 0x08156078, bytes.fromhex('00bf'), 'EV splice pad is a nop')
    expect_bytes(image, 0x0815607A, bytes.fromhex('80f82d307047'), 'the X-Env trigger tail is stock')
    # Q1: the project service's loop head; MF1-MF5: MIDI flow, poly pressure
    project_gate, midi_fanout, polyat, usb_rx_put = struct.unpack_from(
        '<4I', image, blob_off + 0x190)
    expect_branch(image, md, 0x08157672, 'b', project_gate & ~1)
    expect_branch(image, md, 0x08119A2C, 'bl', midi_fanout & ~1)
    expect_branch(image, md, 0x08108D4A, 'bl', usb_rx_put & ~1)
    expect_branch(image, md, 0x0811B754, 'bhs', polyat & ~1)
    # N1: note start -> snap request; per-sample DSP call -> snapping wrapper
    # N2: the mod-note parameter replay, direct
    note_start, dsp_sample, modnote_replay = struct.unpack_from('<3I', image, blob_off + 0x1A0)
    expect_branch(image, md, 0x08116D30, 'b', note_start & ~1)
    expect_branch(image, md, 0x0811503C, 'bl', dsp_sample & ~1)
    expect_branch(image, md, 0x081209F4, 'b', modnote_replay & ~1)
    # CH: the instrument-switch fade (off when its sample count is 0)
    cnote_entry, noteoff_entry, choke_samples = struct.unpack_from('<3I', image, blob_off + 0x1AC)
    if choke_samples:
        expect_branch(image, md, 0x08117AD0, 'bl', cnote_entry & ~1)
        expect_branch(image, md, 0x08117594, 'b', noteoff_entry & ~1)
    else:
        expect_bytes(image, 0x08117594, at(stock, 0x08117594, 4), 'CH off: note-off prologue stock')
    # R1: the mod-note randomizer builds instruments (stock with --no-smart-random)
    modnote_random = struct.unpack_from('<I', image, blob_off + 0x1B8)[0]
    if at(image, 0x0814476C, 4) != at(stock, 0x0814476C, 4):
        expect_branch(image, md, 0x0814476C, 'b', modnote_random & ~1)
    # R2: black + white keys (needs R1 and the UI fix)
    mn_key_press, mn_rec_press, mn_rec_release = struct.unpack_from('<3I', image, blob_off + 0x1BC)
    if at(image, 0x081521AA, 4) != at(stock, 0x081521AA, 4):
        expect_branch(image, md, 0x081521AA, 'b', mn_key_press & ~1)
        expect_branch(image, md, 0x0814FEB8, 'bl', mn_rec_press & ~1)
        expect_branch(image, md, 0x081521FE, 'bl', mn_rec_release & ~1)
    # K1: TEMPO+FUNC+C# builds a drum kit
    mn_chord_check = struct.unpack_from('<I', image, blob_off + 0x1C8)[0]
    if at(image, 0x081524D4, 4) != at(stock, 0x081524D4, 4):
        expect_branch(image, md, 0x081524D4, 'bl', mn_chord_check & ~1)
    # P1: TEMPO+FUNC+note builds the patch in normal mode (the key thunks' tails)
    nm_key_press, nm_key_release = struct.unpack_from('<2I', image, blob_off + 0x1CC)
    if at(image, 0x08147B98, 4) != at(stock, 0x08147B98, 4):
        expect_branch(image, md, 0x08147B98, 'b', nm_key_press & ~1)
        expect_branch(image, md, 0x08147BA0, 'b', nm_key_release & ~1)
        expect_bytes(image, 0x08147B94, at(stock, 0x08147B94, 4), 'P1: press thunk loads stock')
        expect_bytes(image, 0x08147B9C, at(stock, 0x08147B9C, 4), 'P1: release thunk loads stock')
    # D1: the clear-patch record match skips mod notes
    clear_match = struct.unpack_from('<I', image, blob_off + 0x1D4)[0]
    if at(image, 0x08127A12, 4) != at(stock, 0x08127A12, 4):
        expect_branch(image, md, 0x08127A12, 'bl', clear_match & ~1)
    # PL: a project load while playing waits for the pattern's end
    load_request, load_done, proj_service, save_gate = struct.unpack_from('<4I', image, blob_off + 0x1D8)
    if at(image, 0x08149DC6, 4) != at(stock, 0x08149DC6, 4):
        expect_branch(image, md, 0x08149DC6, 'bl', load_request & ~1)
        expect_branch(image, md, 0x08119A70, 'bl', proj_service & ~1)
        expect_branch(image, md, 0x081516A2, 'bl', save_gate & ~1)
        expect_bytes(image, 0x0811F938, struct.pack('<I', load_done | 1), 'PL: load completion invoker')
    # PJ: a journal cut short ends at its first unknown id; no first event fails as 0xA4
    boot_load, project_fail = struct.unpack_from('<2I', image, blob_off + 0x1E8)
    if at(image, 0x08123CAE, 4) != at(stock, 0x08123CAE, 4):
        expect_bytes(image, 0x08123CAE, bytes.fromhex('0023bde7'), 'PJ: reader, no factory -> no event')
        expect_bytes(image, 0x08124406, bytes.fromhex('a421'), 'PJ: movs r1, #0xA4')
        expect_branch(image, md, 0x08124408, 'bl', project_fail & ~1)
        expect_bytes(image, 0x0812440C, bytes.fromhex('a3e700bf'), 'PJ: b 0x08124356 (the replay returns)')
    # RG: a pattern rotate stays inside the 128 step buckets
    rotate_guard = struct.unpack_from('<I', image, blob_off + 0x1F0)[0]
    if at(image, 0x08145EE0, 4) != at(stock, 0x08145EE0, 4):
        expect_branch(image, md, 0x08145EE0, 'b', rotate_guard & ~1)
    # LV: the LED level preset the image starts on (np_led_smooth levels the targets)
    led_level = struct.unpack_from('<I', image, blob_off + 0x1F4)[0]
    if not 1 <= led_level <= 5:
        fail("LV: LED level preset %d is not 1..5" % led_level)
    # PB: the power-on load of slot 0 and the replay's three checks
    if at(image, 0x081199AE, 4) != at(stock, 0x081199AE, 4):
        expect_branch(image, md, 0x081199AE, 'bl', boot_load & ~1)
        for site in (0x08124166, 0x08124362, 0x0812436E):
            expect_branch(image, md, site, 'bl', project_fail & ~1)
    # V: the dim SEQ-key level, one immediate per renderer (default 2)
    dim = image[0x0814F05E - BASE]
    for address, reg in ((0x0814F0CE, 0x0A), (0x0814F1E4, 0x09), (0x0814F28C, 0x09), (0x0814F33C, 0x0B)):
        expect_bytes(image, address, bytes([0x4F, 0xF0, dim, reg]), 'SEQ dim level %d' % dim)
    expect_bytes(image, 0x0814F05F, bytes([0x27]), 'SEQ dim: movs r7')
    expect_branch(image, md, 0x081504FA, "bl", ui_begin & ~1)
    expect_branch(image, md, 0x0814A734, 'b', ui_modifier & ~1)
    for address, target in zip(
            (0x0814A314, 0x0814A33C, 0x0814A358, 0x0814A374, 0x0814A390, 0x0814A3AC),
            ui_helpers):
        expect_branch(image, md, address, 'b', target & ~1)
    for address, size in ((0x08155CE0, 4),
                          (0x08157670, 2), (0x081576BE, 2),
                          (0x081218E8, 2), (0x08150522, 4)):
        expect_bytes(image, address, at(stock, address, size),
                     "UI fix preserves release call site, project budget and exit callback")

    # Exact UI arms.  Each site is one `itete` carrying both counts: the
    # waveform arm (local 6, five as shipped) and the X-Mod type arm
    # (local 7, four).  Checking the whole pair catches a poke that landed on
    # the wrong arm and quietly shortened the other list.  Three
    # independently compiled UI implementations, all of which need both.
    for address, want, label in (
            (0x081500FE, bytes.fromhex("15bf0527042706260726"),
             "direct TYPE/WAVE cycle"),
            (0x08150550, bytes.fromhex("15bf4ff005084ff0040806270727"),
             "alternate TYPE/WAVE cycle"),
            (0x081506EA, bytes.fromhex("15bf042305234ff007094ff00609"),
             "batch TYPE/WAVE cycle"),
            (0x0815E8D6, bytes.fromhex("0400"), "generic type count"),
            (0x0815E8D4, bytes.fromhex("0500"), "waveform count, stock"),
            (0x08112C8C, bytes.fromhex("00bf"), "DSP splice tail")):
        expect_bytes(image, address, want, label)

    # Ratio mode is unison at the played MIDI note: its only note scale is
    # one table-position semitone (1/127), with no old C1/A3 anchor literal.
    xmod_body = at(image, xmod_pos & ~1, 0x100)
    if struct.pack("<f", 1.0 / 127.0) not in xmod_body:
        fail("X-Mod ratio splice has no 1/127 semitone scale")
    if struct.pack("<f", 24.0) in xmod_body or struct.pack("<f", 57.0) in xmod_body:
        fail("X-Mod ratio splice still contains a reference-note anchor")

    for address, want, label in (
            (0x0810903C, bytes.fromhex("0f21"), "QSPI priority 15"),
            (0x081093AA, bytes.fromhex("0221"), "USART1 priority 2"),
            (0x0810F400, bytes.fromhex("0521"), "audio priority stock 5 (K off by default)"),
            (0x0815EA38, struct.pack("<I", 0x3C000), "240 KiB heap")):
        expect_bytes(image, address, want, label)

    # The fourth X-Mod type must leave no trace in the shared mode byte's
    # consumers: these three stock sites used to be widened and no longer are.
    for address, length, what in ((0x081562C8, 2, "setMode bound"),
                                  (0x0811582E, 6, "evaluator audio test A"),
                                  (0x08115BBC, 4, "evaluator audio test B")):
        off = address - BASE
        if image[off:off + length] != stock[off:off + length]:
            fail("%s at 0x%08X was modified; the ratio type must publish "
                 "mode 2 and leave every stock reader alone" % (what, address))

    # No accidental changes inside the stock application.  The allowed spans
    # are exact patch sites; the appended NPAT blob is beyond STOCK_SIZE.
    spans = ((0x0810000C, 4), (0x08100010, 4), (0x08100014, 4),
             (0x08100018, 4), (0x0810003C, 4), (0x08100060, 4),
             (0x0810006C, 4), (0x08100108, 4),
             (0x0810903C, 2), (0x081093AA, 2), (0x0810EEA0, 4),
             (0x08112C88, 6),   # 0x0810F400 (K) is stock by default now
             (0x08119678, 4), (0x08119A02, 4),
             (0x081196A0, 4), (0x081196B8, 4), (0x08119720, 4),
             (0x081172B0, 4), (0x0811AC90, 4),
             (0x0811C6E0, 4), (0x0811C892, 4), (0x081112F8, 4),
             (0x08111508, 4), (0x08110B18, 4), (0x08110D34, 4),
             (0x0811BBA8, 4), (0x0811BD48, 4), (0x0811CC70, 4),
             (0x08150102, 2), (0x08150556, 4), (0x081506EC, 2),
             (0x081504FA, 4), (0x08155C7C, 4),
             (0x0814A314, 4), (0x0814A33C, 4), (0x0814A358, 4),
             (0x0814A374, 4), (0x0814A390, 4), (0x0814A3AC, 4),
             (0x0814A734, 4), (0x08143C14, 4),
             (0x0811C69E, 4), (0x0811129A, 4), (0x08110ABA, 4),
             (0x0811271E, 4), (0x08112952, 4), (0x08112F34, 4), (0x0811255A, 4),
             (0x08112DDA, 4), (0x0810A002, 4), (0x0810A11E, 4), (0x0810A166, 8),
             (0x08157558, 4), (0x0811972A, 8), (0x08119A60, 4), (0x0815766C, 4),
             # Y: every slot-evaluator call site, and the per-sample setRate
             (0x08111858, 4), (0x08111D08, 4), (0x081127D4, 4), (0x08112BF4, 4),
             (0x08112D16, 4), (0x08112D28, 4), (0x0811317E, 4), (0x081131C8, 4),
             (0x08112CEC, 4), (0x08112CFC, 4), (0x08112CD8, 4),
             # Y3: the audio-rate slot walk's entry jump and its exit call
             (0x08112C4A, 6), (0x08112CE2, 4),
             # A2b: the project journal writer, fenced for the watchdog
             (0x0812407C, 4),
             # M2: the LED fade's smoothing pair
             (0x0810A098, 8),
             # S1: the pattern store's per-step sort; S2: main()'s two passes
             (0x08158708, 4), (0x08146E76, 4), (0x08146E82, 4),
             # T1: morph trigs; V: the dim SEQ-key level
             (0x08146804, 4),
             # Q1: paste deferred; MF1-MF5: MIDI flow, poly pressure
             (0x08157672, 4), (0x08119A2C, 4), (0x08108D4A, 4), (0x0811B754, 4),
             # N1: note start, per-sample DSP call; N2: mod-note replay
             (0x08116D30, 4), (0x0811503C, 4), (0x081209F4, 4),
             # CH: note-on call, note-off prologue; R1: the mod-note randomizer
             (0x08117AD0, 4), (0x08117594, 4), (0x0814476C, 4),
             # R2: the mod-note key press tail and both RECORD reads
             (0x081521AA, 4), (0x0814FEB8, 4), (0x081521FE, 4),
             # K1: the TEMPO+FUNC randomize handler's key check
             (0x081524D4, 4),
             # P1: the normal-mode key press / release thunks' branches
             (0x08147B98, 4), (0x08147BA0, 4),
             # D1: the clear-patch handler's record match
             (0x08127A12, 4),
             # PL: load request, completion invoker literal, project service, save key
             (0x08149DC6, 4), (0x0811F938, 4), (0x08119A70, 4), (0x081516A2, 4),
             # PJ: the journal reader, the first event; PB: the power-on load, three checks
             (0x08123CAE, 4), (0x08124406, 10),
             (0x081199AE, 4), (0x08124166, 4), (0x08124362, 4), (0x0812436E, 4),
             # RG: the rotate setup's tail call
             (0x08145EE0, 4),
             (0x0814F05E, 1), (0x0814F0D0, 1), (0x0814F1E6, 1), (0x0814F28E, 1),
             (0x0814F33E, 1), (0x0814F0F0, 1),
             (0x08109DF4, 4), (0x0814F80C, 4), (0x08157324, 4), (0x08157528, 4),
             (0x08156022, 4), (0x0815602A, 4),              (0x08156656, 4), (0x0815669A, 4), (0x081566D2, 4),
             (0x0815670E, 4), (0x0815906C, 4), (0x08159070, 4),
             (0x081591D4, 4), (0x0815E8D6, 2), (0x0815EA38, 4))
    if ev_log:                                    # EV: the X-Env trigger, logged
        spans = spans + ((0x08156074, 6),)
    allowed = {address + i for address, length in spans for i in range(length)}
    changed = {BASE + i for i, (a, b) in enumerate(zip(stock, image)) if a != b}
    unexpected = sorted(changed - allowed)
    if unexpected:
        fail("unexpected stock-image changes at " +
             ", ".join("0x%08X" % a for a in unexpected))
    # The number of differing bytes inside a valid branch instruction is not
    # stable: moving a function inside the appended blob may make another
    # encoded byte coincidentally equal to the stock byte.  Check that every
    # intended span was actually touched instead of pinning that accidental
    # Hamming distance to one build (the previous value 116 became 119 after
    # a source-only LFO change even though the patch-site set was identical).
    untouched = [address for address, length in spans
                 if not any(address + i in changed for i in range(length))]
    if untouched:
        fail("intended patch spans left untouched at " +
             ", ".join("0x%08X" % a for a in untouched))

    print("PASS: static image, branch targets and SysEx round-trip verified")
    print("changed stock bytes inside approved spans: %d" % len(changed))
    print("patched SHA-256: " + hashlib.sha256(image).hexdigest())
    print("SysEx SHA-256:   " + hashlib.sha256(syx).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
