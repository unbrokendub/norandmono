/*
 * np.ld -- places the patch blob past the end of the stock image.
 *
 * np_base must land exactly at the start: tools/patch.py reads the NPAT
 * header from the first bytes of the blob to find every entry point, so the
 * header cannot be allowed to float.
 */

ENTRY(np_base)

SECTIONS
{
    . = 0x08175200;

    .text :
    {
        KEEP(*(.text.np))          /* np_patch.S, header first */
        *(.text .text.*)           /* np_lfo.cpp              */
        *(.rodata .rodata.*)
        . = ALIGN(8);
    }

    /DISCARD/ :
    {
        *(.ARM.exidx*)
        *(.ARM.extab*)
        *(.comment)
        *(.note*)
        *(.debug*)
    }
}
