/* NorandDac.java -- the CV/DAC output chain: timer, SPI3, DMA.
 *
 * Mono's signal path is analog; the digital side computes control voltages
 * and pushes them through DACs. This dumps the clocking so the CV update
 * rate can be read off rather than guessed.
 *
 * @category Norand
 */

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileOptions;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

public class NorandDac extends GhidraScript {
    private static final long[] FUNCS = {
        0x0810F9C8L,   // timers (TIM1/3/5/8) + GPIO init
        0x0810F2F0L,   // SPI3 init
        0x0810F208L,   // DMA1_Stream0 IRQ
        0x08119780L,   // SPI3 / TIM1 user
        0x0811992CL,   // TIM3 user
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
            println(String.format("%n=== DAC 0x%08X %s ===", a,
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
