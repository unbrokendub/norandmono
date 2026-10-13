/* NorandXmod.java -- the X-Mod type parameter.
 *
 * Each discrete CParam stores its integer as value = n / 65536.0f; the three
 * handlers that decode it (0x0811BB50, 0x0811BB80, 0x0811BD48) all do
 * (uint)(value * 65536.0f) & 0xff.  Adding a fourth X-Mod type means finding
 * where n is produced and where it is bounded at 2.
 *
 * 0x0811CD08 holds the vptrs of every LFO CParam class in one literal pool,
 * so it is the factory that builds them and the place any per-class range
 * would be set.
 *
 * @category Norand
 */

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileOptions;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;

public class NorandXmod extends GhidraScript {

    private static final long[] FUNCS = {
        0x0811CD08L,   // CParam factory: every LFO CParam vptr sits in its pool
        0x0811BB4CL,   // MedianValue/LFOLevel handler, for the plain case
        0x08116CF4L,   // note applied to the synth
        0x081170E8L,   // note-on path
    };

    @Override
    public void run() throws Exception {
        DecompInterface di = new DecompInterface();
        di.setOptions(new DecompileOptions());
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
            if (f == null) f = createFunction(addr, null);
            println("");
            println("================================================================");
            println(String.format("=== 0x%08X  %s", a, f == null ? "?" : f.getName()));
            println("================================================================");
            if (f == null) { println("   no function here"); continue; }
            println("callers:");
            ReferenceIterator it = currentProgram.getReferenceManager()
                    .getReferencesTo(addr);
            int n = 0;
            while (it.hasNext() && n < 20) {
                Reference r = it.next();
                Function c = getFunctionContaining(r.getFromAddress());
                println("   " + r.getFromAddress() + "  " + r.getReferenceType()
                        + "  in " + (c == null ? "(none)" : c.getName()));
                n++;
            }
            DecompileResults res = di.decompileFunction(f, 600, monitor);
            if (res.decompileCompleted()) println(res.getDecompiledFunction().getC());
            else println("   decompile failed: " + res.getErrorMessage());
        }
        di.dispose();
    }
}
