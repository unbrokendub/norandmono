/* P6 diagnostic extension. All mutable storage is owned DTCM, never .bss. */
/* 0x1C00 since the X-Env event log (2026-10-02): 0x2000E400..0x2000FFFF.
 * The stack starts at 0x20020000 and was measured on sysemu to reach down
 * to 0x2001FC78; nothing in the stock image lives between .bss end
 * (0x2000E2DC) and the stack. */
#define NP_DIAG_SIZE 0x1C00
/* X-Env event log.  np_ev_put (np_patch.S) appends {uwTick ms, transport
 * tick position, type << 24 | bit mask}; one note triggers a dozen X-Envs
 * in a row, so an entry with the same type and millisecond that has not
 * been sent yet takes the new bit instead.  The sender (np_runtime.cpp)
 * copies up to 16 entries into the EV record (tag 7); the PM record (tag
 * 8) is a map of both tracks' 64 steps, every 5 s. */
#define D_EV_BASE     0x1400
#define D_EV_HEAD     (D_EV_BASE + 0x00)   /* entries written, monotonic */
#define D_EV_SENT     (D_EV_BASE + 0x04)   /* entries frozen for sending */
#define D_EV_LOST     (D_EV_BASE + 0x08)   /* overwritten before sending */
#define D_EV_LAST_MS  (D_EV_BASE + 0x0C)
#define D_PM_LAST_MS  (D_EV_BASE + 0x10)
#define D_EV_RING     (D_EV_BASE + 0x20)   /* NP_EV_RING_N x 12 bytes */
#define NP_EV_RING_N  128
#define D_EV_SNAP     (D_EV_BASE + 0x620)  /* NP_EV_WORDS */
#define D_PM_SNAP     (D_EV_BASE + 0x6F0)  /* NP_PM_WORDS */
#define NP_EV_WORDS   52
#define NP_PM_WORDS   52
#define NP_EV_PER_RECORD 16
/* event types, the top byte of an entry's third word */
#define EV_TRIG_NOTE   1   /* stock X-Env trigger from a note (0x0811612C)       */
#define EV_TRIG_PARAM  2   /* ...from a param trig / unlinked X-Env (0x08117FE8) */
#define EV_TRIG_OTHER  3   /* ...from anywhere else                               */
#define EV_RC_ATTACK   4   /* the RC envelope saw a new attack (entry bits)       */
#define EV_RC_DECAY    5   /* ...restarted from a decay-accumulator jump (p31)    */
/* Q1 / MIDI flow (p35): counters after the journal-writer fence words at
 * 0xF90..0xFA0 (np_patch.S); cleared on every boot with them. */
#define D_QSPI_DEFER    0xFA4   /* flash-touching paste events held back   */
#define D_MIDI_USB_HELD 0xFA8   /* fan-out waits: USB staging full         */
#define D_MIDI_DIN_HELD 0xFAC   /* fan-out waits (USB down): no DIN room   */
#define D_MIDI_RX_DROP  0xFB0   /* DIN bytes dropped, receive ring full    */
#define D_USB_RX_DROP   0xFB4   /* USB-MIDI packets dropped, ring full     */
#define D_MIDI_DIN_DROP 0xFB8   /* whole messages not copied to DIN (USB up) */
/* N1 (p36): a note start snaps the 19 slots' 100 Hz output smoothers */
#define D_SNAP          0xFBC   /* samples left with the smoothers bypassed */
#define D_SNAP_G        0xFC0   /* the stock coefficient they get back      */
#define D_SNAPS         0xFC4   /* note starts that requested a snap        */
/* CH (p37): an instrument switch fades the sounding one out first */
#define D_CHOKE_N       0xFC8   /* fade samples left (0: no fade)           */
#define D_CHOKE_L       0xFCC   /* the level the fade started from (float)  */
#define D_CHOKE_STARTED 0xFD0   /* the DSP has captured D_CHOKE_L           */
#define D_CHOKE_PEND    0xFD4   /* the deferred note-on event, or 0         */
#define D_CHOKE_PROJ    0xFD8   /* its handler's first argument             */
#define D_CHOKE_LAST0   0xFDC   /* last instrument per layer: id | 0x100    */
#define D_CHOKE_LAST1   0xFE0
#define D_CHOKES        0xFE4   /* fades started                            */
/* R1 (p38): the instrument-aware mod-note randomizer (np_mnrand.cpp) */
#define D_MNRAND_N      0xFE8   /* mod notes randomized                     */
#define D_MNRAND_LAST   0xFEC   /* instrument | note << 8 | params << 16    */
#define D_MNRAND_FORCE  0xFF0   /* test hook: 1 + instrument, 0 = random    */
/* R2 (p40): mod notes built by black + white, until the key's release:
 * that press and release record no step (bit per mod note 0..63) */
#define D_MNRAND_MARK0  0xFF4
#define D_MNRAND_MARK1  0xFF8
/* K1 (p41): drum kits built (TEMPO+FUNC+C#); bits 8.. bass kits (D#), 16..
 * full sets (A#), 24.. styled full sets (G#, p56) */
