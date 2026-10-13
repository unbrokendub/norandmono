#!/usr/bin/env python3
"""
nvicprio.py -- recover the NVIC priority layout of the firmware.

Finds every call to __NVIC_SetPriority (0x08100F8C) and reads back the
immediates the compiler loaded into r0/r1/r2 just before it, which are the
IRQ number, the preemption priority and the sub priority.

The layout decides whether __malloc_lock can be changed from PRIMASK
(`cpsid i`, which stops every interrupt) to BASEPRI: raising BASEPRI only
helps if there are interrupts more urgent than the ones that allocate.
"""

import re
import sys

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_LITTLE_ENDIAN
from capstone.arm import ARM_OP_IMM, ARM_OP_REG

BASE = 0x08100000
SET_PRIORITY = 0x08100F8C

# STM32H743 vector table, the entries this firmware actually enables
IRQ_NAMES = {
    0: "WWDG", 6: "EXTI0", 7: "EXTI1", 8: "EXTI2", 9: "EXTI3", 10: "EXTI4",
    11: "DMA1_Stream0", 12: "DMA1_Stream1", 13: "DMA1_Stream2",
    14: "DMA1_Stream3", 15: "DMA1_Stream4", 16: "DMA1_Stream5",
    17: "DMA1_Stream6", 18: "ADC1_2", 23: "EXTI9_5", 24: "TIM1_BRK",
    25: "TIM1_UP", 27: "TIM1_CC", 28: "TIM2", 29: "TIM3", 30: "TIM4",
    31: "I2C1_EV", 35: "SPI1", 36: "SPI2", 37: "USART1", 38: "USART2",
    39: "USART3", 40: "EXTI15_10", 43: "TIM8_BRK_TIM12", 45: "TIM8_CC",
    46: "DMA1_Stream7", 47: "FMC", 49: "TIM5", 50: "SPI3", 51: "UART4",
    52: "UART5", 53: "TIM6_DAC", 54: "TIM7", 55: "DMA2_Stream0",
    56: "DMA2_Stream1", 57: "DMA2_Stream2", 58: "DMA2_Stream3",
    59: "DMA2_Stream4", 68: "DMA2_Stream5", 69: "DMA2_Stream6",
    70: "DMA2_Stream7", 71: "USART6", 77: "OTG_HS_EP1_OUT",
    78: "OTG_HS_EP1_IN", 79: "OTG_HS_WKUP", 80: "OTG_HS",
    92: "QUADSPI", 127: "SDMMC1",
}


def main():
    img = open("build/firmware.bin", "rb").read()
    asm = open("build/firmware.asm").read()
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)
    md.detail = True

    target = "%x" % SET_PRIORITY
    sites = [int(m.group(1), 16) for m in
             re.finditer(r"^\s*([0-9a-f]{7,8}):.*\bbl\s+" + target + r"\b",
                         asm, re.M)]
    print("calls to __NVIC_SetPriority: %d\n" % len(sites))

    rows = []
    for pc in sites:
        start = max(BASE, pc - 48)
        regs = {}
        for ins in md.disasm(img[start - BASE:pc - BASE], start):
            if ins.mnemonic.startswith("mov") and len(ins.operands) == 2 and \
                    ins.operands[0].type == ARM_OP_REG and \
                    ins.operands[1].type == ARM_OP_IMM:
                regs[ins.reg_name(ins.operands[0].reg)] = ins.operands[1].imm
        rows.append((pc, regs.get("r0"), regs.get("r1"), regs.get("r2")))

    print("%-12s %-6s %-18s %-9s %s" %
          ("call site", "IRQ", "peripheral", "preempt", "sub"))
    for pc, irq, pre, sub in sorted(rows, key=lambda r: (r[2] is None, r[2])):
        name = IRQ_NAMES.get(irq, "?") if irq is not None else "?"
        print("0x%08X   %-6s %-18s %-9s %s" %
              (pc, irq if irq is not None else "?", name,
               pre if pre is not None else "?",
               sub if sub is not None else "?"))

    known = [r for r in rows if r[2] is not None]
    if known:
        print("\npreemption priorities in use: %s  (lower = more urgent)" %
              sorted({r[2] for r in known}))


if __name__ == "__main__":
    sys.exit(main())
