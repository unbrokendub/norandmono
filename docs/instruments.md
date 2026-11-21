# Instruments

The 54 instruments the mod-note generator builds, in catalogue order (the number is what
`D_MNRAND_FORCE` and the tests use).  "Black keys" lists the held black key or pair that
picks it; a set of keys that has no pair of its own picks the union of its single keys.

| # | Name | What it is | Weight | Black keys |
|---|---|---|---|---|
| 0 | kick | kick: 808 boom, punch, hard, sub, techno click, FM and pinged | 12 | C# |
| 1 | snare | snare | 9 | C#+D# |
| 2 | clap | clap | 6 | C#+D# |
| 3 | hat-closed | closed hi-hat | 9 | C#+D# |
| 4 | hat-open | open hi-hat | 5 | C#+D# |
| 5 | tom | tom (in a kit, tuned over the kick) | 5 | D# |
| 6 | perc | percussion: metal can, AM ping, zip, blip, metal ping, triangle, tick | 4 | D# |
| 7 | bass | bass: sub, unison, octave, fifth, FM pluck, sync sweep | 9 | F# |
| 8 | acid | 303-style acid bass | 5 | F#+G# |
| 9 | lead | lead | 4 | G# |
| 10 | pad | pad | 4 | G# |
| 11 | pluck | pluck | 5 | G#, G#+A# |
| 12 | bell | FM bell | 4 | G#, G#+A# |
| 13 | zap | zap | 4 | A# |
| 14 | stab | two-note stab in the scale | 4 | G#, G#+A# |
| 15 | glitch | glitch | 5 | A# |
| 16 | cymbal | cymbal | 4 | D# |
| 17 | shaker | shaker | 4 | D# |
| 18 | drop | drop: a pinged filter falling | 3 | D# |
| 19 | fm-bass | FM bass: DX ratio, analog TZFM, 4-operator | 6 | F#, D#+F# |
| 20 | reese | reese bass | 4 | F# |
| 21 | wobble | wobble bass (tempo-synced LFO) | 5 | F#+G# |
| 22 | growl | growl bass (filter FM) | 4 | F#+G# |
| 23 | sync-lead | sync lead | 4 | G# |
| 24 | hoover | hoover | 3 | G# |
| 25 | phaser-pad | phaser pad | 3 | G# |
| 26 | gate | tempo-gated pad/lead | 4 | G# |
| 27 | fm-ep | FM electric piano | 4 | G#, G#+A# |
| 28 | mallet | mallet | 4 | G#, G#+A# |
| 29 | bleeps | bleeps | 4 | A# |
| 30 | riser | riser | 3 | F#+A# |
| 31 | siren | siren | 3 | F#+A# |
| 32 | drone | drone | 3 | G# |
| 33 | metal | metallic percussion | 4 | D# |
| 34 | am-bell | AM bell | 3 | G#, G#+A# |
| 35 | talk | talking formant | 3 | G# |
| 36 | wind | wind | 2 | F#+A# |
| 37 | crackle | crackle | 2 | F#+A# |
| 38 | 808 | 808 bass | 5 | F# |
| 39 | cowbell | cowbell: 808, clank, choked | 3 | D# |
| 40 | rim | rimshot, clave, wood block | 4 | D# |
| 41 | conga | conga, bongo, timbale | 4 | D# |
| 42 | chord | dub chord | 4 | G#, G#+A# |
| 43 | syncussion | a Pearl Syncussion SY-1 channel: modes A–F, one sweep on both VCOs and the filter, LFO trill or S&H | 4 | C#+F# |
| 44 | organ | drawbar organ: two sines, percussion, key click, Leslie | 4 | G# |
| 45 | gong | gong, tam-tam: FM that blooms after the stroke | 3 | G#, G#+A# |
| 46 | bounce | bouncing octave: a held bass note hopping root/octave in time | 4 | F#+G# |
| 47 | vargan | jaw harp: a low reed through a moving resonant filter | 3 | G# |
| 48 | rumble | techno rumble: a low tone and noise under a dark low-pass, pumping with the beat | 3 | F#, F#+A# |
| 49 | brass | synth brass: saws, the filter blatted open, a scoop up into the note | 4 | G# |
| 50 | flute | flute, pan flute: a soft tone, breath and chiff, late vibrato | 3 | G# |
| 51 | bowl | singing bowl: beating sines or the bowl's second mode, struck or rubbed | 3 | G#, G#+A# |
| 52 | sub | sub bass: a sine two or three octaves down, its octave or twelfth quietly over it | 4 | F# |
| 53 | deep | deep bass: a dark square or saw plucked open, a sine an octave under it | 4 | F# |

Since p70, about 45 % of the basses (F# and F#+G#) are moved after their recipe: 20 % an
octave down, 15 % keep the note with a sine an octave under it, 10 % a fifth down (an
octave on a bass kit's key). Nothing sounds under C1.
