/* NorandRam.java -- pre-analysis pass: give the program its RAM.
 *
 * The ELF only carries flash (0x08100000..0x081751D8).  Every access to a
 * global lands in unmapped space, so Ghidra records it as a bare constant
 * and builds no reference.  Asking "who writes project+0x2F20" against that
 * program returns "nobody" no matter what the code does -- which is exactly
 * the wrong answer to get by accident.
 *
 * This has to run as -preScript so the blocks exist before the analysers
 * do their work.  Adding them afterwards leaves the references uncomputed.
 *
 * @category Norand
 */

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;

public class NorandRam extends GhidraScript {

    private static final Object[][] BLOCKS = {
        { "DTCM",     0x20000000L, 0x20000L },  // .data, .bss, project, stack
        { "AXI_SRAM", 0x24000000L, 0x80000L },  // project/pattern working set
        { "D2_SRAM",  0x30000000L, 0x50000L },  // the heap and the QSPI buffer
        { "D3_SRAM",  0x38000000L, 0x10000L },
        { "PERIPH",   0x40000000L, 0x20000000L } // APB/AHB, so MMIO resolves too
    };

    @Override
    public void run() throws Exception {
        Memory mem = currentProgram.getMemory();
        for (Object[] b : BLOCKS) {
            String name = (String) b[0];
            long start = (Long) b[1];
            long size = (Long) b[2];
            Address a = toAddr(start);
            if (mem.getBlock(a) != null) {
                println("skip " + name + ": already mapped");
                continue;
            }
            MemoryBlock blk = mem.createUninitializedBlock(name, a, size, false);
            blk.setRead(true);
            blk.setWrite(true);
            blk.setExecute(false);
            blk.setVolatile(name.equals("PERIPH"));
            println(String.format("mapped %-9s 0x%08X + 0x%X", name, start, size));
        }
    }
}
