/* NorandAudio.java -- decompile the audio block processor.
 *
 * 0x08112488 is the per-block DSP update.  Inside it, at 0x08112C70, sits a
 * loop over the 19 mod slots that recomputes the rate of every slot flagged
 * "audio rate" (owner+0x25) and calls setRate on it -- the same computation
 * the two CParam::vfn2 handlers do, but running every block.  That makes it,
 * not vfn2, the place where an audio-rate LFO's pitch is actually decided,
 * and therefore the place a keytracking mode has to hook.
 *
 * The static read is blocked by register allocation: the base position the
 * loop adds is held in s17 across ~1.8 KB of code with two candidate
 * definitions.  The decompiler resolves that; a linear disassembly does not.
 *
 * @category Norand
 */

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileOptions;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

public class NorandAudio extends GhidraScript {

    private static final long[] FUNCS = {
        0x08112488L,   // per-block DSP update; contains the audio-rate loop
        0x0811108CL,   // the getter the pitch terms come from
        0x08110894L,   // second getter used next to it
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
            DecompileResults res = di.decompileFunction(f, 600, monitor);
            if (res.decompileCompleted()) println(res.getDecompiledFunction().getC());
            else println("   decompile failed: " + res.getErrorMessage());
        }
        di.dispose();
    }
}
