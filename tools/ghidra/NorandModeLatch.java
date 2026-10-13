/* NorandModeLatch.java -- the two writers of the TYPE/WAVE mode latch.
 * @category Norand
 */
import ghidra.app.decompiler.*;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

public class NorandModeLatch extends GhidraScript {
    private static final long[] SITES = { 0x08151870L, 0x08151936L };
    @Override
    public void run() throws Exception {
        DecompInterface di = new DecompInterface();
        DecompileOptions o = new DecompileOptions(); o.setMaxWidth(100);
        di.setOptions(o); di.toggleCCode(true); di.toggleSyntaxTree(true);
        di.setSimplificationStyle("decompile");
        if (!di.openProgram(currentProgram)) { println("open failed"); return; }
        for (long a : SITES) {
            Address ad = toAddr(a);
            Function f = getFunctionContaining(ad);
            if (f == null) f = createFunction(ad, null);
            println(String.format("%n=== LATCH site 0x%08X in %s ===", a,
                    f == null ? "?" : f.getName()));
            if (f == null) continue;
            DecompileResults r = di.decompileFunction(f, 900, monitor);
            println(r.decompileCompleted() ? r.getDecompiledFunction().getC()
                                           : "failed: " + r.getErrorMessage());
        }
        di.dispose();
    }
}
