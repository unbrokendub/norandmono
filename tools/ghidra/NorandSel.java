/* NorandSel.java -- the single writer of the selected-parameter index.
 * @category Norand
 */
import ghidra.app.decompiler.*;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

public class NorandSel extends GhidraScript {
    private static final long[] SITES = { 0x0814A438L };
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
            println(String.format("%n=== SEL 0x%08X in %s ===", a,
                    f == null ? "?" : f.getName()));
            if (f == null) continue;
            DecompileResults r = di.decompileFunction(f, 900, monitor);
            println(r.decompileCompleted() ? r.getDecompiledFunction().getC()
                                           : "failed: " + r.getErrorMessage());
        }
        di.dispose();
    }
}
