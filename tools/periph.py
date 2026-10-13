#!/usr/bin/env python3
"""
periph.py -- list the STM32H7 peripherals an image touches.

Scans every 32 bit word of the image (literal pools are where Cortex-M code
keeps its MMIO base addresses) and maps them onto the STM32H743/H750 memory
map, then reports which functions reference what.
"""

import bisect
import json
import struct
import sys

MAP = [
    (0x40000000, 0x400003FF, "TIM2"),
    (0x40000400, 0x400007FF, "TIM3"),
    (0x40000800, 0x40000BFF, "TIM4"),
    (0x40000C00, 0x40000FFF, "TIM5"),
    (0x40001000, 0x400013FF, "TIM6"),
    (0x40001400, 0x400017FF, "TIM7"),
    (0x40001800, 0x40001BFF, "TIM12"),
    (0x40001C00, 0x40001FFF, "TIM13"),
    (0x40002000, 0x400023FF, "TIM14"),
    (0x40002800, 0x40002BFF, "WWDG"),
    (0x40003800, 0x40003BFF, "SPI2/I2S2"),
    (0x40003C00, 0x40003FFF, "SPI3/I2S3"),
    (0x40004400, 0x400047FF, "USART2"),
    (0x40004800, 0x40004BFF, "USART3"),
    (0x40004C00, 0x40004FFF, "UART4"),
    (0x40005000, 0x400053FF, "UART5"),
    (0x40005400, 0x400057FF, "I2C1"),
    (0x40005800, 0x40005BFF, "I2C2"),
    (0x40005C00, 0x40005FFF, "I2C3"),
    (0x40006400, 0x400067FF, "HDMI-CEC"),
    (0x40006C00, 0x40006FFF, "DAC1"),
    (0x40007800, 0x40007BFF, "UART7"),
    (0x40007C00, 0x40007FFF, "UART8"),
    (0x40010000, 0x400103FF, "TIM1"),
    (0x40010400, 0x400107FF, "TIM8"),
    (0x40011000, 0x400113FF, "USART1"),
    (0x40011400, 0x400117FF, "USART6"),
    (0x40012000, 0x400123FF, "SPI1/I2S1"),
    (0x40013400, 0x400137FF, "SPI4"),
    (0x40014000, 0x400143FF, "TIM15"),
    (0x40014400, 0x400147FF, "TIM16"),
    (0x40014800, 0x40014BFF, "TIM17"),
    (0x40015000, 0x400153FF, "SPI5"),
    (0x40015800, 0x40015BFF, "SAI1"),
    (0x40016000, 0x400163FF, "SAI2"),
    (0x40020000, 0x400203FF, "DMA1"),
    (0x40020400, 0x400207FF, "DMA2"),
    (0x40022000, 0x400223FF, "ADC1"),
    (0x40022100, 0x400224FF, "ADC2"),
    (0x40040000, 0x4007FFFF, "USB1 OTG HS"),
    (0x40080000, 0x400BFFFF, "USB2 OTG FS"),
    (0x48022400, 0x480227FF, "DMA2D"),
    (0x51000000, 0x5100FFFF, "GPU2D/LTDC"),
    (0x52000000, 0x52000FFF, "MDMA"),
    (0x52001000, 0x52001FFF, "DMA2D"),
    (0x52002000, 0x52002FFF, "FLASH ctl"),
    (0x52004000, 0x52004FFF, "JPEG"),
    (0x52005000, 0x52005FFF, "QUADSPI"),
    (0x52006000, 0x52006FFF, "SDMMC1"),
    (0x52007000, 0x52007FFF, "SDMMC1"),
    (0x58000000, 0x58000FFF, "DCMI"),
    (0x58001000, 0x58001FFF, "CRYP"),
    (0x58020000, 0x580203FF, "GPIOA"),
    (0x58020400, 0x580207FF, "GPIOB"),
    (0x58020800, 0x58020BFF, "GPIOC"),
    (0x58020C00, 0x58020FFF, "GPIOD"),
    (0x58021000, 0x580213FF, "GPIOE"),
    (0x58021400, 0x580217FF, "GPIOF"),
    (0x58021800, 0x58021BFF, "GPIOG"),
    (0x58021C00, 0x58021FFF, "GPIOH"),
    (0x58022000, 0x580223FF, "GPIOI"),
    (0x58022400, 0x580227FF, "GPIOJ"),
    (0x58022800, 0x58022BFF, "GPIOK"),
    (0x58024400, 0x580247FF, "RCC"),
    (0x58024800, 0x58024BFF, "PWR"),
    (0x58024C00, 0x58024FFF, "CRC"),
    (0x58026000, 0x580263FF, "ADC3"),
    (0x58000C00, 0x58000FFF, "LPUART1"),
    (0x58003C00, 0x58003FFF, "COMP"),
    (0x58004400, 0x580047FF, "SPI6"),
    (0x58005400, 0x580057FF, "I2C4"),
    (0x58006400, 0x580067FF, "LPTIM2"),
    (0x5C001000, 0x5C001FFF, "DBGMCU"),
    (0xE000E000, 0xE000EFFF, "SCS (NVIC/SysTick/SCB)"),
    (0x24000000, 0x2407FFFF, "AXI SRAM (D1)"),
    (0x30000000, 0x3004FFFF, "SRAM1/2/3 (D2)"),
    (0x38000000, 0x3801FFFF, "SRAM4 (D3)"),
    (0x38800000, 0x38800FFF, "backup SRAM"),
    (0x20000000, 0x2001FFFF, "DTCM"),
    (0x00000000, 0x0000FFFF, "ITCM"),
    (0x08000000, 0x080FFFFF, "flash bank 1"),
    (0x08100000, 0x081FFFFF, "flash bank 2"),
    (0x90000000, 0x9FFFFFFF, "QSPI mapped"),
]


def lookup(v):
    for lo, hi, name in MAP:
        if lo <= v <= hi:
            return name, lo
    return None, None


def main(argv):
    which = argv[1] if len(argv) > 1 else "firmware"
    sc = json.load(open("build/%s.symbols.json" % which))
    base = sc["base"]
    funcs = sc["functions"]
    data = open("build/%s.bin" % which, "rb").read()

    hits = {}
    for off in range(0, len(data) - 3, 4):
        w = struct.unpack_from("<I", data, off)[0]
        name, lo = lookup(w)
        if not name or name.startswith(("flash", "ITCM")):
            continue
        i = bisect.bisect_right(funcs, base + off) - 1
        fn = funcs[i] if i >= 0 else 0
        hits.setdefault(name, {}).setdefault(fn, set()).add(w)

    print("%-24s %6s  %s" % ("PERIPHERAL", "REFS", "referencing functions"))
    for name in sorted(hits, key=lambda n: -sum(len(v) for v in hits[n].values())):
        fns = hits[name]
        total = sum(len(v) for v in fns.values())
        sample = ", ".join("0x%08X" % f for f in sorted(fns)[:6])
        print("%-24s %6d  %s%s" % (name, total, sample,
                                   " ..." if len(fns) > 6 else ""))


if __name__ == "__main__":
    main(sys.argv)
