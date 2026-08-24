from __future__ import annotations

import shutil
from pathlib import Path

from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.models import Artifact, Attempt, AttemptCheckpoint, ChallengeGroup, ChallengeGroupItem, ContextSnapshot, DiscoveredTarget, Fact, Finding, FlagCandidate, Intent, LLMTrace, Project, ToolTrace, Worker, WorkerEvent, now_utc
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

    reset_group_item_ids: list[str] = []
    group_ids: set[str] = set()
    for group_item in session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.project_id == project_id)).all():
        if group_item.fused_status == "COMPLETED" or group_item.submission_status in {"SUBMITTED", "ACCEPTED", "MANUALLY_ACCEPTED"}:
            continue
        group_item.status = "PENDING"
        group_item.fused_status = "PENDING"
        group_item.phase = 1
        group_item.phase_attempts = {}
        group_item.failure_history = []
        group_item.submission_status = "NOT_SUBMITTED"
        group_item.stop_reason = None
        group_item.started_at = None
        group_item.phase_started_at = None
        group_item.phase_deadline_at = None
        group_item.finished_at = None
        group_item.updated_at = now_utc()
        session.add(group_item)
        reset_group_item_ids.append(group_item.id)
        group_ids.add(group_item.group_id)
    for group_id in group_ids:
        group = session.get(ChallengeGroup, group_id)
        if group is None:
            continue
        if group.status in {"COMPLETED", "FAILED", "STOPPED", "WAITING_INPUT", "WAITING_RESOURCE", "AWAITING_MANUAL_VALIDATION"}:
            group.status = "READY"
            group.finished_at = None
        if group.current_item_id in reset_group_item_ids:
            group.current_item_id = None
        group.updated_at = now_utc()
        session.add(group)

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
            payload_json={"cleared": ["facts", "intents", "workers", "attempts", "attempt_checkpoints", "findings", "flag_candidates", "context_snapshots", "llm_traces", "tool_traces"], "preserved": ["artifacts", "hints", "events"], "acknowledged_warning_ids": warning_ids, "reset_group_item_ids": reset_group_item_ids},
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
