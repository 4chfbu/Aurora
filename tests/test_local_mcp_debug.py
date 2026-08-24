import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_debug_server(monkeypatch):
    fastmcp = ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = type("FakeFastMCP", (), {"__init__": lambda self, name: None, "tool": lambda self: (lambda function: function)})
    monkeypatch.setitem(sys.modules, "mcp", ModuleType("mcp"))
    monkeypatch.setitem(sys.modules, "mcp.server", ModuleType("mcp.server"))
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp)
    gdbmi = ModuleType("pygdbmi.gdbcontroller")
    gdbmi.GdbController = object
    monkeypatch.setitem(sys.modules, "pygdbmi", ModuleType("pygdbmi"))
    monkeypatch.setitem(sys.modules, "pygdbmi.gdbcontroller", gdbmi)
    common = ModuleType("common")
    common.audited = lambda server, tool, request, operation: operation()
    common.workspace_path = lambda value: Path(value)
    monkeypatch.setitem(sys.modules, "common", common)
    path = Path(__file__).parents[1] / "container" / "kali-codex" / "mcp" / "debug_server.py"
    spec = importlib.util.spec_from_file_location("aurora_test_debug_server", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pwndbg_command_is_allowlisted(monkeypatch) -> None:
    debug_server = _load_debug_server(monkeypatch)
    assert debug_server._validate_pwndbg_command("telescope $rsp 8") == "telescope $rsp 8"
    with pytest.raises(ValueError, match="not allowlisted"):
        debug_server._validate_pwndbg_command("shell id")
    with pytest.raises(ValueError, match="invalid"):
        debug_server._validate_pwndbg_command("search foo; shell id")


def test_debugger_command_prefers_pwndbg(monkeypatch) -> None:
    debug_server = _load_debug_server(monkeypatch)
    monkeypatch.setattr(debug_server.shutil, "which", lambda command: "/usr/bin/pwndbg" if command == "pwndbg" else None)

    assert debug_server._debugger_command() == ["/usr/bin/pwndbg", "--quiet", "--interpreter=mi3"]


def test_mi_write_waits_for_its_token(monkeypatch) -> None:
    debug_server = _load_debug_server(monkeypatch)

    class FakeController:
        def __init__(self) -> None:
            self.token = 0
            self.reads = 0

        def write(self, command, **kwargs) -> None:
            self.token = int(command.split("-", 1)[0])
            assert kwargs["read_response"] is False

        def get_gdb_response(self, **kwargs):
            self.reads += 1
            if self.reads == 1:
                return [{"type": "console", "message": None, "payload": "pwndbg startup"}]
            return [{"type": "result", "message": "done", "payload": None, "token": self.token}]

    controller = FakeController()
    debug_server.SESSIONS["dbg_test"] = controller

    result = debug_server._write("dbg_test", "-gdb-version")

    assert result["completed"] is True
    assert result["timed_out"] is False
    assert controller.reads == 2


def test_pwndbg_response_is_bounded(monkeypatch) -> None:
    debug_server = _load_debug_server(monkeypatch)
    bounded = debug_server._bound_pwndbg_value([{"payload": "x" * 5000}] * 200)

    assert len(bounded) == debug_server.PWNDBG_RESPONSE_ROWS
    assert len(bounded[0]["payload"]) == debug_server.PWNDBG_RESPONSE_TEXT


def test_pwndbg_command_audit_request_is_serializable(monkeypatch) -> None:
    debug_server = _load_debug_server(monkeypatch)
    monkeypatch.setattr(
        debug_server,
        "audited",
        lambda server, tool, request, operation: (json.dumps(request), operation())[1],
    )
    monkeypatch.setattr(
        debug_server,
        "_write",
        lambda session_id, command: {"session_id": session_id, "command": command, "responses": [], "errors": []},
    )

    result = debug_server.pwndbg_command("dbg_test", "checksec")

    assert result["command"].endswith("checksec")
