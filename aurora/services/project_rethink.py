from __future__ import annotations

import shutil
from pathlib import Path

from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.models import Artifact, Attempt, AttemptCheckpoint, ContextSnapshot, DiscoveredTarget, Fact, Finding, FlagCandidate, Intent, LLMTrace, Project, ToolTrace, Worker, WorkerEvent, now_utc
from aurora.services.blackboard_repository import BlackboardRepository
from aurora.services.browser_sessions import browser_session_registry
from aurora.services.runtime_warnings import acknowledge_runtime_warning, list_active_runtime_warnings


BOOTSTRAP_OBJECTIVE = "Bootstrap the project by validating scope and collecting the first actionable facts."


def rethink_project(session: Session, *, project_id: str) -> Intent:
    """Clear derived working state while preserving raw evidence and audit history."""
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError("project not found")

    warning_ids = [warning.id for warning in list_active_runtime_warnings(session, project_id=project_id)]
    for warning_id in warning_ids:
        acknowledge_runtime_warning(session, project_id=project_id, warning_event_id=warning_id, source="rethink_reset")

    # Artifacts are retained as the starting evidence for the next line of reasoning.
    for artifact in session.exec(select(Artifact).where(Artifact.project_id == project_id)).all():
        artifact.source_attempt_id = None
        session.add(artifact)

    for model in (DiscoveredTarget, Fact, Finding, FlagCandidate, ToolTrace, LLMTrace, ContextSnapshot, AttemptCheckpoint, Attempt, Worker, Intent):
        for item in session.exec(select(model).where(model.project_id == project_id)).all():
            session.delete(item)

    workspace = get_settings().codex_workspace_dir / project_id
    shutil.rmtree(workspace, ignore_errors=True)
    Path(workspace).mkdir(parents=True, exist_ok=True)
    browser_session_registry.clear_project_session(project_id)

    project.status = "WORKING"
    project.updated_at = now_utc()
    session.add(project)
    session.add(
        WorkerEvent(
            project_id=project_id,
            event_type="project.rethought",
            payload_json={"cleared": ["facts", "intents", "workers", "attempts", "attempt_checkpoints", "findings", "flag_candidates", "context_snapshots", "llm_traces", "tool_traces"], "preserved": ["artifacts", "hints", "events"], "acknowledged_warning_ids": warning_ids},
        )
    )
    session.commit()

    return BlackboardRepository().upsert_intent(
        session,
        project_id=project_id,
        objective=BOOTSTRAP_OBJECTIVE,
        capability_tags=["sandbox.exec", "blackboard.query"],
        priority=1.0,
        risk_level="low",
    ).item
