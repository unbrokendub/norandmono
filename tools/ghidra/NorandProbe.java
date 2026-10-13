/* NorandProbe.java -- headless Ghidra pass over build/firmware.elf.
 *
 * The questions grep could not answer:
 *
 *   1. What are project+0x2F20 and project+0x2F60, the two terms the
 *      audio-rate X-Mod adds to its table position?  Nothing in the image
 *      computes those addresses as a store target, which suggests they are
 *      loaded as part of the project blob rather than written by code --
 *      the decompiler settles it either way.
 *   2. Where does the currently sounding note live?  That is what a
 *      keytracking X-Mod mode needs.
 *   3. Is a semitone quantity normalised by 126 or by 127 in this firmware?
 *
 * The ELF only carries flash, so RAM is added as blocks first; without them
 * Ghidra shows bare constants instead of resolvable data references.
 *
 *   analyzeHeadless <proj-dir> norand -import build/firmware.elf \
 *       -scriptPath tools/ghidra -postScript NorandProbe.java
 *
 * @category Norand
 */

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileOptions;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;

public class NorandProbe extends GhidraScript {

    /* handlers worth reading in full */
    private static final long[] FUNCS = {
        0x0811BD48L,   // LFOTimeCParam::vfn2  -- the audio-rate branch
        0x0811BBA8L,   // LFOSpeedCParam::vfn2 -- same logic, second copy
        0x08116C64L,   // parameter value getter, project + index
        0x08117A44L,   // touches CNoteData and a 127.0 constant
        0x0811CC70L,   // per-slot tick handler (the one we replace)
        0x08117594L,   // applies a CNoteData event to the synth -- where the
                       // sounding note should end up, if it is stored at all
        0x0811B3FCL,   // MIDI note out, for the note-number units
    };

    /* fields the audio-rate position is built from */
    private static final long[] DATA = {
        0x20007710L,   // project + 0x2F20
        0x20007750L,   // project + 0x2F60
        0x200047F0L,   // project base
        0x2000AF78L,   // transport singleton
    };

    private static final long[][] RAM = {
        { 0x20000000L, 0x20000L, 0 },   // DTCM: .data, .bss, project, stack
        { 0x24000000L, 0x80000L, 1 },   // AXI SRAM
        { 0x30000000L, 0x50000L, 2 },   // D2 SRAM: the heap
        { 0x38000000L, 0x10000L, 3 },   // D3 SRAM
    };

    private static final String[] RAM_NAMES = { "DTCM", "AXI_SRAM", "D2_SRAM", "D3_SRAM" };

    @Override
    public void run() throws Exception {
        addRam();   /* no-op when NorandRam.java already ran as -preScript */

        println("");
        println("=== references to the audio-rate pitch fields ===");
        for (long a : DATA) {
            Address addr = toAddr(a);
            println(String.format("%n0x%08X:", a));
            ReferenceIterator it = currentProgram.getReferenceManager()
                    .getReferencesTo(addr);
            int n = 0;
            while (it.hasNext() && n < 40) {
                Reference r = it.next();
                Function f = getFunctionContaining(r.getFromAddress());
                println(String.format("   %s  %-10s  in %s",
                        r.getFromAddress(), r.getReferenceType(),
                        f == null ? "(no function)" : f.getName()));
                n++;
            }
            if (n == 0) {
                println("   no references -- consistent with a field that is only "
                        + "ever loaded as part of the project blob");
            }
        }

        DecompInterface di = new DecompInterface();
        DecompileOptions opts = new DecompileOptions();
        di.setOptions(opts);
        di.toggleCCode(true);
        di.toggleSyntaxTree(true);
        di.setSimplificationStyle("decompile");
        if (!di.openProgram(currentProgram)) {
            println("decompiler failed to open: " + di.getLastMessage());
            return;
        }

        for (long a : FUNCS) {
            Address addr = toAddr(a);
            Function f = getFunctionAt(addr);
            if (f == null) {
                f = createFunction(addr, null);
            }
            println("");
            println("================================================================");
            println(String.format("=== 0x%08X  %s", a, f == null ? "?" : f.getName()));
            println("================================================================");
            if (f == null) {
                println("   could not create a function here");
                continue;
            }
            DecompileResults res = di.decompileFunction(f, 180, monitor);
            if (res.decompileCompleted()) {
                println(res.getDecompiledFunction().getC());
            } else {
                println("   decompile failed: " + res.getErrorMessage());
            }
        }
        di.dispose();
    }

    private void addRam() throws Exception {
        Memory mem = currentProgram.getMemory();
        for (int i = 0; i < RAM.length; i++) {
            long start = RAM[i][0];
            long size = RAM[i][1];
            String name = RAM_NAMES[(int) RAM[i][2]];
            Address a = toAddr(start);
            MemoryBlock existing = mem.getBlock(a);
            if (existing != null) {
                println("block already covers " + name);
                continue;
            }
            MemoryBlock b = mem.createUninitializedBlock(name, a, size, false);
            b.setRead(true);
            b.setWrite(true);
            b.setExecute(false);
            println(String.format("added %-9s 0x%08X + 0x%X", name, start, size));
        }
    }
}
