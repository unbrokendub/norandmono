/* NorandSlotEval.java -- decompile the per-slot evaluators.
 *
 * Slot 0 (VCO1Tune) and slot 4 (VCO2Tune) share 0x0811C464, so the two
 * oscillators run identical code; this dumps it, plus the other two classes
 * that read the X-Envelope destination flag at slot+0x26.
 *
 * @category Norand
 */

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileOptions;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

public class NorandSlotEval extends GhidraScript {
    private static final long[] FUNCS = {
        0x0811C464L,   // QuantizedLinAD  slots 0 (VCO1Tune), 4 (VCO2Tune), 9
        0x08110894L,   // ExpAD           slots 3, 7, 8 (X-Mod)
        0x0811108CL,   // LinAD           slots 1,2,5,6,10,11,12 (cutoff),17
    };

    @Override
    public void run() throws Exception {
        DecompInterface di = new DecompInterface();
        DecompileOptions o = new DecompileOptions();
        o.setMaxWidth(100);
        di.setOptions(o);
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
            println(String.format("%n=== SLOTEVAL 0x%08X %s ===", a,
                    f == null ? "?" : f.getName()));
            if (f == null) continue;
            DecompileResults res = di.decompileFunction(f, 900, monitor);
            if (res.decompileCompleted()) {
                println(res.getDecompiledFunction().getC());
            } else {
                println("decompile failed: " + res.getErrorMessage());
            }
        }
        di.dispose();
    }
}
