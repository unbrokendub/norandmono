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
    /* HL (p66), HM (p68): stock code our patches leave unreachable holds
     * functions of ours.  tools/patch.py replaces the marker below with one
     * output section per usable hole -- address, the input sections packed
     * into it, an ASSERT on its size -- and writes the sections there
     * (HOLES in patch.py).  What it does not place stays in .text. */
    /* @HOLES@ */

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
