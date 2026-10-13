# Synced LFO — the ten divisions

Read out of the shipped image: the stock table at `0x0815F320`, ten of its
fifteen entries selected by `kDivSelect` in `patch/np_lfo.cpp`.

The knob picks the index as `floor(value x 10)`, so each division owns 10 %
of the travel. One LFO cycle is exactly one of these note values. Rate is
`transport_tick_rate / division`; a quarter note is 1024 engine ticks, so at
120 BPM the tick rate is 2048.

| # | knob | division | ticks | Hz @120 | cycle @120 | F8 pulses |
|---|------|----------|-------|---------|------------|-----------|
| 0 | 0–10 % | `1/1.` | 6144.00 | 0.333 | 3.00 s | 144 |
| 1 | 10–20 % | `1/1` | 4096.00 | 0.500 | 2.00 s | 96 |
| 2 | 20–30 % | `1/2.` | 3072.00 | 0.667 | 1.50 s | 72 |
| 3 | 30–40 % | `1/2` | 2048.00 | 1.000 | 1.00 s | 48 |
| 4 | 40–50 % | **`1/4`** | 1024.00 | 2.000 | 500 ms | 24 |
| 5 | 50–60 % | `1/4T` | 682.67 | 3.000 | 333 ms | 16 |
| 6 | 60–70 % | `1/8` | 512.00 | 4.000 | 250 ms | 12 |
| 7 | 70–80 % | `1/8T` | 341.33 | 6.000 | 167 ms | 8 |
| 8 | 80–90 % | **`1/16`** | 256.00 | 8.000 | 125 ms | 6 |
| 9 | 90–100 % | `1/32` | 128.00 | 16.000 | 62 ms | 3 |

`.` is dotted, `T` is triplet. Everything scales with tempo: at 60 BPM every
rate halves, at 240 it doubles.

## Reading it off the knob

Bottom to top, one per tenth of the travel:

```
1/1.  1/1  1/2.  1/2  1/4  1/4T  1/8  1/8T  1/16  1/32
```

`1/4` is the middle of the knob, `1/16` is the ninth of ten.

## What was removed and why it simplified things

Dropped from the stock fifteen: `2/1`, `1/4.`, `1/16Q`, `1/32T`, `1/64`.

Those five included both divisions whose period was not a whole number of
MIDI clock pulses — the quintuplet at 24/5 and 1/64 at 3/2 — so the special
case that made them realign only every 24 and every 3 pulses is gone with
them. All ten that remain retrigger once per cycle, exactly on their own
boundary.

## Square

The square starts its cycle **high**, so after a retrigger the beat lands on
the rising edge and the falling edge sits halfway through the division. Stock
was the other way round. The two polyBLEP corrections swap sign with it, so
the band limiting is unchanged — measured at 17 dB less alias energy than the
naive square either way.
