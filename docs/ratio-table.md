# X-Mod type 4 — the ratio table

Read out of the shipped image: **128 detents, two banks of 64**. The rate knob
picks one as `floor(value x 128)`, so each owns 0.78 % of the travel.

`position = ratioOffset[detent] + (note + transpose) / 127`, and the offset
column is `12 * log2(ratio) / 127` — the ratio as a table position, so
detent 3 (ratio 1.00) is exact unison with the played note.

## Bank A, detents 0–63 — quarters, 0.25 to 16.00

Every value is a rational p/4, so every one is harmonic: the sidebands land
on multiples of carrier/4, carrier/2 or the carrier, and the tone keeps a
definite pitch. The half for basses, keys, plucks — anything that has to sit
in a chord.

| # | ratio | offset | # | ratio | offset | # | ratio | offset | # | ratio | offset |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 0.25 | -0.188976 | 16 | 4.25 | +0.197241 | 32 | 8.25 | +0.287659 | 48 | 12.25 | +0.341547 |
| 1 | 0.50 | -0.094488 | 17 | 4.50 | +0.205032 | 33 | 8.50 | +0.291729 | 49 | 12.50 | +0.344301 |
| 2 | 0.75 | -0.039216 | 18 | 4.75 | +0.212403 | 34 | 8.75 | +0.295680 | 50 | 12.75 | +0.347001 |
| 3 | 1.00 | +0.000000 | 19 | 5.00 | +0.219395 | 35 | 9.00 | +0.299520 | 51 | 13.00 | +0.349648 |
| 4 | 1.25 | +0.030418 | 20 | 5.25 | +0.226046 | 36 | 9.25 | +0.303255 | 52 | 13.25 | +0.352244 |
| 5 | 1.50 | +0.055272 | 21 | 5.50 | +0.232387 | 37 | 9.50 | +0.306891 | 53 | 13.50 | +0.354793 |
| 6 | 1.75 | +0.076286 | 22 | 5.75 | +0.238447 | 38 | 9.75 | +0.310432 | 54 | 13.75 | +0.357294 |
| 7 | 2.00 | +0.094488 | 23 | 6.00 | +0.244248 | 39 | 10.00 | +0.313883 | 55 | 14.00 | +0.359750 |
| 8 | 2.25 | +0.110544 | 24 | 6.25 | +0.249813 | 40 | 10.25 | +0.317249 | 56 | 14.25 | +0.362163 |
| 9 | 2.50 | +0.124907 | 25 | 6.50 | +0.255160 | 41 | 10.50 | +0.320534 | 57 | 14.50 | +0.364534 |
| 10 | 2.75 | +0.137899 | 26 | 6.75 | +0.260304 | 42 | 10.75 | +0.323742 | 58 | 14.75 | +0.366864 |
| 11 | 3.00 | +0.149760 | 27 | 7.00 | +0.265262 | 43 | 11.00 | +0.326875 | 59 | 15.00 | +0.369155 |
| 12 | 3.25 | +0.160671 | 28 | 7.25 | +0.270045 | 44 | 11.25 | +0.329939 | 60 | 15.25 | +0.371408 |
| 13 | 3.50 | +0.170774 | 29 | 7.50 | +0.274667 | 45 | 11.50 | +0.332935 | 61 | 15.50 | +0.373625 |
| 14 | 3.75 | +0.180179 | 30 | 7.75 | +0.279137 | 46 | 11.75 | +0.335867 | 62 | 15.75 | +0.375806 |
| 15 | 4.00 | +0.188976 | 31 | 8.00 | +0.283465 | 47 | 12.00 | +0.338737 | 63 | 16.00 | +0.377953 |

## Bank B, detents 64–127 — Yamaha style, 0.7071 to 16.9706

The irrational columns of the Yamaha four-operator coarse set: `n x m` for
m in {sqrt2, pi/2, sqrt3}, **n in halves**, the first 64 of the sorted grid.
With no rational relation between carrier and modulator the reflected
sidebands never coincide with the upright ones, there is no common divisor
and so no fundamental — a bell, a gong, struck metal.

