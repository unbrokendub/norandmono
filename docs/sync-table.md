# Synced LFO — the twelve divisions

Eleven come from the stock table at `0x0815F320`, including the restored
`1/4.`; `1/8.` is not in the stock table and is added. All twelve are in
`kDivTicks` in `patch/np_lfo.cpp`.

The knob picks the index as `floor(value x 12)`, clamped to 11 at the top,
so each division owns 8.33 % of the travel. One LFO cycle is one note value. Rate
is `transport_tick_rate / division`; a quarter note is 1024 engine ticks, so
at 120 BPM the tick rate is 2048.

| # | knob | division | ticks | Hz @120 | cycle @120 | F8 pulses |
|---|------|----------|-------|---------|------------|-----------|
| 0 | 0–8.3 % | `1/1.` | 6144.00 | 0.333 | 3.00 s | 144 |
| 1 | 8.3–16.7 % | `1/1` | 4096.00 | 0.500 | 2.00 s | 96 |
| 2 | 16.7–25 % | `1/2.` | 3072.00 | 0.667 | 1.50 s | 72 |
| 3 | 25–33.3 % | `1/2` | 2048.00 | 1.000 | 1.00 s | 48 |
| 4 | 33.3–41.7 % | **`1/4.`** | 1536.00 | 1.333 | 750 ms | 36 |
| 5 | 41.7–50 % | `1/4` | 1024.00 | 2.000 | 500 ms | 24 |
| 6 | 50–58.3 % | **`1/8.`** | 768.00 | 2.667 | 375 ms | 18 |
| 7 | 58.3–66.7 % | `1/4T` | 682.67 | 3.000 | 333 ms | 16 |
| 8 | 66.7–75 % | `1/8` | 512.00 | 4.000 | 250 ms | 12 |
| 9 | 75–83.3 % | `1/8T` | 341.33 | 6.000 | 167 ms | 8 |
| 10 | 83.3–91.7 % | `1/16` | 256.00 | 8.000 | 125 ms | 6 |
| 11 | 91.7–100 % | `1/32` | 128.00 | 16.000 | 62 ms | 3 |

`.` is dotted, `T` is triplet. Everything scales with tempo: at 60 BPM every
rate halves, at 240 it doubles.

## Reading it off the knob

Bottom to top, one per twelfth of the travel:

```
1/1.  1/1  1/2.  1/2  1/4.  1/4  1/8.  1/4T  1/8  1/8T  1/16  1/32
```

`1/8.` is just above the middle of the knob; `1/4.` is around 37.5 %.

## Dotted notes and compatibility

`1/8.` is 768 ticks, 18 MIDI clock pulses: a whole number, so it retriggers
exactly on its own boundary like the others. It sits between `1/4` and `1/4T`
in length order. `1/4.` is 1536 ticks and 36 pulses, between `1/2` and `1/4`.

Saved raw Rate values are not migrated. Changing from ten or eleven detents
to twelve can change their selected division. In particular, four old
ten-detent centres move: `1/2.` to `1/2`, `1/2` to `1/4.`, `1/4T` to `1/8.`,
and `1/8` to `1/4T`. Other values near boundaries can move as well.

Newly generated instruments keep their intended durations: `division()` in
`np_mnrand.cpp` maps the recipes' ten logical IDs to centres on the new knob,
with a compile-time assertion. The recipes do not automatically add dotted
choices to their random ranges.

## What was removed and why it simplified things

Dropped from the stock fifteen: `2/1`, `1/16Q`, `1/32T`, `1/64`.

Those four include both divisions whose period was not a whole number of
MIDI clock pulses — the quintuplet at 24/5 and 1/64 at 3/2 — so the special
case that made them realign only every 24 and every 3 pulses is gone with
them. All eleven that remain, and the added `1/8.`, retrigger once per cycle,
exactly on their own boundary.

## Square

The square starts its cycle **high**, so after a retrigger the beat lands on
the rising edge and the falling edge sits halfway through the division. Stock
was the other way round. The two polyBLEP corrections swap sign with it, so
the band limiting is unchanged — measured at 17 dB less alias energy than the
naive square either way.
