/* NorandTiming.java -- the sequencer clock and the MIDI path.
 *
 * Targets used to recover the DIN/USB realtime-message path and transport
 * state transitions.  In particular, 0x08118B00 is the accepted F8 edge
 * and contains the otherwise easily missed call to the modulation walker.
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

public class NorandTiming extends GhidraScript {

    private static final long[] FUNCS = {
        0x08118570L,   // MIDI Start/reset transport state
        0x081184F8L,   // MIDI Start/Continue run action; zeroes local phase
        0x08118624L,   // MIDI Stop action
        0x0811A7A0L,   // raw MIDI parser, including realtime bytes
        0x0811B700L,   // parsed MIDI event consumer
        0x0811B2CCL,   // F8/FA/FB/FC dispatch
        0x08118B00L,   // accepted MIDI F8 edge
        0x08118670L,   // transport start/stop action
        0x08118A14L,   // internal tempo -> transport rate
        0x08116CD4L,   // all-slot modulation walker
        0x0811CC70L,   // per-slot sync handler
    };

    @Override
    public void run() throws Exception {
        DecompInterface di = new DecompInterface();
        di.setOptions(new DecompileOptions());
        di.toggleCCode(true);
        di.toggleSyntaxTree(true);
        di.setSimplificationStyle("decompile");
        if (!di.openProgram(currentProgram)) {
            println("decompiler failed: " + di.getLastMessage());
            return;
        }
        for (long a : FUNCS) {
            Address addr = toAddr(a);
            Function f = getFunctionContaining(addr);
            if (f == null) f = createFunction(addr, null);
            println("");
            println("================================================================");
            println(String.format("=== 0x%08X  %s", a, f == null ? "?" : f.getName()));
            println("================================================================");
            if (f == null) { println("   no function"); continue; }
            println("callers:");
            ReferenceIterator it = currentProgram.getReferenceManager()
                    .getReferencesTo(f.getEntryPoint());
            int n = 0;
            while (it.hasNext() && n < 15) {
                Reference r = it.next();
                Function c = getFunctionContaining(r.getFromAddress());
                println("   " + r.getFromAddress() + "  " + r.getReferenceType()
                        + "  in " + (c == null ? "(none)" : c.getName()));
                n++;
            }
            DecompileResults res = di.decompileFunction(f, 300, monitor);
            if (res.decompileCompleted()) println(res.getDecompiledFunction().getC());
            else println("   decompile failed: " + res.getErrorMessage());
        }
        di.dispose();
    }
}
