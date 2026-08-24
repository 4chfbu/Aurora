from __future__ import annotations

import hashlib

from sqlmodel import Session

from aurora.models import WorkerEvent
from aurora.services.blackboard_repository import BlackboardRepository


def record_flag_rejection(
    session: Session,
    *,
    project_id: str,
    value: str,
    reason: str,
    evidence_refs: list[str] | None = None,
    worker_id: str | None = None,
    intent_id: str | None = None,
    attempt_id: str | None = None,
    event_type: str = "finding.flag_candidate_rejected",
) -> None:
    """Persist actionable feedback so the next worker can continue the solve."""
    refs = list(dict.fromkeys(evidence_refs or []))
    feedback = f"Candidate flag {value} was rejected: {reason}. Do not submit it again; continue investigating for the correct flag."
    repository = BlackboardRepository()
    fact = repository.upsert_fact(
        session,
        project_id=project_id,
        statement=feedback,
        category="flag_validation_feedback",
        confidence=1.0,
        evidence_refs=refs,
        source_intent_id=intent_id,
        source_attempt_id=attempt_id,
    ).item
    repository.upsert_intent(
        session,
        project_id=project_id,
        objective=f"Continue investigating after rejected candidate {value}; find the correct flag without resubmitting it.",
        capability_tags=["sandbox.exec", "blackboard.query"],
        dependency_fact_ids=[fact.id],
        parent_intent_id=intent_id,
        priority=1.0,
        risk_level="low",
    )
    session.add(
        WorkerEvent(
            project_id=project_id,
            worker_id=worker_id,
            intent_id=intent_id,
            attempt_id=attempt_id,
            event_type=event_type,
            payload_json={
                "value_hash": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                "reason": reason,
                "evidence_refs": refs,
                "continue": True,
            },
        )
    )
