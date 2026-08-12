#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


SERVERS = {
    "aurora_reverse": ("/opt/aurora-mcp/reverse_server.py", {"open_binary", "decompile_function", "find_xrefs", "list_sessions"}),
    "aurora_debug": ("/opt/aurora-mcp/debug_server.py", {"start_session", "read_memory", "stop_session"}),
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
    subprocess.run(["gcc", "-O0", "-g", "-fno-pie", "-no-pie", str(source), "-o", str(binary)], check=True)
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


async def check_server(name: str, script: str, expected: set[str], binary: Path) -> None:
    parameters = StdioServerParameters(command="/opt/aurora-venv/bin/python", args=[script])
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
            print(f"{name}: {len(names)} tools")


async def main() -> None:
    binary = build_smoke_binary()
    for name, (script, expected) in SERVERS.items():
        await check_server(name, script, expected, binary)


if __name__ == "__main__":
    asyncio.run(main())
