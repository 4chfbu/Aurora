import re
import shlex
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
from pygdbmi.gdbcontroller import GdbController

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import audited, workspace_path  # noqa: E402


mcp = FastMCP("aurora_debug")
SESSIONS: dict[str, GdbController] = {}
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ALLOWED_ENV = {"LD_LIBRARY_PATH", "LD_PRELOAD", "PATH", "LANG", "LC_ALL", "TERM"}


def _controller(session_id: str) -> GdbController:
    if session_id not in SESSIONS:
        raise ValueError(f"unknown debug session: {session_id}")
    return SESSIONS[session_id]


def _write(session_id: str, command: str, timeout: float = 10.0) -> dict[str, Any]:
    responses = _controller(session_id).write(command, timeout_sec=timeout, raise_error_on_timeout=False)
    errors = [row for row in responses if row.get("message") == "error"]
    return {"session_id": session_id, "command": command, "responses": responses, "errors": errors}


@mcp.tool()
def start_session(program: str, args: list[str] | None = None, cwd: str = "/workspace", env: dict[str, str] | None = None) -> dict[str, Any]:
    """Start a reusable GDB/MI session for a program inside /workspace."""
    def operation() -> dict[str, Any]:
        executable = workspace_path(program)
        working_dir = workspace_path(cwd)
        if not executable.is_file() or not working_dir.is_dir():
            raise ValueError("program must be a file and cwd must be a directory")
        for name in (env or {}):
            if name not in ALLOWED_ENV or not ENV_NAME.fullmatch(name):
                raise ValueError(f"environment variable is not allowed: {name}")
        controller = GdbController(command=["gdb", "--quiet", "--interpreter=mi3"])
        session_id = f"dbg_{uuid4().hex[:12]}"
        SESSIONS[session_id] = controller
        _write(session_id, f"-file-exec-and-symbols {shlex.quote(str(executable))}")
        _write(session_id, f"-environment-cd {shlex.quote(str(working_dir))}")
        if args:
            _write(session_id, "-exec-arguments " + " ".join(shlex.quote(str(item)) for item in args))
        for name, value in (env or {}).items():
            _write(session_id, f"-gdb-set environment {name}={shlex.quote(str(value))}")
        return {"session_id": session_id, "program": str(executable), "cwd": str(working_dir), "status": "ready"}
    return audited("aurora_debug", "start_session", {"program": program, "args": args or [], "cwd": cwd, "env": env or {}}, operation)


@mcp.tool()
def set_breakpoint(session_id: str, location: str) -> dict[str, Any]:
    """Set a source, symbol, or address breakpoint."""
    return audited("aurora_debug", "set_breakpoint", locals(), lambda: _write(session_id, f"-break-insert {shlex.quote(location)}"))


@mcp.tool()
def run(session_id: str) -> dict[str, Any]:
    """Run the inferior until it exits or stops."""
    return audited("aurora_debug", "run", locals(), lambda: _write(session_id, "-exec-run", 30.0))


@mcp.tool()
def continue_execution(session_id: str) -> dict[str, Any]:
    """Continue the stopped inferior."""
    return audited("aurora_debug", "continue_execution", locals(), lambda: _write(session_id, "-exec-continue", 30.0))


@mcp.tool()
def step_instruction(session_id: str) -> dict[str, Any]:
    """Step into one machine instruction."""
    return audited("aurora_debug", "step_instruction", locals(), lambda: _write(session_id, "-exec-step-instruction"))


@mcp.tool()
def next_instruction(session_id: str) -> dict[str, Any]:
    """Step over one machine instruction."""
    return audited("aurora_debug", "next_instruction", locals(), lambda: _write(session_id, "-exec-next-instruction"))


@mcp.tool()
def read_registers(session_id: str, names: list[str] | None = None) -> dict[str, Any]:
    """Read named registers, or all natural-format registers when names is empty."""
    def operation() -> dict[str, Any]:
        if names:
            values = {name: _write(session_id, f'-data-evaluate-expression "${name}"') for name in names[:64]}
            return {"session_id": session_id, "registers": values}
        return _write(session_id, "-data-list-register-values x")
    return audited("aurora_debug", "read_registers", {"session_id": session_id, "names": names or []}, operation)


@mcp.tool()
def read_memory(session_id: str, address: str, length: int = 64, format: str = "hex") -> dict[str, Any]:
    """Read a bounded memory range from the stopped inferior."""
    del format
    size = max(1, min(length, 65536))
    return audited("aurora_debug", "read_memory", {"session_id": session_id, "address": address, "length": size}, lambda: _write(session_id, f"-data-read-memory-bytes {shlex.quote(address)} {size}"))


@mcp.tool()
def evaluate(session_id: str, expression: str) -> dict[str, Any]:
    """Evaluate a GDB expression in the current frame."""
    return audited("aurora_debug", "evaluate", locals(), lambda: _write(session_id, f"-data-evaluate-expression {shlex.quote(expression)}"))


@mcp.tool()
def backtrace(session_id: str, max_frames: int = 64) -> dict[str, Any]:
    """Return a bounded stack trace."""
    limit = max(1, min(max_frames, 256))
    return audited("aurora_debug", "backtrace", locals(), lambda: _write(session_id, f"-stack-list-frames 0 {limit - 1}"))


@mcp.tool()
def disassemble(session_id: str, location: str = "$pc") -> dict[str, Any]:
    """Disassemble a symbol, address, or the current program counter."""
    return audited("aurora_debug", "disassemble", locals(), lambda: _write(session_id, f"-interpreter-exec console {shlex.quote('disassemble /r ' + location)}"))


@mcp.tool()
def stop_session(session_id: str) -> dict[str, Any]:
    """Terminate the inferior and close the GDB session."""
    def operation() -> dict[str, Any]:
        controller = _controller(session_id)
        controller.exit()
        del SESSIONS[session_id]
        return {"session_id": session_id, "status": "closed"}
    return audited("aurora_debug", "stop_session", {"session_id": session_id}, operation)


if __name__ == "__main__":
    mcp.run(transport="stdio")
