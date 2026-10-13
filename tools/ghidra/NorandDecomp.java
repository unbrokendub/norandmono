/* NorandDecomp.java -- decompile the functions given as script arguments,
 * after clearing the bogus no-return flag Ghidra infers for operator new
 * (0x081591D4) and malloc (0x0815A0C0), which otherwise truncates every
 * function that allocates.  Usage: ... -postScript NorandDecomp.java 0x0812xxxx ...
 * @category Norand
 */
import ghidra.app.decompiler.*;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

public class NorandDecomp extends GhidraScript {
    @Override
    public void run() throws Exception {
        for (long a : new long[] { 0x081591D4L, 0x0815A0C0L, 0x08159074L }) {
            Function f = getFunctionAt(toAddr(a));
            if (f == null) f = createFunction(toAddr(a), null);
            if (f != null) f.setNoReturn(false);
        }
        DecompInterface di = new DecompInterface();
        DecompileOptions o = new DecompileOptions(); o.setMaxWidth(100);
        di.setOptions(o); di.toggleCCode(true); di.toggleSyntaxTree(true);
        di.setSimplificationStyle("decompile");
        if (!di.openProgram(currentProgram)) { println("open failed"); return; }
        for (String arg : getScriptArgs()) {
            Address ad = toAddr(Long.decode(arg));
            Function f = getFunctionAt(ad);
            if (f == null) f = createFunction(ad, null);
            println(String.format("%n=== BLOCK 0x%08X ===", ad.getOffset()));
            if (f == null) continue;
            DecompileResults r = di.decompileFunction(f, 900, monitor);
            println(r.decompileCompleted() ? r.getDecompiledFunction().getC()
                                           : "failed: " + r.getErrorMessage());
        }
        di.dispose();
    }
}
