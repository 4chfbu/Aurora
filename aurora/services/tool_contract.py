from __future__ import annotations

from aurora.config import Settings

# Server-side gates that intentionally stay outside the Codex worker container:
# they need isolated replay, secrets, or browser session control.
SERVER_GATE_TOOLS = frozenset({"flag.verify", "flag.submit", "fofa.search", "browser.interact"})

# Network actions remain gateway-routed until egress authorization is enforced
# at the container/network layer. Until then, native shell must not be
# allowed to bypass the per-request authorization policy.
POLICY_GATE_TOOLS = frozenset({"http.request", "network.scan", "web.enumerate"})

BLACKBOARD_FALLBACK_TOOL = "blackboard.query"

# Codex native shell and local MCP servers already cover these; exposing them
# in tool_requests creates a second, conflicting execution contract.
REMOVED_FROM_NATIVE = frozenset({"sandbox.exec", "binary.inspect", "forensic.inspect", "capability.request"})


def is_codex_runtime(worker_runtime: str) -> bool:
    return worker_runtime.strip().lower() in {"codex", "codex_harness", "harness"}


def native_visible_tool_names(settings: Settings) -> frozenset[str]:
    names = set(SERVER_GATE_TOOLS) | set(POLICY_GATE_TOOLS)
    if settings.native_allow_blackboard_query:
        names.add(BLACKBOARD_FALLBACK_TOOL)
    return frozenset(names)


def tools_for_runtime(settings: Settings) -> frozenset[str] | None:
    """Return the tool_requests contract, or None for the full gateway set."""
    if not is_codex_runtime(settings.worker_runtime):
        return None
    if settings.tool_contract.strip().lower() == "full_gateway":
        return None
    return native_visible_tool_names(settings)
