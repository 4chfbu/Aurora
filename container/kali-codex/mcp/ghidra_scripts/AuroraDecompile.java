// Decompile one function selected by name or address and write plain C output.
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

public class AuroraDecompile extends GhidraScript {
    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length != 2) {
            throw new IllegalArgumentException("expected function selector and output path");
        }
        Function function = null;
        try {
            Address address = currentProgram.getAddressFactory().getDefaultAddressSpace().getAddress(args[0]);
            function = currentProgram.getFunctionManager().getFunctionContaining(address);
        } catch (Exception ignored) {
            for (Function candidate : currentProgram.getFunctionManager().getFunctions(true)) {
                if (candidate.getName().equals(args[0])) {
                    function = candidate;
                    break;
                }
            }
        }
        if (function == null) {
            throw new IllegalArgumentException("function not found: " + args[0]);
        }
        DecompInterface decompiler = new DecompInterface();
        decompiler.openProgram(currentProgram);
        DecompileResults result = decompiler.decompileFunction(function, 120, monitor);
        if (!result.decompileCompleted()) {
            throw new IllegalStateException(result.getErrorMessage());
        }
        String code = result.getDecompiledFunction().getC();
        Files.writeString(new File(args[1]).toPath(), code, StandardCharsets.UTF_8);
        decompiler.dispose();
    }
}
