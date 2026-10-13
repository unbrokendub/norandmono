/*
 * np_patch.S -- code appended to the Norand Mono application image.
 *
 * Addresses the findings in docs/audit.md that can be fixed without the
 * original source:
 *
 *   A1  real fault handlers: record what happened, then reset
 *   A2  IWDG watchdog, armed only once the main loop is running
 *   A4  _sbrk returns -1 on a full heap instead of executing a breakpoint
 *   B1  _sbrk records the heap high-water mark, so the remaining margin
 *       becomes a measurement rather than a guess
 *   C   instruction cache enabled before the C++ static constructors run
 *   E1  optional CRC32 self check at boot (off unless the tool enables it)
 *
 * Linked at a fixed address past the end of the stock image; tools/patch.py
 * rewrites four vector table entries and four instructions to reach it, and
 * fills in the configuration words in the header below.
 *
 * Nothing here allocates and nothing depends on application state, so every
 * entry point is safe to run before the application is initialised.
 */

/* Mirrors NP_XMOD_FOLLOW in np_lfo.cpp; tools/patch.py sets both together. */
#ifndef NP_XMOD_FOLLOW
#define NP_XMOD_FOLLOW 0
#endif
#include "np_diag.h"

    .syntax unified
    .cpu cortex-m7
    .fpu fpv5-d16
    .thumb

/* ------------------------------------------------------------------ */
/* Diagnostic block, in DTCM past the application's .bss end           */
/* (0x2000E2DC; its last word, 0x2000E2D8, is the sbrk errno slot, so  */
/* keep clear of it).  SRAM keeps its contents across a warm reset, so */
/* the record survives the reset the fault handler performs.           */
/* ------------------------------------------------------------------ */
    .equ DIAG,       0x2000E400
    .equ DIAG_MAGIC, 0x4E4F5246          /* 'NORF' */

    .equ D_MAGIC,    0x00
    .equ D_FAULT,    0x04                /* 1 hard, 2 mem, 3 bus, 4 usage */
                                         /* 5 fixed-pool exhaustion       */
    .equ D_SP,       0x08
    .equ D_PC,       0x0C
    .equ D_LR,       0x10
    .equ D_PSR,      0x14
    .equ D_CFSR,     0x18
    .equ D_HFSR,     0x1C
    .equ D_FAULTS,   0x20                /* faults since power on        */
    .equ D_CRC_EXP,  0x24
    .equ D_CRC_GOT,  0x28
    .equ D_BOOTS,    0x2C                /* boots since power on         */
    .equ D_WDGON,    0x30                /* watchdog armed this boot     */
    .equ D_HEAPMAX,  0x34                /* highest brk ever reached     */
    .equ D_LFORATE,  0x38                /* last transport rate applied  */
    .equ D_MIDIERR,  0x3C                /* lo: DIN framing/noise drops  */
                                         /* hi: DIN overruns             */
    .equ D_FORMAT,   0x40
    .equ D_DWTOK,    0x48                /* np_runtime: CYCCNT is counting */
    .equ D_SBRKFAIL, 0x4C
    .equ D_IRQMAX,   0x50
    .equ D_IRQSTART, 0x54
    .equ D_POOLFAIL, 0x74
    .equ D_NOTE,     0xB4                /* note captured at note-on     */
    .equ D_XENVK,    0x140               /* envelope constants, 9 words  */
    .equ D_ENVSTATE, 0x180               /* RC envelope state: 21 x 32 B */
    .equ D_PERBOOT_END, 0x420            /* cleared on every boot up to here */
    /* --- retained across warm resets (only a cold boot clears them) --- */
    .equ D_RSR,      0x420               /* RCC_RSR at this boot           */
    .equ D_PCIDX,    0x424               /* samples written (mod 256)      */
    .equ D_PCHIST,   0x428               /* 4 words: thread/audio/systick/other */
    .equ D_LASTSTATS, 0x440              /* 0x60..0x130 as the last boot left it */
    .equ D_PCRING,   0x540               /* 256 words: PC[23:0] | IPSR << 24 */
    .equ D_LASTRING, 0x940               /* the ring as the last boot left it */
    .equ D_LASTIDX,  0xD40               /* and its sample count             */
    .equ D_LEDWAIT,  0xD44               /* LED frames the guard had to hold back */
    .equ D_LEDTS,    0xD48               /* DWT_CYCCNT when the last frame started */
    .equ D_LEDSENT,  0xD4C               /* LED frames started, monotonic  */
    .equ D_SLOTHOLD, 0x12E0              /* 21 x {level, block count, slope} */
    .equ NP_SLOTHOLD_BYTES, (21 * 12)
    .equ D_BLOCKMODE, (D_SLOTHOLD + NP_SLOTHOLD_BYTES)
    .equ D_BLOCKFRAC, (D_BLOCKMODE + 4)  /* (N-1)/N for the attack's reach */
    .equ D_WALK,      0xF00              /* audio-rate slot list, per block */
    .equ W_TICK,      0                  /*   sample count, top three bits  */
    .equ W_COUNT,     4                  /*   slots in the list             */
    .equ W_FP,        8                  /*   the walk's stock end pointer  */
    .equ W_LIST,      16                 /*   up to 19 slot pointers        */
    .equ NP_WALK_BYTES, (16 + 19 * 4)
    .equ D_SQRTLUT,   0xF60              /* 3 x {in, gain, y, y*gain}       */
    .equ NP_SQRTLUT_BYTES, (3 * 16)
    .equ D_FLASH_ACTIVE, 0xF90           /* CYCCNT at journal-write entry, or 0 */
    .equ D_FLASH_COUNT,  0xF94           /* journal writes this boot        */
    .equ D_FLASH_MAX,    0xF98           /* longest one, cycles             */
    .equ D_FLASH_FED,    0xF9C           /* watchdog refreshes granted to it */
    .equ D_FLASH_TICK0,  0xFA0           /* uwTick at entry: bounds the grace */
                                         /* this evaluator call = 8 samples */
    /* Runtime: PLL2Q 40 MHz /16 = 2.5 MHz, MIDI=6, mode 3.
     * Three bytes take 14.4 us. C++ guard enforces 20 us start-to-CS. */
    .equ GPIOE_BSRR, 0x58021018
    .equ LED_CS,      8                  /* PE3 drives the HT16D35A CSB    */
    .equ LED_CSW_LOOPS, 64               /* tCSW >= 100 ns of CS high      */
    .equ DMA1_LISR,  0x40020000
    .equ DMA1_S0CR,  0x40020010
    .equ ADC1_ISR,   0x40022000
    .equ ADC1_CR,    0x40022008
    .equ ADC_ISR_OVR, 0x10
    .equ SYSTICK_SCAN_RESUME, 0x0811973E       /* after the displaced eight */
    .equ D_SIZE,     NP_DIAG_SIZE
    .equ RCC_RSR,    0x580244D0
    .equ RCC_RSR_RMVF, (1 << 16)
    .equ NP_XENV_CONSTS,  (DIAG + D_XENVK)    /* np_xenv.S reads these  */
    .equ NP_XENV_NCONSTS, 9
    .equ NP_ENV_STATE,    (DIAG + D_ENVSTATE)
    .equ NP_SLOT_HOLD,    (DIAG + D_SLOTHOLD)
    .equ NP_BLOCK_MODE,   (DIAG + D_BLOCKMODE)
    .equ NP_BLOCK_FRAC,   (DIAG + D_BLOCKFRAC)
    .equ NP_WALK,         (DIAG + D_WALK)
    .equ NP_SQRTLUT,      (DIAG + D_SQRTLUT)
    .global NP_SLOT_HOLD
    .global NP_BLOCK_MODE
    .global NP_BLOCK_FRAC
    /* P_AUDIO's S_COUNT: np_prof_record bumps it once per audio ISR, after
     * the handler body, so it is constant inside a block and differs
     * between blocks -- the envelope's per-block tag. */
    .equ NP_AUDIO_BLOCK,  (DIAG + 0xB8)
    .global NP_AUDIO_BLOCK
    .global NP_XENV_CONSTS                    /* absolute, for movw/movt */
    .global NP_ENV_STATE

    .equ SCB_AIRCR,  0xE000ED0C
    .equ SCB_CCR,    0xE000ED14
    .equ SCB_CFSR,   0xE000ED28
    .equ SCB_HFSR,   0xE000ED2C
    .equ ICIALLU,    0xE000EF50
    .equ AIRCR_RESET, 0x05FA0004
    .equ DWT_CYCCNT, 0xE0001004

    .equ IWDG,       0x58004800
    .equ UWTICK,     0x20000174          /* HAL_GetTick's millisecond counter */
    .equ IWDG_KR,    0x00
    .equ IWDG_PR,    0x04
    .equ IWDG_RLR,   0x08
    .equ IWDG_SR,    0x0C

    .equ HEAP_BASE,  0x30000000          /* D2 SRAM                      */
    .equ DTCM_BASE,  0x20000000
    .equ DTCM_SIZE,  0x00020000

/* stock entry points the hooks tail-call into */
    .equ ORIG_SYSTEMINIT, 0x081093F4
    .equ ORIG_MAIN,       0x08119938
    .equ ORIG_SYSOBJ,     0x08109614
    .equ ORIG_AUDIO_IRQ,  0x08119845
    .equ ORIG_SYSTICK,    0x081196D9
    .equ ORIG_TIM5_IRQ,   0x081194A5          /* IRQ 50 = TIM5: the sequencer tick, not SPI3 */
    .equ ORIG_DMA_IRQ,    0x0810F209
    .equ MALLOC_PRIMASK,  0x2000B860

/* np_xmod_pos is spliced into the middle of the per-block DSP update and
 * rejoins it at the instruction after the two it displaced. */
    .equ XMOD_LOOP_RET,   0x08112C8E      /* stock path rejoins here      */
    .equ XMOD_RATIO_RET,  0x08112C9C      /* ratio path: past the pitch   */
/* At the splice, r3 is freshly formed by the stock loop as
 * engine+0x2EDC = project+0x2F20, VCO1's live pitch coordinate, and s18 is
 * the loop invariant 1/126.  NP_XMOD_FOLLOW=1 uses those, which follows
 * OSC1 through pitch bend, glide, tuning and parameter locks -- correct
 * while OSC1 is the carrier and wrong as soon as it is not.
 *
 * The default reaches the played note instead, through the project
 * singleton's fixed address.  0x0811621C is `ldr r0,[pc,#0] / bx lr` over
 * the literal 0x200047F0, so the getter returns a constant.  Not through
 * r4: the DSP update is called as `bl 0x0811621C / adds r0,#0x44 / bl
 * 0x08112488`, so its argument, and every offset inside it, is relative to
 * an engine sub-object 0x44 further on. */
    .equ PROJECT,         0x200047F0

/* The played note lives in a *pair* of fields, one per oscillator, and
 * which of the two receives an incoming note depends on the path the note
 * handler at 0x081170E8 takes:
 *
 *   nothing held      0x5FCC = 0x5FD0 = the new note   (0x0811739A)
 *   legato            0x5FCC = previous, 0x5FD0 = new  (0x0811728A)
 *   slide             0x5FCC = new, 0x5FD0 = previous  (0x0811757C)
 *   note off in a
 *   held stack        0x5FCC = 0x5FD0 = what is left   (0x0811782E)
 *
 * The evaluator picks per slot -- `cmp r1,#4 / beq` at 0x08115864 sends
 * VCO2Tune to 0x5FD0 and everything else to 0x5FCC -- so during legato the
 * two oscillators genuinely hold different notes, and the slide path swaps
 * which one is which.
 *
 * Reading either field alone therefore gives the wrong note on half of the
 * transitions.  When they agree the answer is unambiguous; when they do not
 * we want the note that just arrived, so np_note_hook records it. */
    .equ XMOD_NOTE_ADDR,  (PROJECT + 0x5FCC)   /* VCO1's note slot      */
    .equ XMOD_NOTE2_OFF,  0x04                 /* -> VCO2's note slot   */
    .equ XMOD_XPOSE_OFF,  0x38                 /* note -> transpose     */
    .equ XMOD_NOTE_TAIL,  0x08116D48           /* np_note_hook tail-call */
    .equ LED_FRAME_RESUME, 0x0810A01C          /* after both CS transitions */
    .equ UI_QUEUE_RESUME,  0x0815756A          /* after the whole transition */
    .equ NP_NOTE,         (DIAG + D_NOTE)      /* RAM: the blob is flash */
    .equ XMOD_KEYED,      2               /* owner+0x25 marker            */
    .equ XMOD_RATIOS,     128             /* detents of the ratio knob    */

/* bits in np_flags */
    .equ FLAG_CRC, 0x01
    .equ FLAG_WDG, 0x02
    .equ FLAG_LFO, 0x04

    .section .text.np, "ax", %progbits
    .align 3
    .global np_base
np_base:

/* ------------------------------------------------------------------ */
/* Header.  tools/patch.py reads the entry addresses from here and     */
/* writes np_flags, np_wdg_pr, np_wdg_rlr, np_crc_*, np_brk_ptr and    */
/* np_heapsize_ptr.                                                    */
/* ------------------------------------------------------------------ */
np_header:
    .ascii  "NPAT"                       /* +0x00 signature            */
    .byte   1                            /* +0x04 version              */
np_flags:
    .byte   0                            /* +0x05 FLAG_CRC | FLAG_WDG  */
np_wdg_pr:
    .byte   6                            /* +0x06 IWDG prescaler code  */
    .byte   0                            /* +0x07 pad                  */
np_crc_expected:
    .word   0                            /* +0x08                      */
np_crc_len:
    .word   0                            /* +0x0C                      */
    .word   np_early                     /* +0x10                      */
    .word   np_late                      /* +0x14                      */
    .word   np_wdg_hook                  /* +0x18                      */
    .word   np_hardfault                 /* +0x1C                      */
    .word   np_memmanage                 /* +0x20                      */
    .word   np_busfault                  /* +0x24                      */
    .word   np_usagefault                /* +0x28                      */
    .word   np_sbrk                      /* +0x2C                      */
    .word   DIAG                         /* +0x30                      */
    .word   D_SIZE                       /* +0x34                      */
