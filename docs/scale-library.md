# The scale library (p57)

In the scale setting, hold **PAGE A–D** and press **SEQ 1–16**: that page's custom scale
becomes the scale below.  Notes are shown from C; with another root everything shifts.
None of these duplicates a built-in scale.  The masks live in `kScaleLib`
(patch/np_mnrand.cpp); `tests/test_scale_library.py` checks all 64 against this list.

## PAGE A — the East: maqams, ragas, Balkan

| SEQ | Scale | Notes from C |
|---|---|---|
| 1 | Hijaz (Phrygian dominant) | C Db E F G Ab Bb |
| 2 | Persian | C Db E F F# Ab B |
| 3 | Oriental | C Db E F F# A Bb |
| 4 | Hungarian major | C Eb E F# G A Bb |
| 5 | Neapolitan minor | C Db Eb F G Ab B |
| 6 | Neapolitan major | C Db Eb F G A B |
| 7 | Romanian major | C Db E F# G A Bb |
| 8 | Arabian (major Locrian) | C D E F F# Ab Bb |
| 9 | Hindu (Mixolydian b6) | C D E F G Ab Bb |
| 10 | Raga Todi | C Db Eb F# G Ab B |
| 11 | Raga Marwa | C Db E F# G A B |
| 12 | Raga Purvi | C Db E F# G Ab B |
| 13 | Raga Ahir Bhairav | C Db E F G A Bb |
| 14 | Raga Hamsadhwani | C D E G B |
| 15 | Raga Malkauns | C Eb F Ab Bb |
| 16 | Egyptian | C D F G Bb |

## PAGE B — pentatonic and hexatonic

| SEQ | Scale | Notes from C |
|---|---|---|
| 1 | Minor pentatonic | C Eb F G Bb |
| 2 | Major pentatonic | C D E G A |
| 3 | Hirajoshi | C D Eb G Ab |
| 4 | In-sen | C Db F G Bb |
| 5 | Iwato | C Db F F# Bb |
| 6 | Kumoi | C D Eb G A |
| 7 | Pelog | C Db Eb G Ab |
| 8 | Yo | C D F G A |
| 9 | Chinese | C E F# G B |
| 10 | Dominant pentatonic | C D E G Bb |
| 11 | Minor 6 pentatonic | C Eb F G A |
| 12 | Major blues | C D Eb E G A |
| 13 | Whole tone | C D E F# Ab Bb |
| 14 | Prometheus | C D E F# A Bb |
| 15 | Augmented | C Eb E G Ab B |
| 16 | Tritone | C Db E F# G Bb |

## PAGE C — modes and jazz

| SEQ | Scale | Notes from C |
|---|---|---|
| 1 | Lydian | C D E F# G A B |
| 2 | Locrian | C Db Eb F F# Ab Bb |
| 3 | Lydian dominant | C D E F# G A Bb |
| 4 | Altered (super Locrian) | C Db Eb E F# Ab Bb |
| 5 | Dorian b2 | C Db Eb F G A Bb |
| 6 | Lydian augmented | C D E F# Ab A B |
| 7 | Locrian natural 2 (half-diminished) | C D Eb F F# Ab Bb |
| 8 | Harmonic major | C D E F G Ab B |
| 9 | Locrian natural 6 | C Db Eb F F# A Bb |
| 10 | Ionian #5 | C D E F Ab A B |
| 11 | Lydian #2 | C Eb E F# G A B |
| 12 | Ultralocrian | C Db Eb E F# Ab A |
| 13 | Lydian minor | C D E F# G Ab Bb |
| 14 | Bebop dominant | C D E F G A Bb B |
| 15 | Bebop major | C D E F G Ab A B |
| 16 | Bebop Dorian | C D Eb E F G A Bb |

## PAGE D — symmetric, Messiaen modes, chord tones

| SEQ | Scale | Notes from C |
|---|---|---|
| 1 | Diminished (half-whole) | C Db Eb E F# G A Bb |
| 2 | Diminished (whole-half) | C D Eb F F# Ab A B |
| 3 | Enigmatic | C Db E F# Ab Bb B |
| 4 | Neapolitan Prometheus | C Db E F# A Bb |
| 5 | Messiaen mode 3 | C D Eb E F# G Ab Bb B |
| 6 | Messiaen mode 4 | C Db D F F# G Ab B |
| 7 | Messiaen mode 5 | C Db F F# G B |
| 8 | Messiaen mode 6 | C D E F F# Ab Bb B |
| 9 | Messiaen mode 7 | C Db D Eb F F# G Ab A B |
| 10 | Major triad | C E G |
| 11 | Minor triad | C Eb G |
| 12 | Minor 7th | C Eb G Bb |
| 13 | Dominant 7th | C E G Bb |
| 14 | Fifths | C G |
| 15 | Fourths | C F Bb |
| 16 | Cluster | C Db D |
