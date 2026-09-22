"""Select explicit handoff memory before recent project-wide observations."""
from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, select

from aurora.models import Attempt, AttemptCheckpoint, Fact, Intent, ProjectRuntimePolicy, WorkerEvent
from aurora.services.evidence_context import current_environment_id


TERMINAL_ATTEMPTS = ["SUCCESS", "COMPLETED", "PARTIAL", "FAILED", "TIMEOUT"]


def parent_attempt_candidates(
    session: Session, *, project_id: str, intent: Intent | None,
    current_attempt: Attempt | None = None, resumable_only: bool = True,
) -> list[Attempt]:
    """Share parent ordering between dispatch, context selection and restoration.

    An explicit persisted parent is authoritative. Peer branches may inherit
    their own retries or declared parent, but never an unrelated peer session.
    """
    candidates: list[Attempt] = []

    def add(candidate: Attempt | None) -> None:
        if (candidate is not None and candidate.project_id == project_id
                and candidate.status in TERMINAL_ATTEMPTS
                and (current_attempt is None or candidate.id != current_attempt.id)
                and all(existing.id != candidate.id for existing in candidates)):
            candidates.append(candidate)

    if current_attempt and current_attempt.parent_attempt_id:
        add(session.get(Attempt, current_attempt.parent_attempt_id))
    base = select(Attempt).where(Attempt.project_id == project_id, Attempt.status.in_(TERMINAL_ATTEMPTS))
    if current_attempt:
        base = base.where(Attempt.id != current_attempt.id)
    if resumable_only:
        base = base.where(Attempt.codex_thread_id.is_not(None), Attempt.resume_manifest_artifact_id.is_not(None))
    base = base.order_by(Attempt.started_at.desc(), Attempt.id.desc())
    intent_id = intent.id if intent else current_attempt.intent_id if current_attempt else None
    if intent_id:
        for candidate in session.exec(base.where(Attempt.intent_id == intent_id).limit(8)).all():
            add(candidate)
    if intent:
        continuation_id = (intent.budget or {}).get("continuation_attempt_id")
        continuation = session.get(Attempt, continuation_id) if isinstance(continuation_id, str) else None
        if continuation and continuation.intent_id == intent.parent_intent_id:
            if not resumable_only or (continuation.codex_thread_id and continuation.resume_manifest_artifact_id):
                add(continuation)
        if intent.parent_intent_id:
            for candidate in session.exec(base.where(Attempt.intent_id == intent.parent_intent_id).limit(8)).all():
                add(candidate)
    policy = session.exec(select(ProjectRuntimePolicy).where(ProjectRuntimePolicy.project_id == project_id)).first()
    if not (policy and policy.multi_agent_exploration_enabled):
        for candidate in session.exec(base.limit(8)).all():
            add(candidate)
    return candidates


@dataclass
class ContextMemory:
    parent: Attempt | None
    lineage: list[Attempt]
    checkpoints: list[AttemptCheckpoint]
    facts: list[Fact]
    live_checkpoints: list[WorkerEvent]
    pinned_fact_ids: set[str]
    pinned_checkpoint_ids: set[str]
    environment_id: str | None

    def handoff(self) -> dict:
        source_environment = self.parent.environment_id if self.parent else None
        return {
            "parent_attempt_id": self.parent.id if self.parent else None,
            "lineage_attempt_ids": [attempt.id for attempt in self.lineage],
            "checkpoint_ids": [checkpoint.id for checkpoint in self.checkpoints if checkpoint.id in self.pinned_checkpoint_ids],
            "source_environment_id": source_environment,
            "current_environment_id": self.environment_id,
            "requires_revalidation": bool(self.parent and source_environment != self.environment_id),
            "policy": "Continue the first lineage checkpoint. Peer observations are shared evidence, not your next-step assignment. Revalidate environment-specific observations after an instance change.",
        }


