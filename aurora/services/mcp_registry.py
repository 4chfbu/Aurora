from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aurora.config import Settings


@dataclass(frozen=True)
class MCPToolDefinition:
    name: str
    mode: str
    backend: str
    description: str
    input_schema: dict[str, Any]


BASE_TOOLS = (
    MCPToolDefinition("sandbox.exec", "restricted", "kali-native", "Run an authorized restricted command.", {"type": "object"}),
    MCPToolDefinition("network.scan", "semantic", "kali-native", "Scan an authorized network target.", {"type": "object"}),
    MCPToolDefinition("http.request", "semantic", "kali-native", "Request an authorized HTTP target.", {"type": "object"}),
    MCPToolDefinition("browser.interact", "browser", "playwright", "Click an authorized challenge-page control and extract provisioned target URLs.", {"type": "object"}),
    MCPToolDefinition("web.enumerate", "semantic", "kali-native", "Enumerate an authorized web target.", {"type": "object"}),
    MCPToolDefinition("binary.inspect", "semantic", "kali-native", "Inspect an authorized local binary.", {"type": "object"}),
    MCPToolDefinition("forensic.inspect", "semantic", "kali-native", "Inspect an authorized local artifact.", {"type": "object"}),
    MCPToolDefinition("blackboard.query", "read", "aurora", "Read scoped blackboard state.", {"type": "object"}),
    MCPToolDefinition("capability.request", "request", "gateway", "Request an additional authorized capability.", {"type": "object"}),
)

FOFA_TOOL = MCPToolDefinition(
    "fofa.search",
    "semantic",
    "fofa-mcp",
    "Search FOFA only for a single target explicitly authorized by the project scope.",
    {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "size": {"type": "integer"}}},
)

SUBAGENT_TOOL = MCPToolDefinition(
    "subagent.spawn",
    "orchestration",
    "same-container-launcher",
    "Start a bounded, non-recursive Solver subagent in this Worker container.",
    {"type": "object", "required": ["objective"], "properties": {"objective": {"type": "string"}, "capability_tags": {"type": "array"}}},
)


def visible_mcp_tools(settings: Settings, *, allow_subagents: bool = False) -> list[dict[str, Any]]:
    tools = list(BASE_TOOLS)
    if settings.fofa_configured:
        tools.append(FOFA_TOOL)
    if allow_subagents:
        tools.append(SUBAGENT_TOOL)
    return [tool.__dict__ for tool in tools]
