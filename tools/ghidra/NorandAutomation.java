/* NorandAutomation.java -- per-step parameter automation playback.
 * @category Norand
 */
import ghidra.app.decompiler.*;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

public class NorandAutomation extends GhidraScript {
    private static final long[] FUNCS = {
        0x0811D834L,   // SequencerEvent<ParamAutomationData>::vfn2
        0x0811D43CL,   // ProjectEvent<PSeqData,ParamAutomationData>::vfn0
    };
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
            println(String.format("%n=== AUTO 0x%08X %s ===", a,
                    f == null ? "?" : f.getName()));
            if (f == null) continue;
            DecompileResults r = di.decompileFunction(f, 900, monitor);
            println(r.decompileCompleted() ? r.getDecompiledFunction().getC()
                                           : "failed: " + r.getErrorMessage());
        }
        di.dispose();
    }
}
