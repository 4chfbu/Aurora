from __future__ import annotations

from sqlmodel import Session, select

from aurora.models import Finding, Intent, Project, WorkerEvent, now_utc
from aurora.services.flag_rejection import record_flag_rejection


def reopen_project_after_invalid_flag(
    session: Session, *, project_id: str, finding_id: str, reason: str
) -> bool:
    """Invalidate a false flag finding and restore work stopped by that finding."""
    project = session.get(Project, project_id)
    finding = session.get(Finding, finding_id)
    if project is None or finding is None or finding.project_id != project_id:
        return False

    restored_intent_ids: list[str] = []
    completion_events = session.exec(
        select(WorkerEvent).where(
            WorkerEvent.project_id == project_id,
            WorkerEvent.event_type == "project.completed",
        )
    ).all()
    for event in completion_events:
        for intent_id in event.payload_json.get("cancelled_intent_ids", []):
            intent = session.get(Intent, intent_id)
            if intent is not None and intent.status == "CANCELLED":
                intent.status = "PENDING"
                intent.updated_at = now_utc()
                session.add(intent)
                restored_intent_ids.append(intent.id)

    invalidated = {
        "finding_id": finding.id,
        "title": finding.title,
        "evidence_refs": finding.evidence_refs,
        "reason": reason,
    }
    value = finding.title.removeprefix("Candidate flag: ")
    evidence_refs = list(finding.evidence_refs)
    session.delete(finding)
    project.status = "WORKING"
    project.updated_at = now_utc()
    session.add(project)
    session.add(WorkerEvent(project_id=project_id, event_type="finding.invalidated", payload_json=invalidated))
    session.add(
        WorkerEvent(
            project_id=project_id,
            event_type="project.reopened",
            payload_json={"reason": reason, "restored_intent_ids": restored_intent_ids},
        )
    )
    record_flag_rejection(
        session,
        project_id=project_id,
        value=value,
        reason=reason,
        evidence_refs=evidence_refs,
        event_type="finding.flag_submission_rejected",
    )
    session.commit()
    return True
