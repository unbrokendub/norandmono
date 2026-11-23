/* Host emulator instrumentation only; never linked into firmware. */
.syntax unified
.cpu cortex-m7
.fpu fpv5-d16
.thumb
.section .text
.global capture, cutoff, run_blocks
.thumb_func
capture:
    push {r0-r2, r4-r5, lr}
    mrs r5, apsr
    ldr r0, =0x200076e8       /* engine 0x20004834 + 0x2eb4 */
    ldr r1, =0x10000000
    ldr r2, [r1]
    ldr r3, =0x10e00000
    cmp r2, r3
    bhs overflow
    movs r4, #31
1:  ldr r3, [r0], #4
    str r3, [r2], #4
    subs r4, #1
    bne 1b
    ldr r3, [r1, #4]
    str r3, [r2], #4
    str r2, [r1]
    msr APSR_nzcvq, r5
    pop {r0-r2, r4-r5, lr}
    pop {r3, pc}             /* displaced 0x08115040 */
overflow:
    bkpt #1
.thumb_func
cutoff:
    push {r0}
    ldr r0, =0x10000004
    vstr s16, [r0]
    pop {r0}
    vcmpe.f32 s16, #0.0      /* displaced 0x081131a2 */
    b.w 0x081131a6
.thumb_func
run_blocks:
    push {r4, lr}
    mov r4, r0
2:  ldr r3, =0x08119845     /* real EXTI2 handler, eight samples */
    blx r3
    subs r4, #1
    bne 2b
    pop {r4, pc}
.ltorg
