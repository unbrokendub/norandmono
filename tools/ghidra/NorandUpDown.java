/* NorandUpDown.java -- the function that drives the per-block DSP update.
 * @category Norand
 */
import ghidra.app.decompiler.*;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

public class NorandUpDown extends GhidraScript {
    private static final long[] FUNCS = { 0x081516B8L, 0x08151540L, 0x081483F0L, 0x08148438L };
    @Override
    public void run() throws Exception {
        DecompInterface di = new DecompInterface();
        DecompileOptions o = new DecompileOptions(); o.setMaxWidth(100);
        di.setOptions(o); di.toggleCCode(true); di.toggleSyntaxTree(true);
        di.setSimplificationStyle("decompile");
        if (!di.openProgram(currentProgram)) { println("open failed"); return; }
        for (long a : FUNCS) {
            Address ad = toAddr(a);
            Function f = getFunctionAt(ad);
            if (f == null) f = createFunction(ad, null);
            println(String.format("%n=== BLOCK 0x%08X ===", a));
            if (f == null) continue;
            DecompileResults r = di.decompileFunction(f, 900, monitor);
            println(r.decompileCompleted() ? r.getDecompiledFunction().getC()
                                           : "failed: " + r.getErrorMessage());
        }
        di.dispose();
    }
}