np_wdg_rlr:
    .word   2000                         /* +0x38 IWDG reload counter  */
np_brk_ptr:
    .word   0                            /* +0x3C &brk                 */
np_heapsize_ptr:
    .word   0                            /* +0x40 &heap_size           */
    .word   np_mod_tick                  /* +0x44 (np_lfo.cpp)         */
    .word   np_lfo_poll                  /* +0x48 (np_lfo.cpp)         */
    .word   np_xmod_time                 /* +0x4C (np_lfo.cpp)         */
    .word   np_xmod_speed                /* +0x50 (np_lfo.cpp)         */
    .word   np_xmod_pos                  /* +0x54 ratio splice         */
    .word   0                            /* +0x58 ratio values: the source only (p49, room); tests read np_lfo.cpp */
    .word   np_ratio_offset              /* +0x5C (np_lfo.cpp)         */
    .word   XMOD_RATIOS                  /* +0x60                      */
np_xmod_flags:
    .byte   NP_XMOD_FOLLOW               /* +0x64 0 note, 1 VCO1 pitch */
    .byte   0, 0, 0
    .word   np_sine                      /* +0x68 sine splice          */
    .word   np_tri                       /* +0x6C                      */
    .word   np_square                    /* +0x70                      */
    .word   np_saw                       /* +0x74                      */
    .word   np_midi_tx                   /* +0x78 (np_midi.cpp)        */
    .word   np_midi_rx                   /* +0x7C (np_midi.cpp)        */
    .word   np_usb_defer                 /* +0x80 (np_runtime.cpp)     */
    .word   np_audio_irq                 /* +0x84 profiled vector      */
    .word   np_systick_irq               /* +0x88                      */
    .word   np_tim5_irq                  /* +0x8C TIM5 (IRQ 50)        */
    .word   np_dma_irq                   /* +0x90                      */
    .word   np_new                       /* +0x94 fixed ISR allocator  */
    .word   np_delete                    /* +0x98                      */
    .word   np_malloc_lock               /* +0x9C profiled lock        */
    .word   np_malloc_unlock             /* +0xA0                      */
    .word   np_main_service              /* +0xA4 deferred work        */
    .word   np_note_hook                 /* +0xA8 note-on capture      */
    .word   NP_NOTE                      /* +0xAC where it is kept     */
    .word   np_wave_q_hi                 /* +0xB0 QuantizedLinAD, X-Env on  */
    .word   np_wave_q_lo                 /* +0xB4 QuantizedLinAD, X-Env off */
    .word   np_wave_l_hi                 /* +0xB8 LinAD, X-Env on           */
    .word   np_wave_l_lo                 /* +0xBC LinAD, X-Env off          */
    .word   np_wave_e_hi                 /* +0xC0 ExpAD, X-Env on           */
    .word   np_wave_e_lo                 /* +0xC4 ExpAD, X-Env off          */
    .word   np_wave_shape                /* +0xC8 (np_lfo.cpp) the renderer */
    .word   np_ui_switch                 /* +0xCC queued TYPE/WAVE release */
    .word   np_ui_begin_unlink           /* +0xD0 idempotent chord entry   */
    .word   np_ui_reconcile              /* +0xD4 event-ordered all-up     */
    .word   np_ui_consumed               /* +0xD8 UI-only button state     */
    .word   np_ui_consume                /* +0xDC                         */
    .word   np_ui_long_hold              /* +0xE0                         */
    .word   np_ui_pressed                /* +0xE4                         */
    .word   np_ui_allow                  /* +0xE8                         */
    .word   0x3003C040                   /* +0xEC 48-byte shadow address   */
    .word   np_ui_consume_modifier       /* +0xF0 group suppression       */
    .word   np_random_splice             /* +0xF4 scale-aware random FREQ */
    .word   np_xenv_q                    /* +0xF8 X-envelope output curve */
    .word   np_xenv_l                    /* +0xFC                         */
    .word   np_xenv_e                    /* +0x100                        */
    .word   np_xenv_osc1                 /* +0x104 inlined audio copies   */
    .word   np_xenv_osc2                 /* +0x108                        */
    .word   np_xenv_filter               /* +0x10C                        */
    .word   np_xenv_osc1_previous         /* +0x110 previous sample        */
    .word   np_env_rc_step               /* +0x114 RC envelope step       */
    .word   np_env_main                  /* +0x118 main ADSR publish site */
    .word   np_env_entry                 /* +0x11C slot index -> state    */
    .word   np_xenv_consts               /* +0x120 flash master of the    */
    .word   NP_XENV_CONSTS               /* +0x124 ... DTCM constants     */
    .word   NP_ENV_STATE                 /* +0x128 RC state entries       */
    .word   np_led_frame                 /* +0x12C LED frame spacing guard */
    .word   np_led_done                  /* +0x130 records the frame start */
    .word   np_ui_queue_pop              /* +0x134 atomic deque transition */
    .word   np_led_configure             /* +0x138 SPI4 runtime setup      */
    .word   np_diag_render               /* +0x13C render entry counter    */
    .word   np_diag_enqueue_hook         /* +0x140 UI producer observer    */
    .word   np_diag_dispatch             /* +0x144 UI consumer observer    */
    .global np_build_id
np_build_id:
    .word   0                            /* +0x148 filled by patch.py      */
    .word   np_scan_rearm                /* +0x14C pot scan re-arm (O)     */
    .word   np_ui_service                /* +0x150 bounded main UI drain  */
    .word   np_project_first             /* +0x154 project queue progress */
    .word   np_eval_linad                /* +0x158 slot hold, LinAD       */
    .word   np_eval_expad                /* +0x15C slot hold, ExpAD       */
    .word   np_set_rate                  /* +0x160 setRate input guard    */
    .word   np_slot_walk                 /* +0x164 audio-rate walk, cached */
    .word   np_after_walk                /* +0x168 ...and its exit        */
    .word   0                            /* +0x16C (Y4 sqrt memo, withdrawn p29; */
    .word   0                            /* +0x170  its code removed p49 for     */
    .word   0                            /* +0x174  room)                        */
    .word   np_flash_write               /* +0x178 journal writer, fenced */
    .word   np_led_smooth                /* +0x17C LED fade that converges */
    .word   np_list_sort                 /* +0x180 pattern store: sort if needed */
    .word   np_seq_worker                /* +0x184 ...and skip an idle pass */
    .word   np_ev_trig                   /* +0x188 X-Env trigger, logged    */
    .word   np_morph_trig                /* +0x18C morph pattern's trigs gated */
    .word   np_project_gate              /* +0x190 paste waits for the QSPI queue */
    .word   np_midi_fanout               /* +0x194 MIDI out, per-port flow  */
    .word   np_polyat                    /* +0x198 poly pressure = 2nd byte */
    .word   np_usb_rx_put                /* +0x19C USB-MIDI receive ring    */
    .word   np_note_start                /* +0x1A0 note start -> snap       */
    .word   np_dsp_sample                /* +0x1A4 per-sample DSP, snapping */
    .word   np_modnote_replay            /* +0x1A8 mod note applied directly */
    .word   np_cnote_entry               /* +0x1AC note-on: maybe deferred   */
    .word   np_noteoff_entry             /* +0x1B0 note-off: flush first     */
    .global np_choke_samples
np_choke_samples:
    .word   32                           /* +0x1B4 fade samples, patch.py    */
    .word   np_modnote_random            /* +0x1B8 R1 instrument randomizer  */
    .word   np_mn_key_press              /* +0x1BC R2 black + white key press */
    .word   np_mn_rec_press              /* +0x1C0 ...its RECORD flag (press)  */
    .word   np_mn_rec_release            /* +0x1C4 ...and on the release      */
    .word   np_mn_chord_check            /* +0x1C8 K1 TEMPO+FUNC+C#: drum kit */
    .word   np_nm_key_press              /* +0x1CC P1 normal-mode key press   */
    .word   np_nm_key_release            /* +0x1D0 ...and its release         */
    .word   np_clear_match               /* +0x1D4 D1 clear patch: not mod notes */
    .word   np_load_request              /* +0x1D8 PL project load requested   */
    .word   np_load_done                 /* +0x1DC ...its read completed       */
    .word   np_proj_service              /* +0x1E0 ...taken over at pattern end */
    .word   np_save_gate                 /* +0x1E4 ...and no save meanwhile    */

