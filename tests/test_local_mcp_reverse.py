from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


class FakeFastMCP:
    def __init__(self, name: str) -> None:
        self.name = name

    def tool(self):
        return lambda function: function

    def resource(self, uri: str):
        del uri
        return lambda function: function


class FakePipe:
    def __init__(self, functions: list[dict[str, Any]], *, pseudocode: str = "", disassembly: str = "assembly") -> None:
        self.functions = functions
        self.pseudocode = pseudocode
        self.disassembly = disassembly

    def cmdj(self, command: str):
        if command == "aflj":
            return self.functions
        return []

    def cmd(self, command: str) -> str:
        if command.startswith("pdc "):
            return self.pseudocode
        if command.startswith("pdf "):
            return self.disassembly
        return ""


@pytest.fixture
def reverse_server(monkeypatch):
    fastmcp = ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = FakeFastMCP
    monkeypatch.setitem(sys.modules, "mcp", ModuleType("mcp"))
    monkeypatch.setitem(sys.modules, "mcp.server", ModuleType("mcp.server"))
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp)
    monkeypatch.setitem(sys.modules, "rzpipe", ModuleType("rzpipe"))

    common = ModuleType("common")
    common.WORKSPACE = Path("/workspace")
    common.audited = lambda server, tool, request, operation: operation()
    common.workspace_path = lambda value: Path(value)
    monkeypatch.setitem(sys.modules, "common", common)

    path = Path(__file__).parents[1] / "container" / "kali-codex" / "mcp" / "reverse_server.py"
    spec = importlib.util.spec_from_file_location("aurora_test_reverse_server", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolve_target_accepts_unique_rizin_alias_and_rejects_unknown(reverse_server) -> None:
    pipe = FakePipe([{"name": "dbg.main", "offset": 0x401135, "size": 38}])

    resolved = reverse_server._resolve_target(pipe, "main", require_function=True)

    assert resolved == {"requested": "main", "selector": "0x401135", "address": 0x401135, "function": "dbg.main"}
    with pytest.raises(ValueError, match="call list_functions first"):
        reverse_server._resolve_target(pipe, "Main.f_info", require_function=True)


def test_rizin_decompile_falls_back_to_function_disassembly(reverse_server) -> None:
    code, representation = reverse_server._rizin_decompile(FakePipe([], disassembly="function assembly"), "0x401135")

    assert code == "function assembly"
    assert representation == "disassembly"


def test_ghidra_diagnostic_prefers_actual_error_over_java_warning(reverse_server) -> None:
    diagnostic = reverse_server._ghidra_diagnostic(
        "ERROR REPORT SCRIPT ERROR: function not found: missing",
        "WARNING: sun.misc.Unsafe::staticFieldOffset will be removed",
    )

    assert "function not found: missing" in diagnostic
    assert "Unsafe" not in diagnostic


def test_ghidra_timeout_removes_partial_import_project(reverse_server, monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "sample"
    binary.write_bytes(b"sample")
    reverse_server.WORKSPACE = tmp_path
    digest = hashlib.sha256(b"sample").hexdigest()[:20]
    project_dir = tmp_path / "runtime" / "ghidra-projects"
    project_dir.mkdir(parents=True)

    def timed_out(*args, **kwargs):
        del args, kwargs
        project_dir.joinpath(f"{digest}.rep").mkdir()
        project_dir.joinpath(f"{digest}.gpr").touch()
        raise subprocess.TimeoutExpired("analyzeHeadless", 270, output="analysis timeout")

    monkeypatch.setattr(reverse_server.subprocess, "run", timed_out)

    with pytest.raises(RuntimeError, match="timed out after 270s"):
        reverse_server._ghidra_decompile(binary, "0x401135")
    assert not project_dir.joinpath(f"{digest}.rep").exists()
    assert not project_dir.joinpath(f"{digest}.gpr").exists()


def test_auto_decompile_falls_back_to_rizin(reverse_server, monkeypatch, tmp_path: Path) -> None:
    pipe = FakePipe([{"name": "sym.main", "offset": 0x401135, "size": 38}], disassembly="fallback assembly")
    reverse_server.SESSIONS["rev_test"] = {"pipe": pipe, "path": tmp_path / "sample"}
    monkeypatch.setattr(reverse_server.shutil, "which", lambda command: "/usr/bin/analyzeHeadless")
    monkeypatch.setattr(reverse_server, "_ghidra_decompile", lambda binary, function: (_ for _ in ()).throw(RuntimeError("script failed")))

    result = reverse_server.decompile_function("rev_test", "main", "auto")

    assert result["engine"] == "rizin"
    assert result["representation"] == "disassembly"
    assert result["code"] == "fallback assembly"
    assert result["fallback_reason"] == "script failed"


def test_find_string_xrefs_returns_only_matching_strings(reverse_server) -> None:
    class StringPipe(FakePipe):
        def cmdj(self, command: str):
            if command == "izj":
                return [{"string": "wrong", "vaddr": 0x402000}, {"string": "flag is here", "vaddr": 0x402100}]
            if command == "iij":
                return [{"name": "puts"}]
            if command == "axtj @ 0x402100":
                return [{"fcn_name": "sym.check", "from": 0x401200}]
            return super().cmdj(command)

    reverse_server.SESSIONS["rev_strings"] = {"pipe": StringPipe([]), "path": Path("/workspace/sample")}

    result = reverse_server.find_string_xrefs("rev_strings", "flag")
    imports = reverse_server.list_imports("rev_strings")

    assert result["total_matches"] == 1
    assert result["matches"][0]["xrefs"][0]["fcn_name"] == "sym.check"
    assert imports["imports"] == [{"name": "puts"}]


def test_triage_binary_bounds_probe_output(reverse_server, monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "sample"
    binary.write_bytes(b"sample")
    reverse_server.WORKSPACE = tmp_path

    monkeypatch.setattr(
        reverse_server.subprocess,
        "run",
        lambda command, **kwargs: type("Completed", (), {"stdout": "ok", "stderr": "", "returncode": 0})(),
    )

    result = reverse_server.triage_binary(str(binary))

    assert result["size"] == 6
    assert len(result["probes"]) == 6
    assert result["next"] == ["open_binary", "list_strings", "find_string_xrefs"]
