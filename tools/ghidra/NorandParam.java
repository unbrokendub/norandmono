/* NorandParam.java -- the discrete-parameter setter.
 *
 * A discrete CParam carries its integer as value = n / 65536.0f.  Only one
 * instruction in the image builds that form without a literal:
 *
 *   0x08115ACC   vcvt.f32.u32 s0, s0, #16
 *
 * so 0x081156C4 is where n is produced, and where the X-Mod type is bounded
 * at three values.
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

public class NorandParam extends GhidraScript {

    private static final long[] FUNCS = {
        0x081156C4L,   // builds n/65536 -- the discrete parameter setter
        0x08116C54L,   // CParam registration (project, param, index)
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
            ReferenceIterator it = currentProgram.getReferenceManager().getReferencesTo(addr);
            int n = 0;
            while (it.hasNext() && n < 30) {
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
