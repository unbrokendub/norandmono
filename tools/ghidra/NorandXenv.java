/* NorandXenv.java -- focused decompile of the X-envelope routing path.
 *
 * @category Norand
 */

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileOptions;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

public class NorandXenv extends GhidraScript {
    private static final long[] FUNCS = {
        0x081156C4L,   // modulation evaluator
        0x0811BB50L,   // XEnvDestCParam::apply
        0x08112488L,   // per-block DSP update
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
            println(String.format("\n=== XENV 0x%08X %s ===", a,
                    f == null ? "?" : f.getName()));
            if (f == null) continue;
            DecompileResults res = di.decompileFunction(f, 600, monitor);
            if (res.decompileCompleted()) {
                println(res.getDecompiledFunction().getC());
            } else {
                println("decompile failed: " + res.getErrorMessage());
            }
        }
        di.dispose();
    }
}
