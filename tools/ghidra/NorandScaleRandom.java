/* Read-only focused decompile. Optional args are hex function addresses. */
import ghidra.app.decompiler.*;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.FlowOverride;
import ghidra.program.model.address.AddressSet;
import java.io.PrintWriter;

public class NorandScaleRandom extends GhidraScript {
    public void run() throws Exception {
        // Existing analysis incorrectly inferred operator new never returns.
        // Change only this read-only session; do not persist the project.
        Function alloc = getFunctionAt(toAddr(0x081591D4L));
        if (alloc != null) alloc.setNoReturn(false);
        for (var ref : getReferencesTo(toAddr(0x081591D4L))) {
            Instruction ins = getInstructionAt(ref.getFromAddress());
            if (ins == null) continue;
            ins.setFlowOverride(FlowOverride.NONE);
            ins.clearFallThroughOverride();
        }
        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);
        String[] args = getScriptArgs();
        if (args.length == 0) args = new String[]{
            "08143B84", "08144E10", "08145204", "081315D0",
            "081329EC", "081217A4", "081434DC", "08143800",
            "08126FA0", "08129568", "08125DE8", "08155F1C"};
        try (PrintWriter out = new PrintWriter("build/scale-random.txt")) {
            for (String arg : args) {
                long a = Long.parseLong(arg.replace("0x", ""), 16);
                Function f = getFunctionAt(toAddr(a));
                if (f == null) f = getFunctionContaining(toAddr(a));
                out.printf("\n=== %08x %s ===\n", a,
                           f == null ? "missing" : f.getName());
                if (f == null) continue;
                Function next = getFunctionAfter(f.getEntryPoint());
                if (next != null) {
                    disassemble(f.getEntryPoint());
                    f.setBody(new AddressSet(f.getEntryPoint(), next.getEntryPoint().subtract(1)));
                }
                DecompileResults r = di.decompileFunction(f, 60, monitor);
                out.println(r.decompileCompleted()
                            ? r.getDecompiledFunction().getC()
                            : r.getErrorMessage());
            }
        }
        di.dispose();
        println("Saved build/scale-random.txt");
    }
}
