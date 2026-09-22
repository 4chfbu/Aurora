from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, or_
from sqlmodel import Session, select

from aurora.models import WorkerEvent


def event_page(session: Session, project_id: str, cursor: tuple[datetime, str] | None, *, limit: int = 100) -> list[WorkerEvent]:
    statement = select(WorkerEvent).where(WorkerEvent.project_id == project_id)
    safe_limit = max(1, min(limit, 500))
    if cursor is None:
        return list(reversed(session.exec(statement.order_by(WorkerEvent.created_at.desc(), WorkerEvent.id.desc()).limit(safe_limit)).all()))
    created_at, event_id = cursor
    return list(session.exec(statement.where(or_(
        WorkerEvent.created_at > created_at,
        and_(WorkerEvent.created_at == created_at, WorkerEvent.id > event_id),
    )).order_by(WorkerEvent.created_at, WorkerEvent.id).limit(safe_limit)).all())
