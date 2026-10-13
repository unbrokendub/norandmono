/* NorandListing.java -- dump every instruction of one function body, in
 * address order, from Ghidra's flow-derived listing rather than a linear
 * decode (which desynchronises at literal pools).  Arguments: function
 * address, then optional substrings to filter on.
 * @category Norand
 */
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.*;
import ghidra.program.model.listing.*;

public class NorandListing extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        Address fa = toAddr(Long.decode(args[0]));
        Function f = getFunctionAt(fa);
        if (f == null) f = createFunction(fa, null);
        if (f == null) { println("no function at " + fa); return; }
        AddressSetView body = f.getBody();
        disassemble(fa);
        InstructionIterator it = currentProgram.getListing().getInstructions(body, true);
        int n = 0;
        while (it.hasNext()) {
            Instruction ins = it.next();
            String s = ins.toString();
            boolean keep = args.length == 1;
            for (int i = 1; i < args.length; i++) if (s.contains(args[i])) keep = true;
            if (keep) println(String.format("%08X  %s", ins.getAddress().getOffset(), s));
            n++;
        }
        println(String.format("body %s, %d instructions", body, n));
    }
}