#define D_MNRAND_KITS   0xFFC
/* each mod note's instrument as last generated, + 1 (0: not generated);
 * 40 bytes in the free tail of the block, cleared every boot by
 * np_runtime_init */
#define D_MNRAND_KIND   0x1BC0
/* p42: the randomizer's seed state, kept across presses (cleared at boot) */
#define D_MNRAND_SEED   0x1BE8
/* P1 (p44): normal mode, TEMPO+FUNC+note builds the pattern's patch.
 * PMARK: keys 34..46 (bit key - 34) pressed as such a chord, until their
 * release (no step recorded); PKIND: the patch's instrument + 1, as last
 * built; PATCHES: patches built.  Cleared at boot with the tail. */
#define D_MNRAND_PMARK   0x1BEC
#define D_MNRAND_PKIND   0x1BF0
#define D_MNRAND_PATCHES 0x1BF4
/* PL (p47): a project load while playing waits for the pattern's end.
 * Bytes: [0] loads requested while playing, [1] their reads completed,
 * [2] requests the replay has caught up with.  [0] != [2]: one is
 * pending; [1] == [0]: its read is in. */
#define D_PLOAD          0x1BF8
/* p54: draws the randomizer threw away as mute (np_mnrand.cpp mute()), low
 * 24 bits; p55: the top byte is the style the last A# chose, + 1 */
#define D_MNRAND_MUTED   0x1BFC
/* PB (p60): the power-on load of slot 0.  Bytes: [0] 1 while it runs,
 * [1] the first check its journal failed (0xA1..0xA4; 0: none), [2] checks
 * failed, [3] 1: it started an empty project instead.  In the gap after
 * the slot-hold table (np_patch.S: D_BLOCKFRAC ends at 0x13E4). */
#define D_PBOOT          0x13F0
/* LV (p62): the LED level preset as TEMPO+FOLLOW last set it (1..4, 5 =
 * the stock's levels); 0 (every boot) means np_led_level, the build's */
#define D_LED_LEVEL      0x13F4
#define D_LED_PENDING 0xD50
#define D_LED_EOT 0xD54
#define D_LED_BUSY 0xD58
#define D_LED_FAULT 0xD5C
#define D_LED_AGE_MAX 0xD60
#define D_LED_ERROR_SR 0xD64
#define D_LED_CHANGED 0xD68
#define D_UI_ENQUEUE 0xD6C
#define D_UI_DISPATCH 0xD70
#define D_UI_POTS 0xD74
#define D_UI_SWITCHES 0xD78
#define D_UI_LAST_POT 0xD7C
#define D_UI_RENDER 0xD80
#define D_UI_LAST_SWITCH 0xD84
#define D_UI_DISPATCH_DONE 0xD88
#define D_LED_HAS_STARTED 0xD8C
#define D_LED_SUBMITTED 0xE00
#define D_LED_COMPLETED 0xE80
#define D_TL_SNAPSHOT 0x1000
/* schema 2 appended the parameter-indicator stages (4 words); schema 3
 * the pot scanner's input side (7 words); schema 4 the scan re-arm
 * counters of patch O (2 words); the CRC is always last */
#define D_SCANSKIP 0xD90
#define D_SCANOVR 0xD94
#define D_P18MIN 0xD98            /* pot 18 current reading, min|max<<16 since the last snapshot */
/* transition record (tag 6, every second): interval maxima per profile
 * kind, and every pot's min|max reading since the last record */
#define D_IMAX 0xD9C              /* 5 words: interval max cycles, P_AUDIO..P_MAIN */
#define D_TR_NEXT_MS 0xDB0
#define D_TR_SEQ 0xDB4
#define D_TR_SNAP 0xDB8           /* 18 words: the frozen record */
#define D_POTSPAN 0x1200          /* 24 words: min|max<<16 per pot (past the P6 snapshot) */
#define D_TR_LASTPOTS 0x1260      /* pot enqueue count at the last record */
#define D_TR_LASTDISP 0x1264
#define D_TR_LASTSKIP 0x1268
#define D_TR_LASTRING 0x126C      /* ring writes of the most-moved pot, last record */
#define D_UI_SERVICE_MAX 0x1270  /* bounded main UI service, lifetime max cycles */
#define D_UI_SERVICE_YIELDS 0x1274
#define D_PROJECT_FIRST 0x1278   /* positive-budget project service made progress */
#define D_POTSPAN_MS 0x127C      /* uwTick when the pot spans were last sampled */
#define D_POT_EVENTS 0x1280      /* 23 per-pot enqueue counts since P6 snapshot */
#define NP_TR_WORDS 18
/* schema 5 appends the small-pool census: thread fallbacks, big pool
 * used/high, and the four most common first words (vtables) among the
 * occupied small blocks with their counts (12 words) */
/* schema 6 appends the project event manager's backlog: pending count,
 * budget, walked list length (3 words) */
/* schema 7: UI service max/yields, project first-item count, busiest pot
 * (id in high byte / count in low 24 bits), active-pot bitmask. */
#define NP_TL_WORDS 128