/* ------------------------------------------------------------------ */
/* Replaces "bl SystemInit" in Reset_Handler.  This is the first       */
/* application code to run after .data/.bss are set up and before the  */
/* static constructors, so diagnostics and the fixed pools are ready   */
/* before the first allocation can occur.                              */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
np_early:
    /* Keep the public-call boundary 8-byte aligned before calling C/C++. */
    push    {r3, lr}
    /* --- diagnostic block ------------------------------------------ */
    ldr     r2, =DIAG
    ldr     r0, [r2, #D_MAGIC]
    ldr     r1, =DIAG_MAGIC
    cmp     r0, r1
    beq     2f
    movs    r0, #0                       /* cold boot: clear the block */
    movs    r3, #0
1:  str     r0, [r2, r3]
    adds    r3, #4
    cmp     r3, #D_SIZE
    blo     1b
    str     r1, [r2, #D_MAGIC]
2:  /* Keep the previous boot's counters and DWT records where a later
       telemetry dump can find them, then clear the per-boot area.       */
    movs    r3, #0x60
9:  ldr     r0, [r2, r3]
    add     r1, r2, #D_LASTSTATS
    subs    r1, r1, #0x60
    str     r0, [r1, r3]
    adds    r3, #4
    cmp     r3, #0x130
    blo     9b
    /* ...and the PC ring itself, before this boot's SysTick starts
       overwriting it (4 samples per millisecond).                       */
    add     r0, r2, #D_PCRING
    add     r1, r2, #D_LASTRING
    movs    r3, #0
10: ldr     r12, [r0, r3]
    str     r12, [r1, r3]
    adds    r3, #4
    cmp     r3, #0x400
    blo     10b
    ldr     r0, [r2, #D_PCIDX]
    str     r0, [r2, #D_LASTIDX]
    movs    r0, #0                       /* per-boot state             */
    str     r0, [r2, #D_WDGON]
    str     r0, [r2, #D_LFORATE]
    movs    r3, #D_FORMAT                /* telemetry is per boot      */
3:  str     r0, [r2, r3]
    adds    r3, #4
    cmp     r3, #D_PERBOOT_END
    blo     3b
    ldr     r0, [r2, #D_BOOTS]
    adds    r0, #1
    str     r0, [r2, #D_BOOTS]
    /* Why did we reset?  RCC_RSR is sticky until RMVF clears it; record
       it and clear so every boot reports only its own cause.            */
    ldr     r1, =RCC_RSR
    ldr     r3, [r1]
    str     r3, [r2, #D_RSR]
    orr     r3, r3, #RCC_RSR_RMVF
    str     r3, [r1]
    /* Boot marker in the PC ring so a dump can tell sessions apart.     */
    ldr     r3, [r2, #D_PCIDX]
    and     r1, r3, #0xFF
    add     r1, r2, r1, lsl #2
    movw    r12, #0x0000
    movt    r12, #0xB007
    orr     r0, r0, r12                  /* 0xB007xxxx | boots         */
    str     r0, [r1, #D_PCRING]
    adds    r3, #1
    str     r3, [r2, #D_PCIDX]
    /* Per-slot hold table: its cached level is only meaningful for the
       session that produced it, and the block counter must start at zero
       so the first call of each slot evaluates rather than serving a
       stale float.  Not covered by the per-boot clear above, which stops
       at D_PERBOOT_END.                                               */
    add     r0, r2, #D_SLOTHOLD
    movs    r1, #0
    movs    r3, #0
11: str     r1, [r0, r3]
    adds    r3, #4
    cmp     r3, #(NP_SLOTHOLD_BYTES + 8)  /* the two block words too  */
    blo     11b
    add     r0, r2, #D_WALK
    movs    r3, #0
12: str     r1, [r0, r3]
    adds    r3, #4
    cmp     r3, #NP_WALK_BYTES
    blo     12b
    /* The per-boot counters from D_LEDWAIT on (journal fence, Q1, MIDI
       flow, snap, choke, randomizer) are cleared by np_runtime_init, which
       runs below, before anything reads them.                           */

    /* Envelope constants: flash master -> DTCM (DTCM is zero-wait; see
       the note in np_xenv.S about what this used to claim).           */
    ldr     r1, =np_xenv_consts
    add     r0, r2, #D_XENVK
    movs    r3, #0
4:  ldr     r12, [r1, r3]
    str     r12, [r0, r3]
    adds    r3, #4
    cmp     r3, #(NP_XENV_NCONSTS * 4)
    blo     4b

    /* --- C: instruction cache -------------------------------------- */
    dsb
    isb
    ldr     r0, =ICIALLU
    movs    r1, #0
    str     r1, [r0]
    dsb
    isb
    ldr     r0, =SCB_CCR
    ldr     r1, [r0]
    orr     r1, r1, #(1 << 17)           /* CCR.IC                     */
    str     r1, [r0]
    dsb
    isb

    ldr     r12, =(ORIG_SYSTEMINIT | 1)
    blx     r12
    bl      np_runtime_init
    pop     {r3, pc}
    .ltorg

/* ------------------------------------------------------------------ */
/* E1 -- replaces "bl main"; optionally verifies the image first.      */
/* Runs with the clocks configured, so the CRC is quick.               */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
np_late:
    ldr     r0, =np_flags
    ldrb    r0, [r0]
    lsls    r0, r0, #31                  /* FLAG_CRC into the sign bit */
    beq     1f
    push    {r4, lr}
    ldr     r4, =DIAG
    ldr     r0, =0x08100000
    ldr     r1, =np_crc_len
    ldr     r1, [r1]
    bl      np_crc32
    str     r0, [r4, #D_CRC_GOT]
    ldr     r0, =np_crc_expected
    ldr     r0, [r0]
    str     r0, [r4, #D_CRC_EXP]
    pop     {r4, lr}
1:  ldr     r12, =(ORIG_MAIN | 1)
    bx      r12
    .ltorg

/* ------------------------------------------------------------------ */
/* A2 -- called once per iteration of the main loop, in place of the   */
/* System singleton accessor.  Arms the watchdog on the first call, so */
/* the whole of main's start-up runs unwatched, and kicks it after     */
/* that.  Every register the caller can see is preserved.              */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
np_wdg_hook:
    push    {r0, r1, r2, r3, r12, lr}
    ldr     r0, =np_flags
    ldrb    r0, [r0]
    lsls    r1, r0, #30                  /* FLAG_WDG into the sign bit */
    bpl     5f
    ldr     r0, =DIAG
    ldr     r1, [r0, #D_WDGON]
    ldr     r2, =IWDG
    cbnz    r1, 8f

    movs    r1, #1                       /* first call this boot       */
    str     r1, [r0, #D_WDGON]
    movw    r1, #0xCCCC                  /* start                      */
    str     r1, [r2, #IWDG_KR]
    movw    r1, #0x5555                  /* unlock PR/RLR              */
    str     r1, [r2, #IWDG_KR]
    ldr     r3, =np_wdg_pr
    ldrb    r3, [r3]
    str     r3, [r2, #IWDG_PR]
    ldr     r3, =np_wdg_rlr
    ldr     r3, [r3]
    str     r3, [r2, #IWDG_RLR]
    movw    r0, #0x4000                  /* bounded wait for SR == 0   */
7:  ldr     r1, [r2, #IWDG_SR]
    cbz     r1, 8f
    subs    r0, #1
    bne     7b
8:  movw    r1, #0xAAAA                  /* reload                     */
    str     r1, [r2, #IWDG_KR]

5:  bl      np_main_service              /* USB and telemetry          */
    pop     {r0, r1, r2, r3, r12, lr}
    ldr     r12, =(ORIG_SYSOBJ | 1)
    bx      r12
    .ltorg

/* ------------------------------------------------------------------ */
/* A4/B1 -- complete replacement for _sbrk.  Same arithmetic and the   */
/* same signed comparison as the stock version, but a full heap now    */
/* returns -1 so malloc can fail, and the high-water mark is kept.     */
/*   r0 = increment  ->  r0 = previous break, or -1                    */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
np_sbrk:
    ldr     r1, =np_brk_ptr
    ldr     r1, [r1]                     /* &brk                       */
    ldr     r2, =np_heapsize_ptr
    ldr     r2, [r2]
    ldr     r2, [r2]                     /* heap size                  */
    add     r2, r2, #HEAP_BASE           /* heap top                   */
    ldr     r3, [r1]                     /* current break              */
    subs    r2, r2, r3                   /* bytes still free           */
    cmp     r2, r0
    blt     2f                           /* signed: increment may be <0 */
    add     r12, r3, r0
    str     r12, [r1]
    ldr     r1, =DIAG
    ldr     r2, [r1, #D_HEAPMAX]
    cmp     r12, r2
    bls     1f
    str     r12, [r1, #D_HEAPMAX]
1:  mov     r0, r3
    bx      lr
2:  ldr     r1, =DIAG
    ldr     r2, [r1, #D_SBRKFAIL]
    adds    r2, #1
    str     r2, [r1, #D_SBRKFAIL]
    movs    r0, #0
    mvns    r0, r0                       /* -1                         */
    bx      lr
    .ltorg

/* ------------------------------------------------------------------ */
/* DWT wrappers.  The measured interval is the original handler body;  */
/* vector-entry and recording overhead is deliberately outside it.     */
/* Higher-priority preemption remains inside the number because that is */
/* what determines whether the real 31.25 us audio deadline was met.    */
/* ------------------------------------------------------------------ */
    .macro PROFILE_IRQ name, original, kind
    .thumb_func
    .align 2
    .global \name
\name:
    push    {r4, lr}
    ldr     r4, =DWT_CYCCNT
    ldr     r4, [r4]
    ldr     r12, =\original
    blx     r12
    ldr     r0, =DWT_CYCCNT
    ldr     r0, [r0]
    subs    r0, r0, r4
    movs    r1, #\kind
    bl      np_prof_record
    pop     {r4, pc}
    .endm

    PROFILE_IRQ np_audio_irq,   ORIG_AUDIO_IRQ, 0
    PROFILE_IRQ np_tim5_irq,    ORIG_TIM5_IRQ,  2
    PROFILE_IRQ np_dma_irq,     ORIG_DMA_IRQ,   3
    .ltorg

/* SysTick is the sampling profiler as well.  At vector entry SP points */
/* at the stacked frame of whatever was interrupted (thread or a lower  */
/* priority handler): PC at +0x18, xPSR at +0x1C, the same offsets for  */
/* basic and extended (FPU) frames.  One word per 250 us goes into the  */
/* retained ring, PC[23:0] | IPSR << 24, and a per-context histogram    */
/* counts thread / audio (IRQ 8, IPSR 24) / everything else.  The ring  */
/* survives a watchdog or fault reset; np_main_service dumps it.        */
    .thumb_func
    .align 2
    .global np_systick_irq
np_systick_irq:
    push    {r4, lr}
    ldr     r4, =DIAG
    ldr     r0, [sp, #(8 + 0x18)]        /* interrupted PC             */
    ldr     r1, [sp, #(8 + 0x1C)]        /* interrupted xPSR           */
    ldr     r2, [r4, #D_PCIDX]
    and     r3, r2, #0xFF
    add     r3, r4, r3, lsl #2
    ubfx    r1, r1, #0, #9               /* IPSR of the interrupted code */
    bfi     r0, r1, #24, #8              /* PC[23:0] | IPSR << 24       */
    str     r0, [r3, #D_PCRING]
    adds    r2, #1
    str     r2, [r4, #D_PCIDX]
    cmp     r1, #0
    beq     1f
    cmp     r1, #24                      /* EXTI2, the CV engine        */
    beq     2f
    movw    r3, #(D_PCHIST + 12)         /* other handler               */
    b       3f
1:  movw    r3, #D_PCHIST                /* thread mode                 */
    b       3f
2:  movw    r3, #(D_PCHIST + 4)          /* audio                       */
3:  ldr     r0, [r4, r3]
    adds    r0, #1
    str     r0, [r4, r3]
    /* A project journal write erases four 128 KB sectors of internal
     * flash and programs 512 KB, with main spinning in the HAL's wait
     * loop the whole time: seconds typical, and past the 16 s watchdog
     * at the sector-erase maximum.  The stock has no watchdog and merely
     * freezes; A2 made that a reboot.  While np_flash_write has flagged
     * the operation, feed the dog from here -- SysTick keeps running,
     * the code is in bank 2 -- but only for 60 s: the writer's own
     * failure path is `b .`, and that must still end in a reset.     */
    ldr     r0, [r4, #D_FLASH_ACTIVE]
    cbz     r0, 4f
    ldr     r0, =UWTICK
    ldr     r0, [r0]
    ldr     r1, [r4, #D_FLASH_TICK0]
    subs    r0, r0, r1
    movw    r1, #60000                   /* not a modified immediate       */
    cmp     r0, r1
    bhs     4f
    ldr     r0, [r4, #D_FLASH_FED]
    adds    r0, #1
    str     r0, [r4, #D_FLASH_FED]
    ldr     r0, =IWDG
    movw    r1, #0xAAAA
    str     r1, [r0, #IWDG_KR]
4:  ldr     r4, =DWT_CYCCNT
    ldr     r4, [r4]
    ldr     r12, =ORIG_SYSTICK
    blx     r12
    ldr     r0, =DWT_CYCCNT
    ldr     r0, [r0]
    subs    r0, r0, r4
    movs    r1, #1
    bl      np_prof_record
    pop     {r4, pc}
    .ltorg

/* The stock allocator masks every interrupt with PRIMASK.  Keep its ABI, */
/* but measure the exact masked interval.  This is allocator IRQ-off max, */
/* not a claim that every cpsid-i site in the closed firmware is covered. */
    .thumb_func
    .align 2
    .global np_malloc_lock
np_malloc_lock:
    push    {r0, r1, r2, r3}
    mrs     r2, PRIMASK
    cpsid   i
    ldr     r3, =MALLOC_PRIMASK
    str     r2, [r3]
    ldr     r3, =DWT_CYCCNT
    ldr     r2, [r3]
    ldr     r3, =DIAG
    str     r2, [r3, #D_IRQSTART]
    pop     {r0, r1, r2, r3}
    bx      lr

    .thumb_func
    .align 2
    .global np_malloc_unlock
np_malloc_unlock:
    push    {r0, r1, r2, r3}
    ldr     r0, =DWT_CYCCNT
    ldr     r0, [r0]
    ldr     r3, =DIAG
    ldr     r1, [r3, #D_IRQSTART]
    subs    r2, r0, r1
    ldr     r1, [r3, #D_IRQMAX]
    cmp     r2, r1
    it      hi
    strhi   r2, [r3, #D_IRQMAX]
    ldr     r0, =MALLOC_PRIMASK
    ldr     r12, [r0]
    pop     {r0, r1, r2, r3}
    msr     PRIMASK, r12
    bx      lr
    .ltorg

/* ------------------------------------------------------------------ */
/* A1 -- fault handlers.  Record the fault, then reset the MCU.        */
/* Only r0-r3 are touched; they are already in the exception frame.    */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
np_hardfault:
    movs    r0, #1
    b       np_fault_common
    .thumb_func
np_memmanage:
    movs    r0, #2
    b       np_fault_common
    .thumb_func
np_busfault:
    movs    r0, #3
    b       np_fault_common
    .thumb_func
np_usagefault:
    movs    r0, #4
    b       np_fault_common

    .thumb_func
    .align 2
np_fault_common:
    ldr     r1, =DIAG
    ldr     r2, [r1, #D_FORMAT]           /* reject random cold-boot RAM */
    cmp     r2, #2
    bne     9f
    ldr     r2, [r1, #D_POOLFAIL]         /* pool panic ends in UDF; keep */
    cbz     r2, 9f                        /* its cause across warm reset  */
    movs    r0, #5
9:
    str     r0, [r1, #D_FAULT]
    ldr     r0, =DIAG_MAGIC
    str     r0, [r1, #D_MAGIC]

    ldr     r0, [r1, #D_FAULTS]          /* the block was cleared in   */
    adds    r0, #1                       /* np_early on the cold boot  */
    str     r0, [r1, #D_FAULTS]

    ldr     r0, =SCB_CFSR
    ldr     r2, [r0]
    str     r2, [r1, #D_CFSR]
    ldr     r0, =SCB_HFSR
    ldr     r2, [r0]
    str     r2, [r1, #D_HFSR]

    mov     r2, lr                       /* EXC_RETURN                 */
    tst     r2, #4
    beq     1f
    mrs     r3, psp
    b       2f
1:  mrs     r3, msp
2:  str     r3, [r1, #D_SP]

    /* The core frame r0..xPSR is at the exception SP for BOTH frame types:
     * an extended (FP) frame appends S0-S15, FPSCR and a reserved word
     * ABOVE xPSR, at SP+0x20..+0x67 (ARMv7-M ARM B1.5.7), so EXC_RETURN
     * bit 4 changes nothing here.  This used to add 0x48 for an FP frame,
     * which decoded S15/FPSCR/the reserved word -- with lazy stacking, stale
     * stack -- as LR/PC/xPSR for every fault taken in a context that had
     * touched the FPU: the audio handler and most of main.  The SysTick
     * sampler always read PC/xPSR at +0x18/+0x1C and was right.          */

    /* Only dereference the frame if it is inside DTCM.  A bad stack   */
    /* pointer is exactly the case where faulting again here would put */
    /* us back where we started.                                       */
    ldr     r0, =DTCM_BASE
    subs    r0, r3, r0
    ldr     r2, =(DTCM_SIZE - 32)
    cmp     r0, r2
    bhs     3f
    ldr     r0, [r3, #20]                /* stacked LR                 */
    str     r0, [r1, #D_LR]
    ldr     r0, [r3, #24]                /* stacked PC                 */
    str     r0, [r1, #D_PC]
    ldr     r0, [r3, #28]                /* stacked xPSR               */
    str     r0, [r1, #D_PSR]
3:
    dsb
    ldr     r0, =SCB_AIRCR
    ldr     r1, =AIRCR_RESET
    str     r1, [r0]
    dsb
4:  b       4b
    .ltorg

/* ------------------------------------------------------------------ */
/* np_xmod_pos -- FM ratio for the audio-rate X-Mod (type 3).          */
/*                                                                     */
/* The per-block DSP update walks the nineteen modulation slots at     */
/* 0x08112C70 and, for every slot whose owner has the audio-rate flag  */
/* at +0x25 set, recomputes the modulator's table position and calls   */
/* setRate.  That loop, not the parameter handlers, is what decides an */
/* audio-rate modulator's pitch: it runs every block and overwrites    */
/* whatever the handler left behind.  So the ratio has to be applied   */
/* here, or it lasts one block.                                        */
/*                                                                     */
/* Spliced over                                                        */
/*     08112C88  ldrb     r2, [r0, #4]                                 */
/*     08112C8A  vsub.f32 s15, s15, s14                                */
/*                                                                     */
/* Flag 1 (type 2, stock audio rate) reproduces both instructions and  */
/* changes nothing.  Flag 2 (type 3) throws the `rate - 0.5` sweep     */
/* away and substitutes the ratio the knob is pointing at, plus the    */
/* live VCO1 table coordinate.                                         */
/*                                                                     */
/* Register discipline: r0 owner and r3 (the live pitch field) are live */
/* and untouched.  The ratio path intentionally restores the stock      */
/* `vldr s13,[r3]` itself, then reuses the stock loop-invariant s18.    */
/* s13 is caller-saved and the stock instruction at 0x08112C8E would    */
/* overwrite it anyway.                                                 */
/*                                                                     */
/* The ratio path rejoins the loop at 0x08112C9C rather than 0x08112C8E, */
/* which skips both of the loop's own pitch branches.  Deliberate: the */
/* branch it skips would add the normal `rate - 0.5` sweep.  This patch */
/* replaces that sweep with an exact ratio offset, then adds precisely  */
/* the same VCO1 coordinate the stock branch would have used.          */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
    .global np_xmod_pos
np_xmod_pos:
    ldrb    r1, [r0, #0x25]
    cmp     r1, #XMOD_KEYED
    beq     2f

1:  ldrb    r2, [r0, #4]                 /* displaced: slot id         */
    vsub.f32 s15, s15, s14               /* displaced: rate - 0.5      */
    ldr     r1, =(XMOD_LOOP_RET | 1)
    bx      r1

    /* --- type 3: ratio + live VCO1 pitch --------------------------- */
2:  vldr    s14, .Lxmod_count            /* 64.0                       */
    vmul.f32 s14, s15, s14
    vcvt.u32.f32 s14, s14
    vmov    r1, s14
    cmp     r1, #(XMOD_RATIOS - 1)
    it      hi
    movhi   r1, #(XMOD_RATIOS - 1)       /* rate == 1.0 would index 64 */
    ldr     r2, =np_ratio_offset
    add     r2, r2, r1, lsl #2
    vldr    s15, [r2]                    /* 12 * log2(ratio) / 127     */

#if NP_XMOD_FOLLOW
    vldr    s13, [r3]                    /* stock: live VCO1 pitch     */
    vfma.f32 s15, s13, s18               /* += VCO1 position / 126     */
#else
    ldr     r1, =XMOD_NOTE_ADDR
    vldr    s0, [r1]                     /* VCO1's note slot           */
    vldr    s14, [r1, #XMOD_NOTE2_OFF]   /* VCO2's note slot           */
    vcmp.f32 s0, s14
    vmrs    APSR_nzcv, fpscr
    beq     3f                           /* agree -> that is the note  */
    ldr     r1, =NP_NOTE                 /* legato or slide: the note  */
    vldr    s0, [r1]                     /* that actually just played  */
    ldr     r1, =XMOD_NOTE_ADDR
3:  vldr    s14, [r1, #XMOD_XPOSE_OFF]   /* transpose                  */
    vadd.f32 s0, s0, s14
    vldr    s14, .Lxmod_semitone
    vfma.f32 s15, s0, s14                /* += semitones / 127         */
#endif

    ldr     r1, =(XMOD_RATIO_RET | 1)    /* past both pitch branches   */
    bx      r1
    .ltorg
    .align 2
.Lxmod_semitone:
    .word   0x3C010204                   /* 1.0f / 127.0f              */
.Lxmod_count:
    .word   0x43000000                   /* 128.0f                     */


/* ------------------------------------------------------------------ */
/* np_wave_* -- the six waveform dispatches in the slot evaluators.    */
/*                                                                     */
/* Each replaces `cmp r3,#4 / bhi.w <bail> / tbb [pc,r3]`, which is    */
/* ten bytes plus a five-byte table, with a jump here.  On entry r3 is */
/* the waveform and r4 the slot, exactly as the dispatch had them.     */
/*                                                                     */
/* The stock blocks clobber s10-s15 and, on the S&H path, r0-r3 and lr */
/* (they spill s0 around the RNG call), so all of that is free here.   */
/* What is *not* free differs per arm, and so does the register the    */
/* convergence point reads the shape out of:                           */
/*                                                                     */
/*   arm            result   converge      also live                   */
/*   Quant  X-Env on   s14   0x0811C714    s0                          */
/*   Quant  X-Env off  s15   0x0811C8C6    s0                          */
/*   LinAD  X-Env on   s13   0x0811132C    s0, s14                     */
/*   LinAD  X-Env off  s15   0x0811153C    s0                          */
/*   ExpAD  X-Env on   s13   0x08110B4C    s0, s14                     */
/*   ExpAD  X-Env off  s15   0x08110D68    s0                          */
/*                                                                     */
/* r12 is pushed with the four argument registers purely to keep the   */
/* stack eight-byte aligned across the call.                           */
/* ------------------------------------------------------------------ */

.macro NP_WAVE name, out, ret, keep14
    .thumb_func
    .align 2
    .global \name
\name:
    push    {r0, r1, r2, r3, r12, lr}
  .if \keep14
    vpush   {s14, s15}
  .endif
    vpush   {s0, s1}
    mov     r0, r4
    mov     r1, r3
    bl      np_wave_shape
    vmov.f32 \out, s0
    vpop    {s0, s1}
  .if \keep14
    vpop    {s14, s15}
  .endif
    pop     {r0, r1, r2, r3, r12, lr}
    b.w     \ret
.endm

    NP_WAVE np_wave_q_hi, s14, 0x0811C714, 0
    NP_WAVE np_wave_q_lo, s15, 0x0811C8C6, 0
    NP_WAVE np_wave_l_hi, s13, 0x0811132C, 1
    NP_WAVE np_wave_l_lo, s15, 0x0811153C, 0
    NP_WAVE np_wave_e_hi, s13, 0x08110B4C, 1
    NP_WAVE np_wave_e_lo, s15, 0x08110D68, 0

/* ------------------------------------------------------------------ */
/* np_led_frame / np_led_done: the stock writer leaves CS low after   */
/* CSTART; the next dirty update closes the previous transaction.      */
/* P6 checks EOT/CSTART and a conservative 20 us start-to-CS interval. */
/* Busy/too-early calls return with dirty intact, not a polling loop.  */
/* The runtime SPI configuration supplies the missing BYTE gaps.       */
/* Hardware acceptance by the write-only LED chip remains unobservable.*/
/* This fixes a measured protocol violation; it does not prove that    */
/* every reported physical freeze had the same cause.                  */
/* Rejoin at 0810A01C: r4=driver, saved r4/lr and 8-byte locals on SP.  */
    .thumb_func
    .align 2
    .global np_led_frame
np_led_frame:
    push    {r4, lr}                     /* displaced from 0810A002    */
    mov     r4, r0
    bl      np_led_ready
    cbnz    r0, 3f
    pop     {r4, pc}                     /* dirty remains set: retry later */
3:  mov     r0, r4
    ldr     r3, =GPIOE_BSRR
    movs    r2, #LED_CS
    str     r2, [r3]                     /* CS high: close the last frame */
    movs    r2, #LED_CSW_LOOPS
4:  subs    r2, #1
    bne     4b                           /* tCSW >= 100 ns             */
    movs    r2, #LED_CS
    lsls    r2, r2, #16
    str     r2, [r3]                     /* CS low: open this frame    */
    mov     r4, r0                       /* displaced from 0810A006    */
    sub     sp, #8                       /* displaced from 0810A008    */
    ldr     r1, =(LED_FRAME_RESUME | 1)
    bx      r1
    .ltorg

/* ------------------------------------------------------------------ */
/* np_ui_queue_pop -- the panel's event deque (a std::deque of event    */
/* pointers, iterators at 0x2000DE54 +0x1C..+0x40) is filled from       */
/* DMA1_Stream0 -- the key and pot scanners push through 0x08157324 --  */
/* and drained by the main loop at 0x081574F8.  Nothing serialises the  */
/* two.  When the consumer retires a block it advances the start        */
/* iterator's map node in three separate steps:                         */
/*                                                                      */
/*     08157558  ldr  r3, [r4, #0x30]   ; old start.node                */
/*     0815755A  adds r2, r3, #4                                        */
/*     0815755C  str  r2, [r4, #0x30]   ; publish the next node         */
/*     0815755E  ldr  r3, [r3, #4]      ; block through the OLD node    */
/*     08157560  add.w r2, r3, #512                                     */
/*     08157564  str  r3, [r4, #0x24]   ; start.cur                     */
/*     08157566  strd r3, r2, [r4, #0x28] ; start.block_start/_end      */
/*                                                                      */
/* A producer push that recenters the map (0x08157422..) or replaces it */
/* (0x081573BE..) rewrites BOTH iterators' node pointers, at           */
/* 0x081573FE and 0x0815740A.  A push landing between the load and the */
/* store leaves the consumer holding a stale node, which it then writes */
/* back as old+4, putting its cursor outside the live map.  From there  */
/* the corruption goes one of two ways: the next dispatch reads a null  */
/* event and ends at 0x0815A6E0 (`b .`, abort), which stops main; or    */
/* start overshoots finish and the queue looks empty for good, which    */
/* does not.  The second is what the unit shows -- main iterating at    */
/* 6000/s, SysTick at its full 4 kHz, the LED writer sending 1282       */
/* frames/s, and yet the UI consuming nothing, every pushed event       */
/* leaking (the small pool pinned at its 192-block thread limit with    */
/* 356 failures) and the panel frozen while keys and pots still reach   */
/* the DSP.  docs/ui-amount-audit.md reproduces the first outcome.      */
/*                                                                      */
/* Replaying the whole transition with interrupts masked makes it       */
/* atomic against the only other writer.  Guarding this side alone is   */
/* enough: the producer runs in an ISR and can never be preempted by    */
/* the consumer.  r0..r3 are dead across it (the loop at 0x0815751E     */
/* reloads r1/r2/r3 from memory); r4 is the panel, r5 a null constant,  */
/* and nothing branches into the displaced run.                         */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
    .global np_ui_queue_pop
np_ui_queue_pop:
    mrs     r0, primask
    cpsid   i
    ldr     r3, [r4, #0x30]              /* displaced from 08157558    */
    adds    r1, r3, #4
    str     r1, [r4, #0x30]
    ldr     r3, [r3, #4]
    add.w   r1, r3, #512
    str     r3, [r4, #0x24]
    strd    r3, r1, [r4, #0x28]
    msr     primask, r0
    ldr     r1, =(UI_QUEUE_RESUME | 1)
    bx      r1
    .ltorg

/* ------------------------------------------------------------------ */
/* np_project_first: at 0x0815766C, after the empty-list check and the   */
/* initial timestamp. The project manager must process its first item  */
/* even if IRQs used its 250 us budget before the first comparison.     */
/* Keep zero-budget and other managers' semantics. Set BOTH arguments  */
/* normally prepared by the check block; merely NOPing the branch is   */
/* invalid (see docs/ui-queue-handoff.md). Subsequent checks are stock. */
    .thumb_func
    .align 2
    .global np_project_first
np_project_first:
    mov.w   r8, #0                      /* displaced instruction */
    ldr     r2, =0x2000C68C
    cmp     r4, r2
    bne     1f
    ldr     r2, [r4, #0x54]
    cbz     r2, 1f
    ldr     r2, =(DIAG + D_PROJECT_FIRST)
    ldr     r3, [r2]
    adds    r3, #1
    str     r3, [r2]
    add     r1, sp, #4
    mov     r0, r4
    ldr     r3, =0x08157673
    bx      r3
1:  ldr     r3, =0x081576B9
    bx      r3
    .ltorg

/* np_scan_rearm -- the pot scan.  Every SysTick the stock handler does  */
/*                                                                      */
/*     0811972A  DMA1 S0CR |= EN        ; NDTR reloads to 3 on an H7    */
/*     08119736  ADC1 CR   |= ADSTART   ; three conversions -> buffer   */
/*                                                                      */
/* and the stream's TC handler 0x0810F208 folds the buffer into each    */
/* pot's 32-sample ring and advances the mux.  Nothing checks that the  */
/* previous row was consumed, and nothing ever clears ADC1's sticky     */
/* overrun flag: the ADC1 NVIC line is off and its vector is            */
/* Default_Handler.  With the CV engine above SysTick (K) a tick can run */
/* with the previous row's TC still pending, and an overrun latched.    */
/* The unit's p8 telemetry shows exactly that state: ISR = ADRDY|EOSMP| */
/* EOC|EOS|OVR, the ISR entered every tick, the rings advancing, every  */
/* channel static while the knob moves, no pot event for 24 s.          */
/*                                                                      */
/* Re-arm only when the stream is idle and its last completion has been */
/* serviced, and clear OVR before ADSTART so a latched overrun can never */
/* outlive one tick.  On a healthy tick both tests pass and the effect  */
/* is byte for byte the stock sequence plus one harmless flag write.    */
/* Called with bl from the SysTick tail; r0-r3 are free there.          */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
    .global np_scan_rearm
np_scan_rearm:
    ldr     r3, =DMA1_LISR
    ldr     r2, [r3]
    lsls    r2, r2, #26                  /* TCIF0 -> sign: row not consumed */
    bmi     1f
    ldr     r2, [r3, #0x10]              /* S0CR                       */
    lsls    r1, r2, #31                  /* EN -> sign: still transferring */
    bmi     1f
    ldr     r1, =ADC1_ISR
    ldr     r0, [r1]
    lsls    r0, r0, #27                  /* OVR (bit 4) -> sign        */
    bpl     2f
    movs    r0, #ADC_ISR_OVR
    str     r0, [r1]                     /* write 1 to clear           */
    ldr     r0, =DIAG
    ldr     r1, [r0, #D_SCANOVR]
    adds    r1, #1
    str     r1, [r0, #D_SCANOVR]
2:  orrs    r2, r2, #1
    str     r2, [r3, #0x10]              /* EN: NDTR reloads to 3      */
    ldr     r1, =ADC1_CR
    ldr     r2, [r1]
    orrs    r2, r2, #4
    str     r2, [r1]                     /* ADSTART                    */
    bx      lr
1:  ldr     r0, =DIAG
    ldr     r1, [r0, #D_SCANSKIP]
    adds    r1, #1
    str     r1, [r0, #D_SCANSKIP]
    bx      lr
    .ltorg

/* np_led_done -- spliced over the writer's epilogue                    */
/*     0810A11E  add sp, #8                                             */
/*     0810A120  pop {r4, pc}                                           */
/* which is reached only after CR1.CSTART was set three instructions    */
/* earlier, so CYCCNT here is the frame's start time.  r2/r3 are dead.  */
    .thumb_func
    .align 2
    .global np_led_done
np_led_done:
    mov     r0, r4
    bl      np_led_started
    add     sp, #8                       /* displaced                  */
    pop     {r4, pc}                     /* displaced                  */
    .ltorg

/* Last init command has finished and CS is high. Change CFG2 only with
 * SPE disabled; then replay the stock TSIZE=3 / SPE=1 setup. */
    .thumb_func
    .align 2
np_led_configure:
    ldr     r2, =0x40013400
    ldr     r3, [r2]
    bic     r3, r3, #1
    str     r3, [r2]
    ldr     r3, [r2, #12]
    bic     r3, r3, #0x03000000
    bic     r3, r3, #0xF0
    orr     r3, r3, #0x03000000          /* CPOL=CPHA=1 */
    orr     r3, r3, #0x60                /* MIDI=6 SCK cycles */
    str     r3, [r2, #12]
    ldr     r3, [r2, #4]                /* displaced CR2 load */
    b.w     0x08109DF8
    .ltorg

    .thumb_func
    .align 2
np_diag_render:
    push    {r3, r4, r5, r6, r7, lr}    /* displaced, preserves r3 */
    mov     r7, r0
    ldr     r3, =(DIAG + D_UI_RENDER)
    ldr     ip, [r3]
    add     ip, ip, #1
    str     ip, [r3]
    b.w     0x0814F810
    .ltorg

    .thumb_func
    .align 2
np_diag_enqueue_hook:
    push    {r0, r1, r2, r3, ip, lr}
    bl      np_diag_enqueue
    pop     {r0, r1, r2, r3, ip, lr}
    push    {r3, r4, r5, r6, r7, r8, r9, lr} /* displaced */
    b.w     0x08157328

/* ------------------------------------------------------------------ */
/* np_note_hook -- replaces `bl 0x08116D48` at 0x081172B0, the call    */
/* the note handler makes on the legato and slide paths once it has    */
/* committed the incoming note to one of the two note slots.  r4 is    */
/* the CNoteData event and the note is the float at r4+8.              */
/*                                                                     */
/* The first-note and note-off paths do not come through here and do   */
/* not need to: both write the same value to *both* slots, so the      */
/* equality test in np_xmod_pos resolves them without this.            */
/*                                                                     */
/* r0-r3 are preserved because the call this replaces is made with     */
/* r0 = project and the rest still holding the handler's live values.  */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
    .global np_note_hook
np_note_hook:
    push    {r2, r3}
    ldr     r2, =NP_NOTE
    ldr     r3, [r4, #8]                 /* CNoteData::note, a float   */
    str     r3, [r2]
    pop     {r2, r3}
    b.w     XMOD_NOTE_TAIL
    .ltorg

/* ------------------------------------------------------------------ */
/* np_sine -- the modulator's sine, spliced into MultiWaveOscFast.     */
/*                                                                     */
/* Stock waveform 0 is the parabola  y = x * (2 - |x|)  over x in      */
/* [-2, 2).  That is what you write for a part with no FPU: five       */
/* instructions, and 3.8 % THD with the third harmonic at -28.6 dB.    */
/* As an FM modulator every one of those harmonics opens its own       */
/* family of sidebands, which is what makes the tone muddy however     */
/* exact the ratio is.                                                 */
/*                                                                     */
/* This part has an FPv5-D16 single-precision FPU with single-cycle    */
/* FMA throughput, so the honest approximation is affordable: fold the */
/* cycle to a quarter wave and evaluate an odd degree-7 polynomial.    */
/*                                                                     */
/*   THD 0.00005 %, third harmonic -167 dB, measured in float32 --     */
/*   at the noise floor of the format.                                 */
/*                                                                     */
/* About ten instructions more than the parabola.  Nineteen oscillators*/
/* at the 32 kHz sample rate is roughly 2 % of one core.               */
/*                                                                     */
/* Spliced at 0x08156656, over `vmov.f32 s13, #0.25`.  Entry: s15 is   */
/* the phase, already wrapped to [-0.25, 0.75).  Exit: s0 is the       */
/* sample and the function returns through the caller's own epilogue,  */
/* which is why this ends in `pop {r4, pc}`.                           */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
    .global np_sine
np_sine:
    vmov.f32 s13, #0.25
    vmov.f32 s14, #4.0
    vsub.f32 s15, s15, s13
    vmul.f32 s15, s15, s14           /* x in [-2, 2), a full cycle    */

    /* fold to |x| <= 1: sin(pi*x/2) is symmetric about x = +-1, so one
     * odd polynomial then covers the whole period */
    vmov.f32 s14, #1.0
    vcmpe.f32 s15, s14
    vmrs    apsr_nzcv, fpscr
    ble     1f
    vmov.f32 s14, #2.0
    vsub.f32 s15, s14, s15           /* x > 1  ->  2 - x              */
    b       2f
1:  vmov.f32 s14, #-1.0
    vcmpe.f32 s15, s14
    vmrs    apsr_nzcv, fpscr
    bpl     2f
    vmov.f32 s14, #-2.0
    vsub.f32 s15, s14, s15           /* x < -1  ->  -2 - x            */

    /* y = x * (c1 + x2*(c3 + x2*(c5 + x2*c7))) */
2:  vmul.f32 s14, s15, s15
    vldr    s0,  .Lsin_c7
    vldr    s13, .Lsin_c5
    vfma.f32 s13, s0, s14
    vldr    s0,  .Lsin_c3
    vfma.f32 s0, s13, s14
    vldr    s13, .Lsin_c1
    vfma.f32 s13, s0, s14
    vmul.f32 s0, s13, s15
    pop     {r4, pc}

    .align 2
/* least-squares fit of sin(pi*x/2) on the folded quarter wave.  Beats
 * the degree-7 Taylor series by 76 dB on the third harmonic because it
 * spreads the error across the interval instead of piling it at the
 * peak.  tests/test_patch.py runs the patched renderer for a full
 * period under emulation and measures what comes out. */
.Lsin_c1:  .float  1.570792380
.Lsin_c3:  .float -0.645906016
.Lsin_c5:  .float  0.079464867
.Lsin_c7:  .float -0.004352813

/* ------------------------------------------------------------------ */
/* np_tri / np_square / np_saw -- band-limited waveforms 1..3.         */
/*                                                                     */
/* Each is spliced at the point where the stock renderer has finished  */
/* wrapping the phase and is about to compute the sample, which is     */
/* also where both wrap paths converge:                                */
/*                                                                     */
/*     0x0815669A  triangle   phase +0x5C, step +0x64                  */
/*     0x081566D2  square     phase +0x80, step +0x88                  */
/*     0x0815670E  sawtooth   phase +0xA4, step +0xAC                  */
/*                                                                     */
/* Entry: s15 is the wrapped phase, r4 the oscillator.  The stock code */
/* stores the phase a couple of instructions later, so these do it.    */
/* Exit through the caller's epilogue, hence `pop {r4, pc}`; `bl`      */
/* clobbers lr, which is fine because lr was pushed on entry and the   */
/* return comes off the stack.  r4 survives the call by the ABI.       */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
    .global np_tri
np_tri:
    vstr    s15, [r4, #0x5C]
    vmov.f32 s0, s15
    vldr    s1, [r4, #0x64]
    bl      np_tri_value
    pop     {r4, pc}

    .thumb_func
    .align 2
    .global np_square
np_square:
    vstr    s15, [r4, #0x80]
    vmov.f32 s0, s15
    vldr    s1, [r4, #0x88]
    bl      np_square_value
    pop     {r4, pc}

    .thumb_func
    .align 2
    .global np_saw
np_saw:
    vstr    s15, [r4, #0xA4]
    vmov.f32 s0, s15
    vldr    s1, [r4, #0xAC]
    bl      np_saw_value
    pop     {r4, pc}

/* ------------------------------------------------------------------ */
/* np_eval_linad / np_eval_expad -- evaluate a modulation slot once per  */
/* audio block instead of once per sample.                              */
/*                                                                      */
/* The stock calls a slot's evaluator on every one of the eight samples  */
/* in a block, for all nineteen slots, whether or not the slot modulates */
/* anything: a factory-default project costs the same 28,670 instructions*/
/* as a fully loaded one, and stubbing the two evaluators out drops that */
/* to 13,052 with the output bit-identical -- 264 peripheral writes the  */
/* same over 24 blocks, every differing byte inside the slots' own phase */
/* and envelope state.  Half of that 54 % is the stock's ramp/LFO/       */
/* routing, half is this patch's RC envelopes and band-limited waveforms */
/* being computed for a modulator at zero depth.                        */
/*                                                                      */
/* A modulation slot's output is a control signal whose bandwidth is its */
/* own LFO rate, so evaluating it at 4 kHz rather than 32 kHz is not     */
/* something the destination can show -- the same trade the envelopes    */
/* already make one level down, and here it is a modulator, one step     */
/* further from the ear.  The exception is a slot running at audio rate, */
/* where the output IS the signal: owner+0x25 is the stock's own flag    */
/* for that (it tests it at 0x08112C74) and those slots are never held.  */
/*                                                                      */
/* Transparent by construction: the evaluators return their level in s0  */
/* alone -- every exit does vpop {d8, d9}, so s16..s19 belong to the     */
/* caller -- and they clobber r0-r3 and s0-s15, which the AAPCS lets a   */
/* call clobber anyway.  So the guard needs no frame on the hold path    */
/* and tail-calls on the audio-rate path.                               */
/* ------------------------------------------------------------------ */
/* save the field, multiply it by eight, put it back on the way out */
    .macro SCALE8 slot, off
    vldr    s2, [r0, #\off]
    vstr    s2, [sp, #(\slot * 4)]
    vmul.f32 s2, s2, s1
    vstr    s2, [r0, #\off]
    .endm
    .macro RESTORE8 slot, off
    vldr    s2, [sp, #(\slot * 4)]
    vstr    s2, [r0, #\off]
    .endm

    .thumb_func
    .align 2
    .global np_eval_linad
np_eval_linad:
    ldr     r12, =(0x0811108C | 1)
    b       np_slot_hold

    .thumb_func
    .align 2
    .global np_eval_expad
np_eval_expad:
    ldr     r12, =(0x08110894 | 1)
    /* fall through */

    .thumb_func
    .align 2
np_slot_hold:
    ldrb    r1, [r0, #0x25]              /* audio rate: the output IS the  */
    cbnz    r1, 1f                       /* signal, never hold it          */
    ldrb    r2, [r0, #4]                 /* slot index                     */
    cmp     r2, #20
    bhi     1f                           /* out of range: never hold       */
    ldr     r3, =NP_SLOT_HOLD
    add.w   r3, r3, r2, lsl #3           /* 12-byte entries                */
    add.w   r3, r3, r2, lsl #2
    /* One count per sample in the top three bits, counting down: the
     * borrow out of bit 31 lands exactly on every eighth call and on the
     * first one after np_early cleared the word, so the cached level is
     * never read before it has been written. */
    ldr     r2, [r3, #4]
    subs    r2, r2, #(1 << 29)
    str     r2, [r3, #4]
    bcc     3f                           /* this call is the block's       */
    /* A gate flag set since the boundary -- the sequencer's interrupt
     * lands between samples -- is taken now, not seven samples later:
     * a drum's X-Env attack has to start on its note.  The call stands
     * for the samples left in the block, counter + 1 of them, so the
     * increments are scaled by that instead of eight; the counter keeps
     * running and the next boundary evaluates as usual. */
    ldrb    r1, [r0, #0x21D]             /* gate on                        */
    cbnz    r1, 5f
    ldrb    r1, [r0, #0x21E]             /* gate off                       */
    cbnz    r1, 5f
    /* ...the other seven walk the line to the block's end value.  A
     * zero-order hold was a 250 us staircase on every held modulation:
     * on a kick's pitch sweep or a drum's OutVol X-Env that is a zipper
     * the per-sample stock never had. */
2:  vldr    s0, [r3]
    vldr    s1, [r3, #8]
    vadd.f32 s0, s0, s1
    vstr    s0, [r3]
    bx      lr
1:  bx      r12                          /* tail call, fully transparent   */
3:  vmov.f32 s1, #8.0                    /* a whole block                  */
    b       6f
5:  lsrs    r1, r2, #29
    adds    r1, #1                       /* samples from here to the end   */
    vmov    s1, r1
    vcvt.f32.u32 s1, s1
6:

    /* One call has to move the slot as far as eight would, or every LFO
     * and every envelope in a held slot runs at an eighth of its rate --
     * measured, and it is the whole reason this is not just a skip.  So
     * scale the three X-Env stage increments and the five waveform phase
     * steps by eight around the call and put the originals back: times
     * eight and back is exact for a normal float, so a slot that is never
     * held is bit-for-bit untouched and a held one has the same increments
     * afterwards as before.  NP_BLOCK_MODE tells np_env_rc_step that the
     * increment it is about to read already covers a block.            */
    push    {r0, r3, r4, lr}
    sub     sp, #24                      /* four saved fields, aligned     */
    /* Publish the number of samples this call stands for, not a flag.  A
     * gate event mid-block makes that fewer than eight, and np_wave_shape
     * advances the LFO phase by it: while this said "1" and np_wave_shape
     * multiplied by a hardcoded 8, every note made every held slot's LFO
     * jump a whole extra block -- measured at 2.00x the correct phase rate
     * with one note per block and 3.00x with two. */
    vcvt.u32.f32 s2, s1
    vmov    r4, s2                       /* N, 1..8                        */
    ldr     r2, =NP_BLOCK_MODE
    str     r4, [r2]
    /* and (N-1)/N, the attack's reach to the end of this stretch */
    vmov.f32 s2, #1.0
    vmov.f32 s3, s2
    vdiv.f32 s2, s2, s1                  /* 1/N                            */
    vsub.f32 s2, s3, s2                  /* (N-1)/N                        */
    ldr     r2, =NP_BLOCK_FRAC
    vstr    s2, [r2]
    /* Only the three X-Env stage increments: the STOCK ramp advance reads
     * them.  The five waveform phase steps are read by np_wave_shape alone,
     * which scales its own step while NP_BLOCK_MODE is set -- three
     * instructions there against thirty of scaling and restoring here. */
    SCALE8  0, 0x1FC                     /* X-Env attack increment       */
    SCALE8  1, 0x200                     /*       decay                  */
    SCALE8  2, 0x204                     /*       release                */
    /* ...and the slot's output smoother.  Every slot passes its value
     * through a one-pole TPT lowpass, y = (g x + s)/(1+g), s' = 2y - s,
     * g = tan(pi 100 Hz / 32 kHz) at owner+0x1D8 (one constant, written
     * by the base constructor 0x0811CF28): the stock's de-zipper.  Run
     * once per block with the per-sample g it was a 12.5 Hz lowpass --
     * a held slot (VCO vol/wave/fine, reso, color, filter env, FM, out
     * vol) took ~30 ms instead of ~4 to reach a new value, which a mod
     * note switching instruments note to note made plain (p19-p35).
     * One step with gN = (1-a^N)/(1+a^N), a = (1-g)/(1+g), decays the
     * state exactly as N per-sample steps do.  A coefficient bypassed
     * for a note start (np_dsp_sample) is left as it is. */
    vldr    s2, [r0, #0x1D8]
    vstr    s2, [sp, #16]
    ldr     r1, [r0, #0x1D8]
    ldr     r2, =SNAP_G_BITS
    cmp     r1, r2
    beq     7f
    vmov.f32 s3, #1.0
    vsub.f32 s4, s3, s2                  /* 1 - g                         */
    vadd.f32 s5, s3, s2                  /* 1 + g                         */
    vdiv.f32 s4, s4, s5                  /* a, the per-sample decay       */
    vmov.f32 s5, s4
    subs    r1, r4, #1
8:  cmp     r1, #0
    beq     9f
    vmul.f32 s5, s5, s4                  /* a^N                           */
    subs    r1, #1
    b       8b
9:  vsub.f32 s6, s3, s5
    vadd.f32 s7, s3, s5
    vdiv.f32 s6, s6, s7                  /* gN                            */
    vstr    s6, [r0, #0x1D8]
7:  vstr    s1, [sp, #12]                /* the sample count: the call   */
    blx     r12                          /* clobbers s0-s15              */
    ldr     r0, [sp, #24]
    RESTORE8 0, 0x1FC
    RESTORE8 1, 0x200
    RESTORE8 2, 0x204
    vldr    s2, [sp, #16]
    vstr    s2, [r0, #0x1D8]
    vldr    s1, [sp, #12]
    movs    r4, #0
    ldr     r2, =NP_BLOCK_MODE
    str     r4, [r2]
    ldr     r2, =NP_BLOCK_FRAC
    str     r4, [r2]
    add     sp, #24
    pop     {r0, r3, r4, lr}
    /* s0 is where this stretch ENDS (the state advanced s1 samples); the
     * entry holds where it is now.  Keep the slope and step onto it. */
    vldr    s2, [r3]                     /* current level                  */
    vsub.f32 s0, s0, s2                  /* distance to the end value      */
    vdiv.f32 s0, s0, s1                  /* per sample, s1 samples to go   */
    vstr    s0, [r3, #8]                 /* the slope                      */
    vadd.f32 s0, s0, s2
    vstr    s0, [r3]                     /* this sample's level            */
    bx      lr
    .ltorg

/* ------------------------------------------------------------------ */
/* N1: a note starts on its own parameters, not on a 4 ms glide from    */
/* the previous note's.                                                  */
/*                                                                      */
/* Every one of the 19 CV destinations passes its value through the     */
/* slot's 100 Hz one-pole smoother (owner+0x1D8 g, +0x1DC state, +0x1E8 */
/* input, +0x1EC output) -- a de-zipper for knobs, and right for them.  */
/* But a mod note is a whole instrument switched at a note-on, and so   */
/* is a step's parameter lock: the hat after the kick started with the  */
/* kick's closed filter, low pitch and long decay and slid to its own  */
/* over ~4 ms (90 %), its transient smeared by exactly the parameters it */
/* was meant to change.  Measured on sysemu, kick -> hat mod notes:     */
/* cutoff 47 % of the way after 1 ms, 72 % after 2.                     */
/*                                                                      */
/* np_note_start replaces the tail call `b.w 0x0811612C` at 0x08116D30  */
/* -- the note start every note-on path reaches (0x08116CF4); a legato  */
/* note (0x08116D48) does not come here, so slides and glides are kept. */
/* After the stock has applied the note's parameters and gates it asks  */
/* the DSP for eight samples with the smoothers bypassed.               */
/*                                                                      */
/* np_dsp_sample replaces `bl 0x08112488` at 0x0811503C, the per-sample */
/* DSP call (the CV engine's callback).  While a snap is pending it     */
/* sets every smoother's g to 1e9 -- y = (g x + s)/(1+g) is then x on   */
/* the very first sample -- runs the sample, then puts each state on    */
/* its input (s' = 2y - s would ring otherwise) and, after the eighth,  */
/* gives the stock g back.  Eight samples cover the block boundary at   */
/* which a held slot (np_slot_hold) evaluates.  Only a coefficient that */
/* holds the stock value is touched, and only it is restored.  No snap  */
/* pending: one load and a tail call.                                   */
/* ------------------------------------------------------------------ */
    .equ SNAP_G_BITS, 0x4E6E6B28         /* 1e9f                           */
    .equ ENGINE,      0x20004834         /* project+0x44                   */
    .equ SLOT_OWNERS, 0x2E68             /* engine+: 19 owner pointers     */
    .equ SNAP_SAMPLES, 8

    .thumb_func
    .align 2
    .global np_note_start
np_note_start:
    /* Asked before AND after the stock: the 171-parameter evaluation in
     * 0x0811612C takes ~70 us, so an audio block can land inside it --
     * its samples must not glide either, and the eight bypassed samples
     * have to be counted from the moment the last parameter is in. */
    push    {r0, r1, r2, lr}             /* r0/r1 args (r2 pads), s0 kept  */
    bl      np_snap_request
    pop     {r0, r1, r2, lr}
    push    {r4, lr}
    ldr     r12, =(0x0811612C | 1)
    blx     r12                          /* args r0, r1, s0 untouched      */
    bl      np_snap_request
    pop     {r4, pc}

    .thumb_func
np_snap_request:                         /* clobbers r0-r2                 */
    ldr     r0, =(DIAG + D_SNAP)
    ldr     r1, [r0]
    cbnz    r1, 1f                       /* a snap in flight: g is saved   */
    ldr     r2, =(ENGINE + 0x48 + 0x1D8) /* VCO1Tune's g, every slot's     */
    ldr     r2, [r2]
    str     r2, [r0, #(D_SNAP_G - D_SNAP)]
    ldr     r1, [r0, #(D_SNAPS - D_SNAP)]
    adds    r1, #1
    str     r1, [r0, #(D_SNAPS - D_SNAP)]
1:  movs    r1, #SNAP_SAMPLES
    dmb
    str     r1, [r0]
    bx      lr
    .ltorg

    .thumb_func
    .align 2
    .global np_dsp_sample
np_dsp_sample:
    ldr     r1, =(DIAG + D_SNAP)
    ldr     r2, [r1]
    cbnz    r2, 1f
    ldr     r12, =(0x08112488 | 1)
    bx      r12                          /* the usual sample               */
1:  push    {r4, r5, r6, r7, r8, lr}
    mov     r4, r0                       /* engine                         */
    mov     r5, r1                       /* &D_SNAP                        */
    ldr     r8, [r5, #(D_SNAP_G - D_SNAP)]
    ldr     r7, =SNAP_G_BITS
    add.w   r6, r4, #0x2E00              /* engine+0x2E68: owner pointers  */
    adds    r6, #0x68
    movs    r3, #19
2:  ldr     r0, [r6], #4
    ldr     r1, [r0, #0x1D8]
    cmp     r1, r8
    it      eq
    streq   r7, [r0, #0x1D8]             /* bypass                         */
    subs    r3, #1
    bne     2b
    mov     r0, r4
    ldr     r12, =(0x08112488 | 1)
    blx     r12
    mov     lr, r0                       /* the DAC codes, returned        */
    ldr     r2, [r5]
    subs    r2, #1
    str     r2, [r5]
    add.w   r6, r4, #0x2E00              /* engine+0x2E68: owner pointers  */
    adds    r6, #0x68
    movs    r3, #19
3:  ldr     r0, [r6], #4
    ldr     r1, [r0, #0x1D8]
    cmp     r1, r7
    bne     4f                           /* not ours                       */
    ldr     r1, [r0, #0x1E8]             /* state onto the input           */
    str     r1, [r0, #0x1DC]
    cbnz    r2, 4f
    str     r8, [r0, #0x1D8]             /* the last one: stock g back     */
4:  subs    r3, #1
    bne     3b
    mov     r0, lr
    pop     {r4, r5, r6, r7, r8, pc}
    .ltorg

/* ------------------------------------------------------------------ */
/* CH: an instrument switch fades the sounding one out, then starts.    */
/*                                                                      */
/* The amp ADSR and the X-Envs continue from the level they are at      */
/* (stock, analogue-style), so a hat after a kick with a long tail      */
/* starts at the kick's level -- no attack from silence -- while after  */
/* another hat it rises from 0: the same hat sounds different by what   */
/* came before it.  Zeroing the level at the trigger (p26) clicked: a   */
/* step from a ringing tail, and an attack squeezed because the stock's */
/* own ramp kept its residual.  So: when a note-on switches instrument  */
/* (a different mod note, or mod note <-> plain note, per layer) while  */
/* the amp envelope is still above 1 % and no note is held (a legato    */
/* note does not retrigger), the note-on is held back (np_runtime.cpp,  */
/* np_choke_note_on), the DSP fades the sounding note's VCA level to 0  */
/* over np_choke_samples (np_env_main, np_xenv.S), puts the amp ADSR    */
/* and the 14 X-Envs at rest at 0 -- stage idle, stock level 0, RC      */
/* level 0, so the next attack runs its full course from silence -- and */
/* the main loop then applies the held note (np_main_service).  Any     */
/* other note-on or note-off first lets the held one go.                */
/*                                                                      */
/* np_cnote_entry replaces `bl 0x081170E8` (the CNoteData note-on       */
/* handler) at 0x08117AD0; np_noteoff_entry the prologue of the         */
/* note-off handler 0x08117594 (six callers).                           */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
    .global np_cnote_entry
np_cnote_entry:                          /* r0 = project, r1 = &event      */
    push    {r0, r1, r2, lr}
    bl      np_choke_note_on
    mov     r3, r0
    pop     {r0, r1, r2, lr}
    cbnz    r3, 1f                       /* held back: ownership taken     */
    ldr     r12, =(0x081170E8 | 1)
    bx      r12
1:  bx      lr

    .thumb_func
    .align 2
    .global np_noteoff_entry
np_noteoff_entry:                        /* r0 sret, r1 project, r2 &event */
    push    {r0, r1, r2, lr}
    bl      np_choke_flush
    pop     {r0, r1, r2, lr}
    push.w  {r4, r5, r6, r7, r8, r9, lr} /* displaced                      */
    ldr     r12, =(0x08117598 | 1)
    bx      r12
    .ltorg

/* np_choke_reset_envs: the amp ADSR and the 14 X-Envs to rest at 0.     */
/* Called by the DSP at the end of a fade (inside np_env_main's frame)   */
/* and by the main loop with interrupts masked.  Uses r0-r3 and saves    */
/* r4-r6: r12 may be live in the DSP body around the splice.            */
    .thumb_func
    .align 2
    .global np_choke_reset_envs
np_choke_reset_envs:
    push    {r4, r5, r6, lr}
    ldr     r0, =ENGINE
    movs    r1, #0
    movs    r4, #4                       /* stage: idle                    */
    strb    r4, [r0, #0x31]
    str     r1, [r0, #0x40]              /* the stock's level              */
    ldr     r2, =NP_ENV_STATE            /* entry 0: the amp ADSR          */
    bl      2f
    add.w   r5, r0, #0x2E00
    adds    r5, #0x68                    /* the 19 slot owners             */
    movs    r6, #0                       /* slot                           */
3:  cmp     r6, #13                      /* 13..16 (envelope times) and 18 */
    blo     4f                           /* (sync) have no X-Env           */
    cmp     r6, #17
    bne     5f
4:  ldr     r3, [r5, r6, lsl #2]
    add.w   r3, r3, #0x1F0               /* the X-Env's stage machine      */
    strb    r4, [r3, #0x31]
    str     r1, [r3, #0x40]
    ldr     r2, =NP_ENV_STATE
    add.w   r2, r2, r6, lsl #5
    adds    r2, #32                      /* entry slot + 1                 */
    bl      2f
5:  adds    r6, #1
    cmp     r6, #19
    bne     3b
    pop     {r4, r5, r6, pc}
2:  str     r1, [r2, #0]                 /* E_Y                            */
    str     r1, [r2, #16]                /* E_DY                           */
    ldr     r3, [r2, #4]                 /* E_FLAGS: block position kept,  */
    lsrs    r3, r3, #29                  /* idle seen, not from decay      */
    lsls    r3, r3, #29
    orr     r3, r3, #5
    str     r3, [r2, #4]
    bx      lr
    .ltorg

/* ------------------------------------------------------------------ */
/* np_set_rate -- skip setRate when its input has not changed.          */
/*                                                                      */
/* The per-sample slot walk at 0x08112C70 recomputes an audio-rate       */
/* slot's position and calls setRate (0x08156910) for it on every        */
/* sample.  setRate writes the phase step of all FIVE waveforms from     */
/* the rate, 73 instructions, when one waveform is selected -- and its   */
/* first act is to store its own argument at [r0+8].  Nothing else       */
/* writes those steps (the per-sample advance writes the phase), so when */
/* the rate is bit-identical to the one already stored, every store      */
/* setRate would perform is a store of the value already there.          */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
    .global np_set_rate
np_set_rate:
    vmov    r1, s0
    ldr     r2, [r0, #8]                 /* the rate setRate last stored   */
    cmp     r1, r2
    it      eq
    bxeq    lr
    ldr     r12, =(0x08156910 | 1)
    bx      r12
    .ltorg

/* ------------------------------------------------------------------ */
/* np_slot_walk / np_after_walk -- the audio-rate slot walk, once per   */
/* block instead of once per sample.                                    */
/*                                                                      */
/* The DSP body walks all nineteen slot pointers on every sample to     */
/* find the ones flagged audio rate (owner+0x25) and recompute their    */
/* modulator frequency: six instructions per slot that is not, so about */
/* 114 per sample, 900 per block, 4.8 % of it, to find the same handful */
/* every time.  The set changes only when a slot's mode is edited,      */
/* which the main loop does between blocks, never inside one.           */
/*                                                                      */
/* So the first sample of a block does the full walk and copies the     */
/* audio-rate pointers into a compact list; the other seven hand the    */
/* stock loop that list instead.  The loop body itself -- the position  */
/* from the knob and VCO1's live pitch, the table, setRate -- is the    */
/* stock's own code, untouched, running over a shorter array.           */
/*                                                                      */
/* Loop contract at 0x08112C70: r6 is the element BEFORE the first (it  */
/* pre-increments), fp is the last element, and the entry jump lands on */
/* the ldr, so the array must hold at least one element or the exit at  */
/* 0x08112CE0 is taken directly.  After the loop r6 == fp; both are     */
/* dead there (r6 is redefined at 0x08112DDE) but np_after_walk puts    */
/* the stock values back regardless.                                    */
/*                                                                      */
/* Spliced over `vstr s15,[r3]; b 0x08112C70` at 0x08112C4A (the vstr  */
/* is replayed) and over `bl 0x08111848` at 0x08112CE2.                 */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
    .global np_slot_walk
np_slot_walk:
    vstr    s15, [r3]                    /* displaced                      */
    ldr     r2, =NP_WALK
    str     r11, [r2, #W_FP]             /* the stock end, for the exit    */
    ldr     r1, [r2, #W_TICK]
    subs    r1, r1, #(1 << 29)
    str     r1, [r2, #W_TICK]
    bcc     3f                           /* the block's first sample       */
    ldr     r1, [r2, #W_COUNT]
2:  cbz     r1, 4f                       /* nothing at audio rate          */
    add.w   r6, r2, #(W_LIST - 4)        /* one before the first           */
    add.w   r11, r6, r1, lsl #2          /* the last                       */
    ldr     r0, =(0x08112C70 | 1)
    bx      r0
4:  ldr     r0, =(0x08112CE0 | 1)        /* r6 == fp already: the exit     */
    bx      r0
3:  /* the full walk, collecting */
    push    {r4, r5}
    movs    r1, #0
    add.w   r4, r2, #W_LIST
    mov     r5, r6
5:  cmp     r11, r5
    beq     6f
    ldr     r0, [r5, #4]!
    ldrb    r3, [r0, #0x25]
    cmp     r3, #0                       /* cbz cannot branch backwards    */
    beq     5b
    str     r0, [r4, r1, lsl #2]
    adds    r1, #1
    b       5b
6:  str     r1, [r2, #W_COUNT]
    pop     {r4, r5}
    b       2b
    .ltorg

    .thumb_func
    .align 2
    .global np_after_walk
np_after_walk:
    ldr     r2, =NP_WALK
    ldr     r11, [r2, #W_FP]             /* as the stock loop left them    */
    mov     r6, r11
    ldr     r2, =(0x08111848 | 1)        /* r0 = r4 was already done       */
    bx      r2
    .ltorg

/* ------------------------------------------------------------------ */
/* np_flash_write -- the project journal writer 0x08143378, fenced.     */
/*                                                                      */
/* Replaces the `bl 0x08143378` at 0x0812407C.  Marks the operation for */
/* np_systick_irq (see there), counts it, and records its duration, so  */
/* a capture shows how long the unit was inside it.  r0-r2 are the      */
/* writer's arguments and pass straight through.                        */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
    .global np_flash_write
np_flash_write:
    push    {r4, lr}
    ldr     r4, =DIAG
    ldr     r3, =UWTICK
    ldr     r3, [r3]
    str     r3, [r4, #D_FLASH_TICK0]
    ldr     r3, =DWT_CYCCNT
    ldr     r3, [r3]
    orr     r3, r3, #1                   /* never zero while active        */
    str     r3, [r4, #D_FLASH_ACTIVE]
    ldr     r3, [r4, #D_FLASH_COUNT]
    adds    r3, #1
    str     r3, [r4, #D_FLASH_COUNT]
    ldr     r12, =(0x08143378 | 1)
    blx     r12
    ldr     r3, =DWT_CYCCNT
    ldr     r3, [r3]
    ldr     r2, [r4, #D_FLASH_ACTIVE]
    subs    r3, r3, r2                   /* duration, cycles (bit 0 noise) */
    ldr     r2, [r4, #D_FLASH_MAX]
    cmp     r3, r2
    it      hi
    strhi   r3, [r4, #D_FLASH_MAX]
    movs    r3, #0
    str     r3, [r4, #D_FLASH_ACTIVE]
    pop     {r4, pc}
    .ltorg

/* ------------------------------------------------------------------ */
/* np_led_smooth -- the LED brightness fade, made to arrive.            */
/*                                                                      */
/* The writer at 0x08109FD0 visits one LED per SysTick and fades its    */
/* brightness towards the target with  s' = 0.33 s + 0.66 t,  then      */
/* clears the LED's dirty flag when (u32)s' == (u32)t.  Those two        */
/* constants sum to 0.99, so the fade settles at 0.985 t: an LED that    */
/* dims passes through the integer from above and clears; an LED that   */
/* brightens approaches from below, its floor stays t - 1 for ever, and  */
/* it is re-sent every 70 ticks until power-off.  Measured on the unit:  */
/* 15-23 LEDs permanently dirty, ~1,300 SPI frames a second carrying     */
/* nothing new, and every one of them a chance at the frame collision    */
/* patch L guards against.                                              */
/*                                                                      */
/* Exact thirds, and a landing: once the remaining distance is under one */
/* brightness step -- invisible, the byte sent is an integer -- go to    */
/* the target itself, so the dirty test succeeds and the LED goes quiet. */
/*                                                                      */
/* Spliced over the vmul/vfma pair at 0x0810A098.  In: s14 = s, s15 = t. */
/* Out: s14 = s'.  s10-s13 are free (the stock reloads s13 after this   */
/* and this is an exception handler with its own FP frame); lr was      */
/* pushed at entry and already spent on two bl's before this point.     */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
    .global np_led_smooth
np_led_smooth:
    vsub.f32  s12, s15, s14             /* d = t - s                      */
    vabs.f32  s13, s12
    vmov.f32  s11, #1.0
    vcmpe.f32 s13, s11
    vmrs      APSR_nzcv, fpscr
    bmi       1f                        /* under a step: land on it       */
    vldr      s13, .Lled_k
    vfma.f32  s14, s12, s13             /* s + (2/3)(t - s)               */
    bx        lr
1:  vmov.f32  s14, s15
    bx        lr
    .align 2
.Lled_k:
    .float    0.6666667

/* ------------------------------------------------------------------ */
/* np_list_sort -- the pattern store's per-step sort, only when needed.  */
/*                                                                      */
/* The sequencer keeps each of its 2 x 64 steps as a std::list of events */
/* (seq 0x2000CFF4, tracks at +0x8C and +0x6C8, 0xC per step) plus a    */
/* list of pending edits per step.  0x0815888C, called by main() every  */
/* iteration through 0x08146E70, walks the steps under a 125 us budget  */
/* per track, and 0x081585C0 commits a step's pending edits and then    */
/* calls std::list::sort (0x081584AC) on the step -- unconditionally,   */
/* also when there was nothing to commit.  So every pass re-sorts every */
/* step of an unchanged pattern: 64 bin heads initialised and n log n   */
/* splices per step.  Measured on sysemu, 16 steps with a note and six  */
/* parameter locks each: 27,300 main-loop instructions a pass, 67 % of  */
/* them that sort, and with the budget the unit's main period is ~two   */
/* of those 125 us slices.  main() also processes the sequencer's ticks */
/* (0x08119270, flagged by TIM5) and the MIDI input, so that period is  */
/* how late a step or an incoming note can be.                          */
/*                                                                      */
/* The sort is stable (merge takes the left node on equal keys) and its */
/* key is the unsigned byte at node+8, `cmp; bhs` in both merge loops.  */
/* A list whose keys never decrease therefore comes back with the same */
/* links, the same order and the same size it went in with: skipping    */
/* the call for it is exact.  Anything else gets the stock sort.        */
/* Replaces the only call, `bl 0x081584AC` at 0x08158708 (r0 = head).  */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
    .global np_list_sort
np_list_sort:
    ldr     r1, [r0]                     /* first node                    */
    cmp     r1, r0
    beq     2f                           /* empty                         */
    ldrb    r2, [r1, #8]
1:  ldr     r1, [r1]
    cmp     r1, r0
    beq     2f                           /* back at the head: in order    */
    ldrb    r3, [r1, #8]
    cmp     r3, r2
    blo     3f                           /* a key below its predecessor   */
    mov     r2, r3
    b       1b
2:  bx      lr
3:  ldr     r12, =(0x081584AC | 1)       /* r0 is still the head          */
    bx      r12
    .ltorg

/* ------------------------------------------------------------------ */
/* np_seq_worker -- main()'s pass over the pattern store, when idle.     */
/*                                                                      */
/* With S1 the pass no longer sorts, but it still visits every step of  */
/* both tracks on every main-loop iteration -- 64 x (cursor, budget     */
/* clock read, commit call, empty-list checks) -- to find nothing: 82 % */
/* of main()'s own instructions on a locked 16-step pattern.  A pass    */
/* does real work only when a clear is running (+0x635), a rotation is  */
/* (+0x632 != +0x633), some step has a pending edit (the second array   */
/* of list heads at +0x300), or some step's events are out of order     */
/* (S1's test).  When none of that holds, the stock pass calls no       */
/* callback and changes no list; its only effects are its own cursor   */
/* (+0x610) and the +0x638 flag.  The cursor is read nowhere but in the */
/* worker and in 0x08158A48, a flush that sets it, calls 0x0815888C     */
/* directly until it reaches a target and is not routed here; +0x638 is */
/* written 1 only inside a clear and back to 0 on the next step, and   */
/* its readers look at it only during one.  So: return.                 */
/*                                                                      */
/* Replaces both calls in 0x08146E70 (`bl` at 0x08146E76, the tail `b.w` */
/* at 0x08146E82).  r0 = track; r4 is the caller's and is preserved.    */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
    .global np_seq_worker
np_seq_worker:
    ldrb    r1, [r0, #0x635]             /* a clear in progress            */
    cbnz    r1, 9f
    ldrb    r1, [r0, #0x632]
    ldrb    r2, [r0, #0x633]
    cmp     r1, r2                       /* a rotation in progress         */
    bne     9f
    push    {r4, r5}
    mov     r1, r0                       /* step i's event list head      */
    add.w   r4, r0, #0x300               /* past the last: pending heads  */
1:  ldr     r2, [r1, #0x300]             /* step i's pending edits         */
    add.w   r3, r1, #0x300
    cmp     r2, r3
    bne     8f                           /* something to commit           */
    ldr     r2, [r1]
    cmp     r2, r1
    beq     3f                           /* no events                     */
    ldrb    r3, [r2, #8]
2:  ldr     r2, [r2]
    cmp     r2, r1
    beq     3f
    ldrb    r5, [r2, #8]
    cmp     r5, r3
    blo     8f                           /* out of order: the stock sorts */
    mov     r3, r5
    b       2b
3:  adds    r1, #0xC
    cmp     r1, r4
    bne     1b
    pop     {r4, r5}
    bx      lr                           /* the stock pass would find nothing */
8:  pop     {r4, r5}
9:  ldr     r12, =(0x0815888C | 1)       /* r0 = track, lr = our caller's */
    bx      r12
    .ltorg

/* ------------------------------------------------------------------ */
/* np_morph_trig -- the morph pattern's unlinked X-Env trigs follow the  */
/* morph knob like its notes do.                                        */
/*                                                                      */
/* The sequencer plays two tracks: 0 the active pattern, 1 the "morphing */
/* pattern" ([FUNC]+[PATTERN]; pattern 1 by default), mixed by the morph */
/* position at project+0x604C ([FUNC] + [CUTOFF], 0 = active only).     */
/* Notes are gated by it -- 0x08117A44 drops a track-1 note below 0.02  */
/* and a track-0 note above 0.98 -- and so is the note path's X-Env      */
/* trigger: 0x0811612C stops a note triggering a slot whose morph-track */
/* unlinked trigs are live, "live" being morph > 0.01.  The trigs       */
/* themselves are not gated at all: SequencerEvent<ParamTrigData>'s tick */
/* (0x0811DCB8) fires whatever track it is on.  So the morph partner's  */
/* unlinked trigs play in every pattern, whatever the knob says, firing */
/* that slot's X-Env and resetting its LFO on their own rhythm -- which */
/* was heard on the cutoff, and the X-Env event log (p32) caught:      */
/* every extra trigger a TRIG-from-PARAM on the morph pattern's steps.  */
/*                                                                      */
/* Replaces `blx r2; b 0x08146766` at 0x08146804 in the step processor  */
/* 0x08146694 -- the generic per-tick call of a pending event, r0 = the */
/* event, r1 = its tick count, r2 = its tick handler, the track index   */
/* at [sp,#4].  A ParamTrig tick on track 1 with morph <= 0.01 is       */
/* skipped; the node's count and lifetime are kept (0x08146766 on).     */
/* r3, s14, s15 are free: the call they replace may clobber them.       */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
    .global np_morph_trig
np_morph_trig:
    ldr     r3, =(0x0811DCB8 | 1)        /* SequencerEvent<ParamTrigData> tick */
    cmp     r2, r3
    bne     1f
    ldr     r3, [sp, #4]                 /* the track: 0 active, 1 morph   */
    cbz     r3, 1f
    ldr     r3, =(PROJECT + 0x604C)      /* morph position                 */
    vldr    s15, [r3]
    vldr    s14, .Lmorph_live
    vcmpe.f32 s15, s14
    vmrs    APSR_nzcv, fpscr
    bhi     1f                           /* the morph pattern is in: stock */
    ldr     r3, =(0x08146766 | 1)        /* silent: no tick, count kept    */
    bx      r3
1:  blx     r2
    ldr     r3, =(0x08146766 | 1)
    bx      r3
    .align 2
.Lmorph_live:
    .float  0.01                         /* 0x0811612C's threshold         */
    .ltorg

/* ------------------------------------------------------------------ */
/* np_project_gate (Q1) -- the project service holds a flash-touching   */
/* paste back while the QSPI operation queue is busy.                   */
/*                                                                      */
/* Replaces `ldr r3,[r4,#0x1C]; ldr r2,[r3,#8]` at 0x08157672, the head */
/* of 0x08157650's loop (r4 = the manager, node = first list node,      */
/* node+8 = its event); np_project_first enters there too.  The check   */
/* is np_qspi_defer (np_runtime.cpp, which has the why).  Deferring is  */
/* leaving the node where it is and taking the loop's own exit at       */
/* 0x081576CA; anything else rejoins at 0x08157676 with r2/r3 as the    */
/* stock pair left them and r0 = manager, r1 = sp+4, the dispatch's    */
/* arguments both loop entries prepare.  r12 and lr are free; r5-r9    */
/* are the loop's and are untouched.                                    */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
    .global np_project_gate
np_project_gate:
    ldr     r3, [r4, #0x1C]              /* the displaced pair            */
    ldr     r2, [r3, #8]
    ldr     r0, =0x2000C68C              /* the project manager only      */
    cmp     r4, r0
    bne     1f
    push    {r2, r3}
    mov     r0, r2
    bl      np_qspi_defer
    pop     {r2, r3}
    cbz     r0, 1f
    ldr     r0, =(0x081576CA | 1)        /* leave it at the head; next pass */
    bx      r0
1:  mov     r0, r4
    add     r1, sp, #4
    ldr     r12, =(0x08157676 | 1)
    bx      r12
    .ltorg

/* ------------------------------------------------------------------ */
/* np_polyat -- polyphonic aftertouch's pressure is its second byte.    */
/*                                                                      */
/* The MIDI input dispatcher 0x0811B700 sends A0 (poly pressure) down   */
/* the D0 (channel pressure) path with the first data byte, r7, as the  */
/* pressure -- but A0's first byte is the KEY: aftertouch followed the  */
/* note number, and a poly-pressure message forwarded to another        */
/* channel went out as channel pressure = key.  Replaces               */
/* `beq 0x0811B79E; bhi 0x0811B80A` after `cmp r0,#0xA0` at 0x0811B754 */
/* with `bhs.w` here; the flags of that cmp arrive intact.  r12 is free.*/
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
    .global np_polyat
np_polyat:
    bne     1f                           /* B0: the stock's bhi            */
    mov     r7, r8                       /* A0 kk vv: pressure = vv        */
    ldr     r12, =(0x0811B79E | 1)
    bx      r12
1:  ldr     r12, =(0x0811B80A | 1)
    bx      r12
    .ltorg

/* ------------------------------------------------------------------ */
/* X-Env event log (diagnostic, 2026-10-02).  On the unit the cutoff    */
/* X-Env fired on its note and a couple more times per pattern; sysemu  */
/* does not reproduce it, so the unit reports every X-Env trigger and   */
/* every restart of an RC envelope itself.  Layout and event types are  */
/* in np_diag.h; tools/diagmidi.py prints the timeline.                 */
/* ------------------------------------------------------------------ */
    .equ EV_BASE,       (DIAG + D_EV_BASE)
    .equ EVB_HEAD,      (D_EV_HEAD - D_EV_BASE)
    .equ EVB_SENT,      (D_EV_SENT - D_EV_BASE)
    .equ EVB_RING,      (D_EV_RING - D_EV_BASE)
    .equ TRANSPORT_POS, 0x2000AF84       /* transport 0x2000AF78 + 0x0C: ticks */
                                         /* since PLAY, 256 per sixteenth     */

/* np_ev_put: r0 = type (1..15), r1 = bit (0..22; anything else -> 23).
 * Appends {uwTick, transport position, type << 24 | 1 << bit}, or ORs the
 * bit into the newest entry when that one is unsent and has the same type
 * and millisecond.  Clobbers r0-r3, r12 and APSR only -- no FP registers.
 *
 * Two entries.  np_ev_put masks interrupts for the ~30 instructions it
 * takes: main() calls it (the X-Env trigger runs there) and the audio
 * handler appends too.  np_ev_put_isr does not: the audio handler, its
 * only caller, cannot be preempted by main() and nothing above it logs --
 * and an M-profile PRIMASK access would stop the A-profile CPU the
 * FP64-capable DSP rig (tests/test_modnote_dsp.py) runs on. */
.macro EV_PUT_BODY masked
    push    {r4, r5, r6, r7}
  .if \masked
    mrs     r12, primask
    cpsid   i
  .endif
    cmp     r1, #23
    it      hi
    movhi   r1, #23
    movs    r5, #1
    lsl.w   r1, r5, r1                   /* the bit                        */
    lsls    r0, r0, #24                  /* the type, top byte             */
    ldr     r5, =UWTICK
    ldr     r5, [r5]                     /* ms                             */
    ldr     r2, =EV_BASE
    ldr     r3, [r2, #EVB_HEAD]
    ldr     r4, [r2, #EVB_SENT]
    cmp     r3, r4
    beq     1f                           /* nothing unsent to merge into   */
    subs    r6, r3, #1
    and     r6, r6, #(NP_EV_RING_N - 1)
    add.w   r6, r6, r6, lsl #1
    add.w   r6, r2, r6, lsl #2           /* + EVB_RING = the newest entry  */
    ldr     r7, [r6, #EVB_RING]
    cmp     r7, r5
    bne     1f                           /* another millisecond            */
    ldr     r7, [r6, #(EVB_RING + 8)]
    eor     r4, r7, r0
    lsrs    r4, r4, #24
    bne     1f                           /* another type                   */
    orr     r7, r7, r1
    str     r7, [r6, #(EVB_RING + 8)]
    b       2f
1:  and     r6, r3, #(NP_EV_RING_N - 1)
    add.w   r6, r6, r6, lsl #1
    add.w   r6, r2, r6, lsl #2
    str     r5, [r6, #EVB_RING]
    ldr     r7, =TRANSPORT_POS
    ldr     r7, [r7]
    str     r7, [r6, #(EVB_RING + 4)]
    orr     r7, r0, r1
    str     r7, [r6, #(EVB_RING + 8)]
    adds    r3, #1
    str     r3, [r2, #EVB_HEAD]
2:
  .if \masked
    msr     primask, r12
  .endif
    pop     {r4, r5, r6, r7}
    bx      lr
    .ltorg
.endm

    .thumb_func
    .align 2
    .global np_ev_put
np_ev_put:
    EV_PUT_BODY 1

    .thumb_func
    .align 2
    .global np_ev_put_isr
np_ev_put_isr:
    EV_PUT_BODY 0

/* np_ev_trig -- spliced over the first two instructions of 0x08156074,  */
/* the stock X-Env trigger (`movs r3,#1; strb r3,[r0,#0x2f]; strb r3,  */
/* [r0,#0x2d]; bx lr`, r0 = owner + 0x1F0).  Logs the slot, by caller:  */
/* the note path returns to 0x08116210, the param-trig/unlinked path to */
/* 0x08118022.  Then does what the stock does.  The stock clobbers only */
/* r3 and the flags, and a caller compiled against it may rely on that */
/* (IPA-RA), so everything else is preserved here.                      */
    .thumb_func
    .align 2
    .global np_ev_trig
np_ev_trig:
    push    {r0, r1, r2, r3, r12, lr}
    sub.w   r1, r0, #0x1F0
    ldrb    r1, [r1, #4]                 /* owner+0x04: the slot           */
    ldr     r2, =0x20004834              /* the main ADSR's own base (the  */
    cmp     r0, r2                       /* engine, project+0x44) comes    */
    it      eq                           /* through here too, from         */
    moveq   r1, #22                      /* 0x0814770C: bit 22             */
    movs    r0, #EV_TRIG_OTHER
    ldr     r2, =(0x08116210 | 1)
    cmp     lr, r2
    it      eq
    moveq   r0, #EV_TRIG_NOTE
    ldr     r2, =(0x08118022 | 1)
    cmp     lr, r2
    it      eq
    moveq   r0, #EV_TRIG_PARAM
    bl      np_ev_put
    pop     {r0, r1, r2, r3, r12, lr}
    movs    r3, #1                       /* displaced                      */
    strb.w  r3, [r0, #0x2F]              /* displaced: the AD flag         */
    b.w     0x0815607A                   /* strb r3,[r0,#0x2d]; bx lr      */
    .ltorg

/* ------------------------------------------------------------------ */
/* CRC-32 (IEEE, reflected), four bits at a time.                      */
/*   r0 = start, r1 = length  ->  r0 = crc                             */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
np_crc32:
    push    {r4, r5, r6, lr}
    mvn     r2, #0
    ldr     r6, =np_crc_nibble
    cbz     r1, 3f
    add     r5, r0, r1
1:  ldrb    r3, [r0], #1
    eor     r2, r2, r3
    and     r4, r2, #0x0F
    lsrs    r2, r2, #4
    ldr     r4, [r6, r4, lsl #2]
    eor     r2, r2, r4
    and     r4, r2, #0x0F
    lsrs    r2, r2, #4
    ldr     r4, [r6, r4, lsl #2]
    eor     r2, r2, r4
    cmp     r0, r5
    blo     1b
3:  mvn     r0, r2
    pop     {r4, r5, r6, pc}
    .ltorg

    .align 2
np_crc_nibble:
    .word 0x00000000, 0x1DB71064, 0x3B6E20C8, 0x26D930AC
    .word 0x76DC4190, 0x6B6B51F4, 0x4DB26158, 0x5005713C
    .word 0xEDB88320, 0xF00F9344, 0xD6D6A3E8, 0xCB61B38C
    .word 0x9B64C2B0, 0x86D3D2D4, 0xA00AE278, 0xBDBDF21C

/* Original switch callback prologue displaced at 0x08155C7C.
 * A normal C call enters here; reproduce both instructions, then rejoin
 * after the patched entry. The stock epilogue returns to the C wrapper's
 * caller (or to the wrapper if its compiler did not emit a tail call).
 * Do not branch to 0x08155C7C: that would recurse into np_ui_switch. */
    .thumb_func
    .align 2
    .global np_ui_stock_switch
np_ui_stock_switch:
    push    {r4, r5, r6, lr}
    mov     r5, r0
    b.w     0x08155C80

/* Installed at the completed RandAutomationData output's submit BL.
 * r0=manager, r1=&output, r4=incoming random request. Tail-call C with
 * r2=request; original LR and all callee-saved registers stay intact. */
    .thumb_func
    .align 2
np_random_splice:
    mov     r2, r4
    b.w     np_random_submit

    .align 3
    .global np_end
np_end:

/* ------------------------------------------------------------------ */
/* R2: a mod note built by black + white records no step.  Both stock   */
/* reads of the RECORD flag become a call that returns it masked       */
/* (np_mn_rec_flag, np_mnrand.cpp); everything live around them is     */
/* preserved, and the stack stays 8-byte aligned for the C call.       */
/* ------------------------------------------------------------------ */
    .thumb_func
    .align 2
    .global np_mn_rec_press
np_mn_rec_press:                 /* for `ldrb.w r3,[r4,#0xed]` at 0x0814FEB8 */
    push    {r0, lr}             /* r4 = UI; r0-r2, r12 are dead here      */
    mov     r0, r4
    ldrb    r1, [r4, #0xe8]      /* the key's mod note                      */
    movs    r2, #0
    bl      np_mn_rec_flag
    mov     r3, r0
    pop     {r0, pc}

/* PL: FUNC+SAVE while a project load waits for the pattern's end is   */
/* ignored -- the store image already holds the new project, and a save */
/* would write the old one over it (np_runtime.cpp).  Replaces the save */
/* key's `bl 0x081263C0` at 0x081516A2; arguments pass through.         */
    .thumb_func
    .align 2
    .global np_save_gate
np_save_gate:
    ldr     r12, =(DIAG + D_PLOAD)
    ldr     r12, [r12]
    eor     r12, r12, r12, lsr #16       /* byte 0 ^ byte 2               */
    tst     r12, #0xFF
    it      ne
    bxne    lr
    ldr     r12, =(0x081263C0 | 1)
    bx      r12
    .ltorg

/* D1: DELETE+RECORD sends ClearPatchData; its handler (0x081279C0)    */
/* resets every record whose byte +4 is the layer, whatever its type --  */
/* and a mod note's records (type 7) carry the mod note there, so mod    */
/* note 0 (the layer-1 pass: mod note 1) lost its sound with the patch.  */
/* The match `ldrb r2,[r3,#4]; cmp r2,r6` at 0x08127A12 becomes a call   */
/* that also turns type 7 away: Z set = reset it.  r3 = the record, r6 = */
/* the layer; r2 is dead after (the reset path reloads it).              */
    .thumb_func
    .align 2
    .global np_clear_match
np_clear_match:
    ldrb    r2, [r3, #5]         /* type                                    */
    cmp     r2, #7
    beq     1f
    ldrb    r2, [r3, #4]
    cmp     r2, r6
    bx      lr
1:  cmp     r2, #0               /* a mod note's: not equal                 */
    bx      lr

    .thumb_func
    .align 2
    .global np_mn_rec_release
np_mn_rec_release:               /* for `ldrb.w r2,[r4,#0xed]` at 0x081521FE */
    push    {r0, r1, r3, lr}     /* r3 (the pitch) is live; r5 = mod note  */
    mov     r0, r4
    mov     r1, r5
    movs    r2, #1
    bl      np_mn_rec_flag
    mov     r2, r0
    pop     {r0, r1, r3, pc}
