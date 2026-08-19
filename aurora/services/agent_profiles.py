from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentProfile:
    id: str
    role: str
    system_template: str
    developer_template: str | None = None
    codex_template: str | None = None
    max_tool_requests: int = 3


PROFILES: dict[str, AgentProfile] = {
    "solver.general": AgentProfile(
        id="solver.general",
        role="solver",
        system_template="solver.system.md",
        developer_template="solver.developer.md",
        codex_template="solver.codex.md",
    ),
    "solver.subagent": AgentProfile(
        id="solver.subagent",
        role="solver",
        system_template="solver.system.md",
        developer_template="solver.developer.md",
        codex_template="solver.codex.md",
        max_tool_requests=3,
    ),
    "planner.general": AgentProfile(
        id="planner.general",
        role="solver",
        system_template="solver.system.md",
        developer_template="solver.developer.md",
        codex_template="solver.codex.md",
        max_tool_requests=1,
    ),
    "triage.general": AgentProfile(
        id="triage.general",
        role="solver",
        system_template="solver.system.md",
        developer_template="solver.developer.md",
        codex_template="solver.codex.md",
        max_tool_requests=2,
    ),
    "reviewer.general": AgentProfile(
        id="reviewer.general",
        role="solver",
        system_template="solver.system.md",
        developer_template="solver.developer.md",
        codex_template="solver.codex.md",
        max_tool_requests=1,
    ),
    "manager.planner": AgentProfile("manager.planner", "manager", "manager.system.md"),
    "observer.reviewer": AgentProfile("observer.reviewer", "observer", "observer.system.md"),
}


def get_agent_profile(profile_id: str) -> AgentProfile:
    try:
        return PROFILES[profile_id]
    except KeyError as exc:
        raise ValueError(f"unknown agent profile: {profile_id}") from exc
