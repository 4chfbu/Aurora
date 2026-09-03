#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import tomllib
from pathlib import Path
from typing import Any
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


SERVERS = {
    "aurora_reverse": ("/opt/aurora-mcp/reverse_server.py", {"triage_binary", "open_binary", "decompile_function", "find_xrefs", "find_string_xrefs", "list_imports", "list_sessions"}),
    "aurora_debug": ("/opt/aurora-mcp/debug_server.py", {"start_session", "read_memory", "pwndbg_command", "stop_session"}),
    "aurora_blackboard": ("/opt/aurora-mcp/blackboard_server.py", {"query", "read_artifact", "append_fact", "save_checkpoint"}),
}

WORKER_CONTROL_ENV_VARS = {
    "AURORA_WORKER_CONTROL_BASE_URL",
    "AURORA_WORKER_ID",
    "AURORA_WORKER_CONTROL_TOKEN",
}

SMOKE_SOURCE = """\
#include <stdio.h>

int helper(int value) {
    return value + 7;
}

int main(void) {
    printf("%d\\n", helper(35));
    return 0;
}
"""


def build_smoke_binary() -> Path:
    source = Path("/workspace/aurora-mcp-smoke.c")
    binary = Path("/workspace/aurora-mcp-smoke")
    source.write_text(SMOKE_SOURCE, encoding="utf-8")
    subprocess.run(["gcc", "-O0", "-g", "-fpie", "-pie", str(source), "-o", str(binary)], check=True)
    return binary


