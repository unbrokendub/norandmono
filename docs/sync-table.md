# Synced LFO — the eleven divisions

Ten are the stock table at `0x0815F320`, ten of its fifteen entries; the
eleventh, `1/8.`, is not in the stock table and is added. All eleven are in
`kDivTicks` in `patch/np_lfo.cpp`.

The knob picks the index as `floor(value x 11)`, so each division owns
9.1 % of the travel. One LFO cycle is exactly one of these note values. Rate
is `transport_tick_rate / division`; a quarter note is 1024 engine ticks, so
at 120 BPM the tick rate is 2048.

| # | knob | division | ticks | Hz @120 | cycle @120 | F8 pulses |
|---|------|----------|-------|---------|------------|-----------|
| 0 | 0–9.1 % | `1/1.` | 6144.00 | 0.333 | 3.00 s | 144 |
| 1 | 9.1–18.2 % | `1/1` | 4096.00 | 0.500 | 2.00 s | 96 |
| 2 | 18.2–27.3 % | `1/2.` | 3072.00 | 0.667 | 1.50 s | 72 |
| 3 | 27.3–36.4 % | `1/2` | 2048.00 | 1.000 | 1.00 s | 48 |
| 4 | 36.4–45.5 % | **`1/4`** | 1024.00 | 2.000 | 500 ms | 24 |
| 5 | 45.5–54.5 % | `1/8.` | 768.00 | 2.667 | 375 ms | 18 |
| 6 | 54.5–63.6 % | `1/4T` | 682.67 | 3.000 | 333 ms | 16 |
| 7 | 63.6–72.7 % | `1/8` | 512.00 | 4.000 | 250 ms | 12 |
| 8 | 72.7–81.8 % | `1/8T` | 341.33 | 6.000 | 167 ms | 8 |
| 9 | 81.8–90.9 % | **`1/16`** | 256.00 | 8.000 | 125 ms | 6 |
| 10 | 90.9–100 % | `1/32` | 128.00 | 16.000 | 62 ms | 3 |

`.` is dotted, `T` is triplet. Everything scales with tempo: at 60 BPM every
rate halves, at 240 it doubles.

## Reading it off the knob

Bottom to top, one per eleventh of the travel:

```
1/1.  1/1  1/2.  1/2  1/4  1/8.  1/4T  1/8  1/8T  1/16  1/32
```

`1/8.` is the middle of the knob, `1/4` just below it, `1/16` the tenth of
eleven.

## The dotted eighth

`1/8.` is 768 ticks, 18 MIDI clock pulses: a whole number, so it retriggers
exactly on its own boundary like the others. It sits between `1/4` and `1/4T`
in length order. The centre of each of the earlier ten 10 % detents still
selects the division it did, so a knob left in the middle of a division, and
every division the instrument generator writes (`np_mnrand.cpp`, checked by a
`static_assert`), are unchanged. Only a hand-set knob near an edge of `1/4` or
`1/4T` (roughly 45–55 %) moves to `1/8.`, and a few positions near the other
edges move to their neighbour.

## What was removed and why it simplified things

Dropped from the stock fifteen: `2/1`, `1/4.`, `1/16Q`, `1/32T`, `1/64`.

Those five included both divisions whose period was not a whole number of
MIDI clock pulses — the quintuplet at 24/5 and 1/64 at 3/2 — so the special
case that made them realign only every 24 and every 3 pulses is gone with
them. All ten that remain, and the added `1/8.`, retrigger once per cycle,
exactly on their own boundary.

## Square

The square starts its cycle **high**, so after a retrigger the beat lands on
the rising edge and the falling edge sits halfway through the division. Stock
was the other way round. The two polyBLEP corrections swap sign with it, so
the band limiting is unchanged — measured at 17 dB less alias energy than the
naive square either way.
