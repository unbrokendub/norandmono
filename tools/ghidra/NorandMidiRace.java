/* NorandMidiRace.java -- the MIDI producer and the queue it feeds.
 *
 * sub_0811A904 was flagged early on as possibly racing the consumer on the
 * read index. This dumps producer, consumer and the enqueue paths so the
 * claim can be settled rather than repeated.
 *
 * @category Norand
 */

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileOptions;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

public class NorandMidiRace extends GhidraScript {
    private static final long[] FUNCS = {
        0x0811A904L,   // MIDI producer, main loop
        0x0811A7A0L,   // DIN byte -> packet parser
        0x0811A738L,   // DIN RX enqueue
        0x0811A760L,   // USB packet enqueue
        0x0811B700L,   // main-loop packet pump
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
            println(String.format("%n=== MIDI 0x%08X %s ===", a,
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
