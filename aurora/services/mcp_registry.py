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
    MCPToolDefinition(
        "flag.verify",
        "verification",
        "aurora",
        "Replay a Python derivation twice against declared evidence before accepting its flag output.",
        {
            "type": "object",
            "required": ["source_artifact_refs", "verification_script"],
            "properties": {
                "source_artifact_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Current-project Artifact IDs or evidence files inside the current Worker's /workspace.",
                },
                "verification_script": {
                    "type": "string",
                    "description": (
                        "Python script path in the Worker workspace (preferred), or inline Python source. "
                        "Receives inputs/manifest.json as sys.argv[1]: a JSON array of objects with artifact_id, path, sha256. "
                        "Use entries = json.load(open(sys.argv[1])); iterate entries directly (no files key or .get on the array). "
                        "Read each entry['path'] relative to the working directory without prepending inputs/. "
                        "Only declared inputs exist; they are read-only. Write scratch files in /tmp. "
                        "Print exactly one computed flag to stdout, diagnostics to stderr; never hard-code the flag."
                    ),
                },
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60},
            },
        },
    ),
    MCPToolDefinition(
        "flag.submit",
        "submission",
        "aurora",
        "Submit a candidate flag to the current competition platform and return the platform verdict. Use candidate_id; a raw value is accepted only when it already matches a LOCAL_VERIFIED candidate in this project.",
        {
            "type": "object",
            "properties": {
                "candidate_id": {
                    "type": "string",
                    "description": "A visible LOCAL_VERIFIED candidate id, or latest_verified immediately after flag.verify in the same request batch.",
                },
                "value": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "description": "A prefix{payload} value that already matches a LOCAL_VERIFIED candidate in this project; unknown values are rejected without creating evidence.",
                },
            },
            "oneOf": [
                {"required": ["candidate_id"]},
                {"required": ["value"]},
            ],
            "additionalProperties": False,
        },
    ),
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


def scheduling_capabilities(settings: Settings) -> set[str]:
    names = {tool.name for tool in BASE_TOOLS}
    names.add("codex.shell")
    if settings.fofa_configured:
        names.add(FOFA_TOOL.name)
    return names


def visible_mcp_tools(
    settings: Settings,
    *,
    allow_subagents: bool = False,
    contract: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    from aurora.services.tool_contract import tools_for_runtime

    allowed = tools_for_runtime(settings) if contract is None else contract
    tools = list(BASE_TOOLS)
    if settings.fofa_configured:
        tools.append(FOFA_TOOL)
    if allow_subagents:
        tools.append(SUBAGENT_TOOL)
    if allowed is not None:
        tools = [
            tool
            for tool in tools
            if tool.name in allowed or (allow_subagents and tool.name == SUBAGENT_TOOL.name)
        ]
    return [tool.__dict__ for tool in tools]