def select_context_memory(
    session: Session, *, project_id: str, intent: Intent | None,
    attempt: Attempt | None = None, fact_limit: int = 25, checkpoint_limit: int = 3,
) -> ContextMemory:
    candidates = parent_attempt_candidates(session, project_id=project_id, intent=intent, current_attempt=attempt, resumable_only=False)
    parent = candidates[0] if candidates else None
    lineage: list[Attempt] = []
    cursor = parent
    while cursor and cursor.project_id == project_id and cursor.id not in {item.id for item in lineage} and len(lineage) < 4:
        lineage.append(cursor)
        cursor = session.get(Attempt, cursor.parent_attempt_id) if cursor.parent_attempt_id else None
    pinned_checkpoints: list[AttemptCheckpoint] = []
    for ancestor in lineage:
        checkpoint = session.exec(select(AttemptCheckpoint).where(
            AttemptCheckpoint.project_id == project_id, AttemptCheckpoint.attempt_id == ancestor.id,
        ).order_by(AttemptCheckpoint.created_at.desc(), AttemptCheckpoint.id.desc())).first()
        if checkpoint:
            pinned_checkpoints.append(checkpoint)
    # A declarative continuation can exist without a resumable native thread.
    # Preserve its checkpoint even in legacy data without an Attempt row.
    continuation_id = (intent.budget or {}).get("continuation_attempt_id") if intent else None
    if not pinned_checkpoints and isinstance(continuation_id, str):
        checkpoint = session.exec(select(AttemptCheckpoint).where(
            AttemptCheckpoint.project_id == project_id, AttemptCheckpoint.attempt_id == continuation_id,
            AttemptCheckpoint.intent_id == intent.parent_intent_id,
        )).first()
        if checkpoint:
            pinned_checkpoints.append(checkpoint)
    pinned_checkpoint_ids = {checkpoint.id for checkpoint in pinned_checkpoints}
    recent = session.exec(select(AttemptCheckpoint).where(AttemptCheckpoint.project_id == project_id)
                          .order_by(AttemptCheckpoint.created_at.desc(), AttemptCheckpoint.id.desc()).limit(checkpoint_limit)).all()
    checkpoints = [*pinned_checkpoints, *(item for item in recent if item.id not in pinned_checkpoint_ids)]
    required_ids = list(dict.fromkeys([
        *(intent.dependency_fact_ids if intent else []),
        *(ref for checkpoint in pinned_checkpoints for ref in checkpoint.fact_refs),
    ]))
    base = select(Fact).where(Fact.project_id == project_id, Fact.status == "ACTIVE").execution_options(populate_existing=True)
    required = session.exec(base.where(Fact.id.in_(required_ids))).all() if required_ids else []
    by_id = {fact.id: fact for fact in required}
    facts = [by_id[ref] for ref in required_ids if ref in by_id]
    # Source facts remain useful when a model omitted fact_refs in its handoff.
    lineage_ids = [ancestor.id for ancestor in lineage]
    own = session.exec(base.where(Fact.source_attempt_id.in_(lineage_ids))
                       .order_by(Fact.created_at.desc(), Fact.id.desc()).limit(fact_limit)).all() if lineage_ids else []
    facts.extend(fact for fact in own if fact.id not in by_id)
    pinned_fact_ids = {fact.id for fact in facts}
    recent_facts = session.exec(base.order_by(Fact.created_at.desc(), Fact.id.desc()).limit(fact_limit)).all()
    facts.extend(fact for fact in recent_facts if fact.id not in pinned_fact_ids)
    live_base = select(WorkerEvent).where(WorkerEvent.project_id == project_id, WorkerEvent.event_type == "checkpoint.saved")
    live: list[WorkerEvent] = []
    for source_id in [*([attempt.id] if attempt else []), *lineage_ids]:
        event = session.exec(live_base.where(WorkerEvent.attempt_id == source_id).order_by(WorkerEvent.created_at.desc(), WorkerEvent.id.desc())).first()
        if event:
            live.append(event)
    known = {event.id for event in live}
    live.extend(event for event in session.exec(live_base.order_by(WorkerEvent.created_at.desc(), WorkerEvent.id.desc()).limit(5)).all() if event.id not in known)
    return ContextMemory(parent, lineage, checkpoints, facts, live, pinned_fact_ids, pinned_checkpoint_ids, current_environment_id(session, project_id))
