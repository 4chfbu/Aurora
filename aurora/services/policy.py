from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str


class PolicyEngine:
    """Authorization gating has been removed.

    The worker container is the trust boundary for a CTF solver, so network
    targets no longer need to be pre-authorized into an AuthorizationScope.
    All tool requests are allowed; callers still route through
    ``check_tool_request`` for a single decision point and a consistent
    ToolTrace ``policy_decision`` value.
    """

    def check_tool_request(self, session, *, project_id: str, tool_name: str, request: dict) -> PolicyDecision:
        return PolicyDecision(True, "authorization disabled")
