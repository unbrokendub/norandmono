/* Focused, read-only UI chord investigation. Run with -noanalysis -readOnly. */
import ghidra.app.decompiler.*;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;
import java.io.PrintWriter;

public class NorandUiDispatch extends GhidraScript {
    public void run() throws Exception {
        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);
        long[] sites = {0x0814E818L, 0x0814A7B0L, 0x08155C7CL,
                        0x0814A2B0L, 0x0814A2E0L, 0x0814A314L,
                        0x0814A33CL, 0x08147B4CL, 0x08147B54L,
                        0x0810F5D0L, 0x08157324L, 0x08149A70L};
        try (PrintWriter out = new PrintWriter("build/ui-dispatch.txt")) {
            for (long a : sites) {
                Function f = getFunctionAt(toAddr(a));
                out.printf("\n=== %08x %s ===\n", a,
                           f == null ? "missing" : f.getName());
                if (f == null) continue;
                DecompileResults r = di.decompileFunction(f, 180, monitor);
                out.println(r.decompileCompleted()
                            ? r.getDecompiledFunction().getC()
                            : r.getErrorMessage());
            }
        }
        di.dispose();
        println("Saved build/ui-dispatch.txt");
    }
}
