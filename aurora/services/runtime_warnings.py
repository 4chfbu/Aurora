from __future__ import annotations

from sqlmodel import Session, select

from aurora.models import WorkerEvent, now_utc


RUNTIME_ERROR_EVENT = "runtime.error"
WARNING_ACKNOWLEDGED_EVENT = "warning.acknowledged"


def list_active_runtime_warnings(session: Session, *, project_id: str) -> list[WorkerEvent]:
    events = session.exec(
        select(WorkerEvent)
        .where(WorkerEvent.project_id == project_id)
        .order_by(WorkerEvent.created_at.desc())
    ).all()
    acknowledged_ids = {
        str(event.payload_json.get("warning_event_id"))
        for event in events
        if event.event_type == WARNING_ACKNOWLEDGED_EVENT and event.payload_json.get("warning_event_id")
    }
    return [event for event in events if event.event_type == RUNTIME_ERROR_EVENT and event.id not in acknowledged_ids]


def acknowledge_runtime_warning(session: Session, *, project_id: str, warning_event_id: str, source: str = "user") -> bool:
    warning = session.get(WorkerEvent, warning_event_id)
    if warning is None or warning.project_id != project_id or warning.event_type != RUNTIME_ERROR_EVENT:
        return False
    if warning_event_id in {event.id for event in list_active_runtime_warnings(session, project_id=project_id)}:
        session.add(
            WorkerEvent(
                project_id=project_id,
                event_type=WARNING_ACKNOWLEDGED_EVENT,
                payload_json={"warning_event_id": warning_event_id, "source": source, "acknowledged_at": now_utc().isoformat()},
            )
        )
        session.commit()
    return True