| # | ratio | from | # | ratio | from | # | ratio | from | # | ratio | from |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 64 | 0.7071 | 0.5 x sqrt2 | 80 | 4.7124 | 3 x pi/2 | 96 | 8.6603 | 5 x sqrt3 | 112 | 12.9904 | 7.5 x sqrt3 |
| 65 | 0.7854 | 0.5 x pi/2 | 81 | 4.9497 | 3.5 x sqrt2 | 97 | 9.1924 | 6.5 x sqrt2 | 113 | 13.3518 | 8.5 x pi/2 |
| 66 | 0.8660 | 0.5 x sqrt3 | 82 | 5.1962 | 3 x sqrt3 | 98 | 9.4248 | 6 x pi/2 | 114 | 13.4350 | 9.5 x sqrt2 |
| 67 | 1.4142 | 1 x sqrt2 | 83 | 5.4978 | 3.5 x pi/2 | 99 | 9.5263 | 5.5 x sqrt3 | 115 | 13.8564 | 8 x sqrt3 |
| 68 | 1.5708 | 1 x pi/2 | 84 | 5.6569 | 4 x sqrt2 | 100 | 9.8995 | 7 x sqrt2 | 116 | 14.1372 | 9 x pi/2 |
| 69 | 1.7321 | 1 x sqrt3 | 85 | 6.0622 | 3.5 x sqrt3 | 101 | 10.2102 | 6.5 x pi/2 | 117 | 14.1421 | 10 x sqrt2 |
| 70 | 2.1213 | 1.5 x sqrt2 | 86 | 6.2832 | 4 x pi/2 | 102 | 10.3923 | 6 x sqrt3 | 118 | 14.7224 | 8.5 x sqrt3 |
| 71 | 2.3562 | 1.5 x pi/2 | 87 | 6.3640 | 4.5 x sqrt2 | 103 | 10.6066 | 7.5 x sqrt2 | 119 | 14.8492 | 10.5 x sqrt2 |
| 72 | 2.5981 | 1.5 x sqrt3 | 88 | 6.9282 | 4 x sqrt3 | 104 | 10.9956 | 7 x pi/2 | 120 | 14.9226 | 9.5 x pi/2 |
| 73 | 2.8284 | 2 x sqrt2 | 89 | 7.0686 | 4.5 x pi/2 | 105 | 11.2583 | 6.5 x sqrt3 | 121 | 15.5563 | 11 x sqrt2 |
| 74 | 3.1416 | 2 x pi/2 | 90 | 7.0711 | 5 x sqrt2 | 106 | 11.3137 | 8 x sqrt2 | 122 | 15.5885 | 9 x sqrt3 |
| 75 | 3.4641 | 2 x sqrt3 | 91 | 7.7782 | 5.5 x sqrt2 | 107 | 11.7810 | 7.5 x pi/2 | 123 | 15.7080 | 10 x pi/2 |
| 76 | 3.5355 | 2.5 x sqrt2 | 92 | 7.7942 | 4.5 x sqrt3 | 108 | 12.0208 | 8.5 x sqrt2 | 124 | 16.2635 | 11.5 x sqrt2 |
| 77 | 3.9270 | 2.5 x pi/2 | 93 | 7.8540 | 5 x pi/2 | 109 | 12.1244 | 7 x sqrt3 | 125 | 16.4545 | 9.5 x sqrt3 |
| 78 | 4.2426 | 3 x sqrt2 | 94 | 8.4853 | 6 x sqrt2 | 110 | 12.5664 | 8 x pi/2 | 126 | 16.4934 | 10.5 x pi/2 |
| 79 | 4.3301 | 2.5 x sqrt3 | 95 | 8.6394 | 5.5 x pi/2 | 111 | 12.7279 | 9 x sqrt2 | 127 | 16.9706 | 12 x sqrt2 |

## Headroom, and why the grid is halves

`position` is clamped to [0, 1] and the frequency table stops at 12543.9 Hz,
so a ratio stops separating above a note set by its own ceiling. The two
banks are kept balanced so neither is the weak link:

| bank | top ratio | flattens above |
|---|---|---|
| A, quarters | 16.00 | MIDI 79.0 |
| B, halves | 16.9706 | MIDI 78.0 |

A quarter grid in bank B would have been denser — 60 entries at or below
ratio 8 rather than 30 — but it caps at 8.49 and gives up the bright
metallic half entirely. Adding a fourth multiplier (sqrt5) reaches 17.89 and
is not part of the Yamaha set.

## The seam is deliberate

Detent 63 is 16.00 and detent 64 is 0.7071 — the table is **not** monotonic.
Turning past the middle of the knob drops the pitch and changes the character
from harmonic to inharmonic. That is the layout, not a mistake, and
`tests/test_patch.py` pins it down so it cannot be "fixed" by accident.
