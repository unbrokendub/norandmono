/* NorandPitch.java -- does project+0x2EDC contain the played note?
 *
 * project+0x2EDC is the value the audio-rate X-Mod uses as its pitch base,
 * built at 0x08112810 as 126 * clamp01(master) + fine.  `master` comes
 * through a one-pole smoother from project+0x16C, which has the CParam ramp
 * shape (value +0xC, step +0x18, count +0x1C) at base project+0x160.
 *
 * The note reaches the synth via 0x0811612C, which tail-calls
 * 0x0814770C(project+0x44, note, retrig).  project+0x44 + 0x11C = 0x160, so
 * if that object owns the ramped value, the note is in the chain and an FM
 * ratio can be taken straight off project+0x2EDC.  If it does not, the
 * modulator has to be placed from the note field instead.
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

public class NorandPitch extends GhidraScript {

    private static final long[] FUNCS = {
        0x0814770CL,   // where 0x0811612C sends the note
        0x08147768L,   // called next to it on the note-on path
        0x081473F4L,   // the constructor that zeroes 0x2EDC/0x2EE0/0x2F20
        0x08116D48L,   // called on note off / list change
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
