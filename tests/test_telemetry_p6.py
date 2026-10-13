"""P6: real scheduling/encoding, immutable records, full indices and CRC.

Boots the final image. Peripheral transport here completes instantaneously;
wire timing and busy/error cases live in separate independent tests.
"""
import struct
import sys
from pathlib import Path
from unicorn import UC_HOOK_CODE
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import diagmidi
import sysemu
from test_telemetry import RingTap

D = 0x2000E400


def main(image):
    if not Path(image).read_bytes()[0x75205] & 0x08:          # header +5, FLAG_TL
        print('SKIP -- this image was built without --telemetry')
        return 0
    # Old boot caches contain RAM but no SPI/RCC registers. A cache restore
    # would silently test an unconfigured peripheral; always boot this test.
    m = sysemu.Mono(image).boot()
    m.settle(50)
    m.factory_defaults()
    uc = m.uc
    def get(a): return struct.unpack('<I', uc.mem_read(a, 4))[0]
    def put(a, v): uc.mem_write(a, struct.pack('<I', v))
    assert get(0x4001340C) == 0x07420060, 'runtime SPI configuration hook not reached'
    assert get(0x40013404) & 0xFFFF == 3
    # Independently count the real call sites, not direct diagnostic calls.
    counts = {'enqueue': 0, 'dispatch': 0, 'render': 0}
    def counter(name, only_ui):
        def hook(uc, a, size, data):
            if not only_ui or uc.reg_read(sysemu.UC_ARM_REG_R0) == sysemu.UI:
                counts[name] += 1
        return hook
    baseline = [get(D + off) for off in (0xD6C, 0xD70, 0xD80)]
    hooks = [uc.hook_add(UC_HOOK_CODE, counter(name, only_ui), begin=a, end=a)
             for name, only_ui, a in [('enqueue', True, 0x08157324),
                                      ('dispatch', True, 0x0815748C),
                                      ('render', False, 0x0814F80C)]]
    pots0, sw0 = get(D + 0xD74), get(D + 0xD78)
    m.tap(34, audio=1)
    m.pot_values[0] = 3500
    m.settle(80, audio=1)
    assert get(D + 0xD74) > pots0, 'pot vtable/field recognition wrong'
    assert get(D + 0xD78) > sw0, 'switch vtable recognition wrong'
    observed = [get(D + off) - b for off, b in zip((0xD6C, 0xD70, 0xD80), baseline)]
    assert observed == list(counts.values()) and min(observed) > 0, (observed, counts)
    assert get(D + 0xD70) == get(D + 0xD88), 'dispatch did not return'
    for h in hooks: uc.hook_del(h)
    print('real UI counters agree with independent call-site trace:', counts, flush=True)

    midi = m.call_preserving(0x0811A6A4)
    tap = RingTap(m, midi)
    service = get(0x081752A4)
    put(D + 0x90, 0)   # no deferred USB during isolated encoding test
    put(D + 0x130, 0); put(D + 0x134, 0); put(D + 0x13C, 100)
    put(D + 0x138, 19000); put(0x20000174, 20000)
    # Actual UI/SPI witnesses exist. Supply deliberately distinctive snapshot
    # metadata too, so accidental numerical equality cannot hide a wrong read.
    put(D + 0xD6C, 0x12345678)
    for pot in range(23): put(D + 0x1280 + 4 * pot, 0)
    put(D + 0x1280 + 4 * 9, 685)
    put(D + 0x1280 + 4 * 18, 11)
    m.call_preserving(service)  # idle -> freeze -> TL_P6, no fragments yet
    assert get(D + 0x130) == 5
    N = diagmidi.P6_WORDS[8]
    expected = list(struct.unpack('<%dI' % N, uc.mem_read(D + 0x1000, 4 * N)))
    assert expected[1] == 8, 'snapshot schema'
    assert expected[125:127] == [(9 << 24) | 685, (1 << 9) | (1 << 18)]
    assert all(get(D + 0x1280 + 4 * p) == 0 for p in range(23))
    assert expected[18] == 0x12345678 and expected[3:5] == [101, 20000]
    assert expected[2] == get(0x08175348)
    assert diagmidi.p6_valid(expected), 'firmware CRC differs from independent zlib CRC'
    # Mutate source counters/images between bursts. The sent record must
    # remain the original snapshot, including high/low fragments.
    UWTICK = 0x20000174
    for i in range(400):                      # 641 CCs at 2 per ms
        put(midi + 0x808, get(midi + 0x80C))
        put(D + 0xD6C, i ^ 0xFEDCBA98)
        uc.mem_write(D + 0xE00, bytes([i & 0xFF]) * 72)
        put(UWTICK, get(UWTICK) + 1)          # the sender is paced by wall clock
        m.call_preserving(service)
        if get(D + 0x130) == 0: break
    assert len(tap.ccs) == 1 + 5 * N
    decoded = [(t, w) for t, w in diagmidi.records(tap.ccs) if t == diagmidi.P6]
    assert decoded == [(5, expected)] and diagmidi.p6_valid(decoded[0][1])
    # Every single fragment loss, 8/16 consecutive whole words lost, mutation,
    # duplicate words, leading/trailing clipping, concatenation, missing tag.
    for pos in range(1, len(tap.ccs)):
        broken = tap.ccs[:pos] + tap.ccs[pos+1:]
        w = diagmidi.records(broken)[0][1]
        wi = (pos - 1) // 5
        assert not diagmidi.p6_valid(w)
        assert w == [None if j == wi else v for j, v in enumerate(expected)]
    for n in (8, 16):
        a, b = 1 + 5*12, 1 + 5*(12+n)
        w = diagmidi.records(tap.ccs[:a] + tap.ccs[b:])[0][1]
        assert w == [None if 12 <= j < 12+n else v for j, v in enumerate(expected)]
        assert not diagmidi.p6_valid(w)
    damaged = tap.ccs.copy(); cc, value = damaged[20]; damaged[20] = cc, value ^ 1
    assert not diagmidi.p6_valid(diagmidi.records(damaged)[0][1])
    for damaged in (tap.ccs[:-1], tap.ccs[:11]+tap.ccs[6:11]+tap.ccs[11:],
                    tap.ccs+tap.ccs[1:]):
        assert not diagmidi.p6_valid(diagmidi.records(damaged)[0][1])
    assert diagmidi.records(tap.ccs + tap.ccs) == [(5, expected), (5, expected)]
    # schema 2 appends the parameter-indicator stages; the slot must be
    # in range and the packed fields decodable
    a, b, f0, f1 = expected[94:98]
    assert (a & 0xFF) < 18, 'selected slot %d' % (a & 0xFF)
    assert all(((a >> sh) & 31) <= 31 for sh in (8, 13, 18, 23))
    assert f0 != 0xFFFFFFFF and f1 != 0xFFFFFFFF, 'no CParam behind locals 0/1'
    v0, v1 = (struct.unpack('<f', struct.pack('<I', f))[0] for f in (f0, f1))
    assert 0.0 <= v0 <= 1.0 and 0.0 <= v1 <= 1.0, (v0, v1)
    # schema 3: the scanner's input side -- the gate must be open (main()
    # clears it), the DMA buffer must be the one the driver registered, and
    # pot 18's ring index must have advanced since boot
    a, d, c18, r18, b01, b2, cfgr = expected[98:105]
    assert (a >> 12) & 15 == 0, 'pot event gate %d' % ((a >> 12) & 15)
    assert b01 != 0xFFFFFFFF and (b2 & 0xFFFF) != 0xFFFF, 'DMA buffer not in D2 SRAM'
    assert (r18 >> 16) > 0, 'pot 18 ring never written'
    assert cfgr & 3 == 3, 'ADC1 DMNGT %d' % (cfgr & 3)
    print('P6 telemetry PASS: %d CCs, CRC, frozen snapshot, %d individual losses, '
          '8/16-word gaps, corruption, duplication and clipping; indicators slot %d '
          'local0 %.3f local1 %.3f' % (1 + 5 * N, 5 * N, a & 0xFF, v0, v1), flush=True)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'build/firmware-patched.bin')
