from __future__ import annotations

from typing import Any
from threading import Lock

from sqlmodel import Session

from aurora.config import get_settings
from aurora.models import AgentRuntimeSetting, now_utc


_initialization_lock = Lock()


def agent_runtime_settings(session: Session) -> AgentRuntimeSetting:
    runtime = session.get(AgentRuntimeSetting, "global")
    if runtime is not None:
        session.refresh(runtime)
        return runtime
    with _initialization_lock:
        runtime = session.get(AgentRuntimeSetting, "global")
        if runtime is None:
            settings = get_settings()
            runtime = AgentRuntimeSetting(
                multi_agent_exploration_enabled=settings.multi_agent_exploration_enabled,
                max_global_workers=settings.multi_agent_max_global_workers,
                default_max_project_workers=settings.multi_agent_max_project_workers,
                default_max_reason_intents=settings.multi_agent_max_reason_intents,
                default_max_pending_intents=settings.multi_agent_max_pending_intents,
                subagents_enabled=settings.subagents_enabled,
                default_max_subagents_per_worker=settings.subagents_max_per_worker,
                default_max_subagents_concurrent=settings.subagents_max_concurrent,
                max_challenge_group_concurrent=settings.max_challenge_group_concurrent,
            )
            session.add(runtime)
            session.commit()
            session.refresh(runtime)
        return runtime


def update_agent_runtime_settings(session: Session, values: dict[str, Any]) -> AgentRuntimeSetting:
    runtime = agent_runtime_settings(session)
    for name, value in values.items():
        setattr(runtime, name, value)
    runtime.updated_at = now_utc()
    session.add(runtime)
    session.commit()
    session.refresh(runtime)
    return runtime