def tool_payload(response: Any) -> dict[str, Any]:
    if response.isError:
        detail = "\n".join(str(getattr(item, "text", item)) for item in response.content)
        raise RuntimeError(detail)
    structured = getattr(response, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for item in response.content:
        text = getattr(item, "text", None)
        if text:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
    raise RuntimeError("MCP tool returned no JSON object")


async def check_reverse_behavior(session: ClientSession, binary: Path) -> None:
    triage = tool_payload(await session.call_tool("triage_binary", {"path": str(binary)}))
    if triage.get("size") != binary.stat().st_size or len(triage.get("probes", [])) != 6:
        raise RuntimeError("aurora_reverse binary triage returned incomplete baseline")
    opened = tool_payload(await session.call_tool("open_binary", {"path": str(binary), "analyze": True}))
    session_id = str(opened["session_id"])
    try:
        listed = tool_payload(await session.call_tool("list_functions", {"session_id": session_id, "query": "main"}))
        if not listed.get("functions"):
            raise RuntimeError("aurora_reverse did not identify the smoke main function")

        invalid = await session.call_tool("disassemble", {"session_id": session_id, "address_or_function": "missing_function"})
        if not invalid.isError:
            raise RuntimeError("aurora_reverse accepted an unknown function selector")

        rizin = tool_payload(await session.call_tool("decompile_function", {"session_id": session_id, "function": "main", "engine": "rizin"}))
        if rizin.get("engine") != "rizin" or not rizin.get("code"):
            raise RuntimeError("aurora_reverse Rizin decompilation returned no code")

        resources = await session.list_resources()
        resource_uri = "mcp-resource://aurora_reverse/list_sessions"
        if resource_uri not in {str(resource.uri) for resource in resources.resources}:
            raise RuntimeError("aurora_reverse session inventory resource is missing")
        inventory = await session.read_resource(resource_uri)
        inventory_payload = json.loads(inventory.contents[0].text)
        if session_id not in {item["session_id"] for item in inventory_payload.get("sessions", [])}:
            raise RuntimeError("aurora_reverse session inventory omitted the active session")

        if os.getenv("AURORA_SMOKE_GHIDRA") == "1":
            ghidra = tool_payload(await session.call_tool("decompile_function", {"session_id": session_id, "function": "main", "engine": "ghidra"}))
            if ghidra.get("engine") != "ghidra" or not ghidra.get("code"):
                raise RuntimeError("aurora_reverse Ghidra decompilation returned no code")
    finally:
        await session.call_tool("close_session", {"session_id": session_id})


async def check_blackboard_behavior(session: ClientSession) -> None:
    response = tool_payload(await session.call_tool("query", {}))
    if response.get("project_id") != "smoke-worker" or response.get("facts") != []:
        raise RuntimeError("aurora_blackboard query did not return the control-plane response")
    artifact = tool_payload(await session.call_tool("read_artifact", {"artifact_id": "artifact_smoke", "max_bytes": 32}))
    if artifact.get("id") != "artifact_smoke" or artifact.get("content") != "smoke evidence":
        raise RuntimeError("aurora_blackboard read_artifact did not return the bounded evidence preview")


async def check_debug_behavior(session: ClientSession, binary: Path) -> None:
    started = tool_payload(await session.call_tool("start_session", {"program": str(binary)}))
    session_id = str(started["session_id"])
    try:
        diagnostic = tool_payload(await session.call_tool("pwndbg_command", {"session_id": session_id, "command": "checksec"}))
        rendered = json.dumps(diagnostic.get("responses", []), ensure_ascii=False)
        if diagnostic.get("errors") or "Undefined command" in rendered or "RELRO" not in rendered:
            raise RuntimeError("aurora_debug did not execute Pwndbg checksec successfully")
    finally:
        await session.call_tool("stop_session", {"session_id": session_id})


async def check_server(name: str, script: str, expected: set[str], binary: Path, env: dict[str, str] | None = None) -> None:
    parameters = StdioServerParameters(command="/opt/aurora-venv/bin/python", args=[script], env=env)
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            response = await session.list_tools()
            names = {tool.name for tool in response.tools}
            missing = expected - names
            if missing:
                raise RuntimeError(f"{name} is missing MCP tools: {', '.join(sorted(missing))}")
            if name == "aurora_reverse":
                await check_reverse_behavior(session, binary)
            if name == "aurora_debug":
                await check_debug_behavior(session, binary)
            if name == "aurora_blackboard":
                await check_blackboard_behavior(session)
            print(f"{name}: {len(names)} tools")


def codex_mcp_environment(name: str) -> dict[str, str]:
    config = tomllib.loads(Path("/root/.codex/config.toml").read_text(encoding="utf-8"))
    server = config["mcp_servers"][name]
    environment = {key: value for key, value in os.environ.items() if key not in WORKER_CONTROL_ENV_VARS}
    environment.update(server.get("env", {}))
    for key in server.get("env_vars", []):
        if key in os.environ:
            environment[key] = os.environ[key]
    return environment


class BlackboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.headers.get("Authorization") != "Bearer smoke-token":
            self.send_error(403)
            return
        if self.path == "/internal/workers/smoke-worker/blackboard":
            payload = {"project_id": "smoke-worker", "facts": [], "checkpoints": []}
        elif self.path == "/internal/workers/smoke-worker/artifacts/artifact_smoke?max_bytes=32":
            payload = {"id": "artifact_smoke", "content": "smoke evidence", "truncated": False}
        else:
            self.send_error(404)
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


async def main() -> None:
    binary = build_smoke_binary()
    control = ThreadingHTTPServer(("127.0.0.1", 0), BlackboardHandler)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    try:
        os.environ.update({
            "AURORA_WORKER_CONTROL_BASE_URL": f"http://127.0.0.1:{control.server_port}",
            "AURORA_WORKER_ID": "smoke-worker",
            "AURORA_WORKER_CONTROL_TOKEN": "smoke-token",
        })
        for name, (script, expected) in SERVERS.items():
            env = None
            if name == "aurora_blackboard":
                env = codex_mcp_environment(name)
            await check_server(name, script, expected, binary, env)
    finally:
        control.shutdown()
        control.server_close()


if __name__ == "__main__":
    asyncio.run(main())
