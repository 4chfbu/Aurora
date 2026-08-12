import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import rzpipe
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import WORKSPACE, audited, workspace_path  # noqa: E402


mcp = FastMCP("aurora_reverse")
SESSIONS: dict[str, dict[str, Any]] = {}
NUMERIC_SELECTOR = re.compile(r"^(?:0[xX][0-9a-fA-F]+|[0-9]+)$")
RIZIN_NAME_PREFIXES = ("sym.", "fcn.", "loc.", "dbg.", "sub.")
GHIDRA_ANALYSIS_TIMEOUT_SECONDS = 240
GHIDRA_PROCESS_TIMEOUT_SECONDS = 270


def _session(session_id: str) -> dict[str, Any]:
    if session_id not in SESSIONS:
        raise ValueError(f"unknown reverse session: {session_id}")
    return SESSIONS[session_id]


def _target(value: str) -> str:
    if not value or len(value) > 500 or any(character in value for character in "\n\r"):
        raise ValueError("invalid address or function selector")
    return value


def _functions(pipe: Any) -> list[dict[str, Any]]:
    return [item for item in (pipe.cmdj("aflj") or []) if isinstance(item, dict)]


def _function_identity(function: dict[str, Any]) -> tuple[str, int, int]:
    name = str(function.get("name") or function.get("realname") or "")
    try:
        address = int(function.get("offset"))
        size = max(1, int(function.get("size") or 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("Rizin returned an invalid function record") from exc
    return name, address, size


def _resolve_target(pipe: Any, value: str, *, require_function: bool = False) -> dict[str, Any]:
    requested = _target(value).strip()
    functions = _functions(pipe)
    if NUMERIC_SELECTOR.fullmatch(requested):
        address = int(requested, 16 if requested.lower().startswith("0x") else 10)
        for function in functions:
            name, start, size = _function_identity(function)
            if start <= address < start + size:
                return {"requested": requested, "selector": hex(address), "address": address, "function": name}
        if require_function:
            raise ValueError(f"unknown function address: {requested}; call list_functions first")
        return {"requested": requested, "selector": hex(address), "address": address, "function": None}

    aliases: list[dict[str, Any]] = []
    for function in functions:
        names = {str(function.get(key) or "") for key in ("name", "realname", "demname")}
        if requested in names:
            name, address, _ = _function_identity(function)
            return {"requested": requested, "selector": hex(address), "address": address, "function": name}
        if any(_selector_alias(name) == requested for name in names):
            aliases.append(function)
    if len(aliases) == 1:
        name, address, _ = _function_identity(aliases[0])
        return {"requested": requested, "selector": hex(address), "address": address, "function": name}
    if aliases:
        names = ", ".join(sorted(_function_identity(function)[0] for function in aliases[:10]))
        raise ValueError(f"ambiguous function selector: {requested}; matches {names}")
    raise ValueError(f"unknown function selector: {requested}; call list_functions first")


def _rizin_decompile(pipe: Any, selector: str) -> tuple[str, str]:
    pseudocode = pipe.cmd(f"pdc @ {selector}").strip()
    if pseudocode and "does not exist" not in pseudocode.lower():
        return pseudocode, "pseudocode"
    disassembly = pipe.cmd(f"pdf @ {selector}").strip()
    if not disassembly:
        raise RuntimeError(f"Rizin returned no pseudocode or function disassembly for {selector}")
    return disassembly, "disassembly"


def _selector_alias(name: str) -> str:
    for prefix in RIZIN_NAME_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


@mcp.tool()
def open_binary(path: str, analyze: bool = True) -> dict[str, Any]:
    """Open a workspace binary in a reusable Rizin analysis session."""
    def operation() -> dict[str, Any]:
        binary = workspace_path(path)
        if not binary.is_file():
            raise ValueError("binary path must be a file")
        pipe = rzpipe.open(str(binary), flags=["-2"])
        if analyze:
            pipe.cmd("aaa")
        session_id = f"rev_{uuid4().hex[:12]}"
        info = pipe.cmdj("ij") or {}
        SESSIONS[session_id] = {"pipe": pipe, "path": binary}
        return {"session_id": session_id, "path": str(binary), "info": info, "ghidra_available": shutil.which("analyzeHeadless") is not None}
    return audited("aurora_reverse", "open_binary", {"path": path, "analyze": analyze}, operation)


@mcp.tool()
def list_functions(session_id: str, query: str = "", offset: int = 0, limit: int = 100) -> dict[str, Any]:
    """List analyzed functions, optionally filtering by name."""
    def operation() -> dict[str, Any]:
        functions = _functions(_session(session_id)["pipe"])
        if query:
            functions = [item for item in functions if query.lower() in str(item.get("name", "")).lower()]
        start = max(0, offset)
        rows = functions[start:start + max(1, min(limit, 500))]
        return {"session_id": session_id, "total": len(functions), "functions": rows}
    return audited("aurora_reverse", "list_functions", {"session_id": session_id, "query": query, "offset": offset, "limit": limit}, operation)


@mcp.tool()
def list_strings(session_id: str, query: str = "", offset: int = 0, limit: int = 100) -> dict[str, Any]:
    """List strings discovered in the binary."""
    def operation() -> dict[str, Any]:
        strings = _session(session_id)["pipe"].cmdj("izj") or []
        if query:
            strings = [item for item in strings if query.lower() in str(item.get("string", "")).lower()]
        start = max(0, offset)
        return {"session_id": session_id, "total": len(strings), "strings": strings[start:start + max(1, min(limit, 500))]}
    return audited("aurora_reverse", "list_strings", {"session_id": session_id, "query": query, "offset": offset, "limit": limit}, operation)


@mcp.tool()
def disassemble(session_id: str, address_or_function: str, instruction_count: int = 80) -> dict[str, Any]:
    """Return structured disassembly at a function name or address."""
    def operation() -> dict[str, Any]:
        pipe = _session(session_id)["pipe"]
        target = _resolve_target(pipe, address_or_function)
        instructions = pipe.cmdj(f"pdj {max(1, min(instruction_count, 500))} @ {target['selector']}") or []
        if not instructions:
            raise ValueError(f"no instructions found at {target['selector']}")
        return {"session_id": session_id, **target, "instructions": instructions}
    return audited("aurora_reverse", "disassemble", {"session_id": session_id, "address_or_function": address_or_function, "instruction_count": instruction_count}, operation)


@mcp.tool()
def find_xrefs(session_id: str, address_or_function: str) -> dict[str, Any]:
    """Find cross references to a function or address."""
    def operation() -> dict[str, Any]:
        pipe = _session(session_id)["pipe"]
        target = _resolve_target(pipe, address_or_function)
        xrefs = pipe.cmdj(f"axtj @ {target['selector']}") or []
        return {"session_id": session_id, **target, "xrefs": xrefs}
    return audited("aurora_reverse", "find_xrefs", {"session_id": session_id, "address_or_function": address_or_function}, operation)


@mcp.tool()
def decompile_function(session_id: str, function: str, engine: str = "auto") -> dict[str, Any]:
    """Decompile a known function, falling back to Rizin when auto-selected Ghidra fails."""
    def operation() -> dict[str, Any]:
        selected = engine.lower()
        if selected not in {"auto", "rizin", "ghidra"}:
            raise ValueError("engine must be auto, rizin, or ghidra")
        session = _session(session_id)
        target = _resolve_target(session["pipe"], function, require_function=True)
        has_ghidra = shutil.which("analyzeHeadless") is not None
        fallback_reason: str | None = None
        if selected == "ghidra" or (selected == "auto" and has_ghidra):
            if not has_ghidra:
                raise ValueError("Ghidra is only available in the heavy worker image")
            try:
                code = _ghidra_decompile(session["path"], target["selector"])
                used = "ghidra"
            except RuntimeError as exc:
                if selected == "ghidra":
                    raise
                fallback_reason = str(exc)[:2000]
                code, representation = _rizin_decompile(session["pipe"], target["selector"])
                used = "rizin"
        else:
            code, representation = _rizin_decompile(session["pipe"], target["selector"])
            used = "rizin"
        if used == "ghidra":
            representation = "pseudocode"
        result = {
            "session_id": session_id,
            "function": function,
            "address": target["selector"],
            "engine": used,
            "representation": representation,
            "code": code,
        }
        if fallback_reason:
            result["fallback_reason"] = fallback_reason
        return result
    return audited("aurora_reverse", "decompile_function", {"session_id": session_id, "function": function, "engine": engine}, operation)


def _ghidra_decompile(binary: Path, function: str) -> str:
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()[:20]
    project_dir = WORKSPACE / "runtime" / "ghidra-projects"
    output_dir = WORKSPACE / "runtime" / "mcp-artifacts"
    project_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"ghidra-{digest}-{uuid4().hex[:8]}.txt"
    marker = project_dir / f"{digest}.rep"
    if marker.exists():
        action = ["-process", binary.name]
    else:
        action = ["-import", str(binary)]
    command = [
        "analyzeHeadless", str(project_dir), digest, *action,
        "-analysisTimeoutPerFile", str(GHIDRA_ANALYSIS_TIMEOUT_SECONDS),
        "-scriptPath", "/opt/aurora-mcp/ghidra_scripts",
        "-postScript", "AuroraDecompile.java", function, str(output),
    ]
    try:
        completed = subprocess.run(command, text=True, capture_output=True, timeout=GHIDRA_PROCESS_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired as exc:
        if action[0] == "-import":
            _remove_ghidra_project(project_dir, digest)
        detail = _ghidra_diagnostic(exc.stdout, exc.stderr)
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"Ghidra process timed out after {GHIDRA_PROCESS_TIMEOUT_SECONDS}s{suffix}") from exc
    if completed.returncode != 0 or not output.exists():
        detail = _ghidra_diagnostic(completed.stdout, completed.stderr)
        raise RuntimeError(f"Ghidra decompilation failed: {detail}")
    return output.read_text(encoding="utf-8", errors="replace")


def _ghidra_diagnostic(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    def decoded(value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value or ""

    combined = "\n".join(part for part in (decoded(stdout), decoded(stderr)) if part).strip()
    interesting = [
        line for line in combined.splitlines()
        if any(marker in line.lower() for marker in ("error", "exception", "failed", "timeout", "not found", "abort"))
    ]
    detail = "\n".join(interesting[-30:]) if interesting else combined[-4000:]
    return detail[-4000:] or "Ghidra produced no output or diagnostic"


def _remove_ghidra_project(project_dir: Path, digest: str) -> None:
    shutil.rmtree(project_dir / f"{digest}.rep", ignore_errors=True)
    for suffix in (".gpr", ".lock"):
        try:
            (project_dir / f"{digest}{suffix}").unlink()
        except FileNotFoundError:
            pass


@mcp.tool()
def list_sessions() -> dict[str, Any]:
    """List active reverse-analysis sessions without exposing backend handles."""
    return {
        "sessions": [
            {"session_id": session_id, "path": str(session["path"])}
            for session_id, session in sorted(SESSIONS.items())
        ]
    }


@mcp.resource("mcp-resource://aurora_reverse/list_sessions")
def session_inventory_resource() -> str:
    """Return active reverse-analysis sessions as JSON."""
    return json.dumps(list_sessions(), ensure_ascii=True, sort_keys=True)


@mcp.tool()
def close_session(session_id: str) -> dict[str, Any]:
    """Close a reverse analysis session."""
    def operation() -> dict[str, Any]:
        session = _session(session_id)
        session["pipe"].quit()
        del SESSIONS[session_id]
        return {"session_id": session_id, "status": "closed"}
    return audited("aurora_reverse", "close_session", {"session_id": session_id}, operation)


if __name__ == "__main__":
    mcp.run(transport="stdio")
