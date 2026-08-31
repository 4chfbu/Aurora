from __future__ import annotations

from datetime import timedelta

from sqlalchemy import or_, update
from sqlmodel import Session, select

from aurora.models import ProjectCoordinationState, new_id, now_utc


class ProjectCoordinationService:
    """Maintain graph watermarks and a fenced, project-scoped reason lease."""

    def ensure(self, session: Session, *, project_id: str) -> ProjectCoordinationState:
        state = session.exec(
            select(ProjectCoordinationState).where(ProjectCoordinationState.project_id == project_id)
        ).first()
        if state is None:
            state = ProjectCoordinationState(project_id=project_id)
            session.add(state)
            session.commit()
            session.refresh(state)
        return state

    def record_graph_change(self, session: Session, *, project_id: str) -> int:
        self.ensure(session, project_id=project_id)
        session.exec(
            update(ProjectCoordinationState)
            .where(ProjectCoordinationState.project_id == project_id)
            .values(graph_version=ProjectCoordinationState.graph_version + 1, updated_at=now_utc())
            .execution_options(synchronize_session=False)
        )
        session.commit()
        state = self.ensure(session, project_id=project_id)
        session.refresh(state)
        return state.graph_version

    def claim_reason(self, session: Session, *, project_id: str, lease_seconds: int = 120) -> tuple[str, int] | None:
        state = self.ensure(session, project_id=project_id)
        if state.graph_version <= state.last_reasoned_version:
            return None
        owner = new_id("reason")
        now = now_utc()
        claimed = session.exec(
            update(ProjectCoordinationState)
            .where(
                ProjectCoordinationState.project_id == project_id,
                ProjectCoordinationState.graph_version > ProjectCoordinationState.last_reasoned_version,
                or_(
                    ProjectCoordinationState.reason_lease_owner.is_(None),
                    ProjectCoordinationState.reason_lease_expires_at < now,
                ),
            )
            .values(
                reason_lease_owner=owner,
                reason_lease_expires_at=now + timedelta(seconds=max(1, lease_seconds)),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            session.rollback()
            return None
        session.commit()
        session.refresh(state)
        return owner, state.graph_version

    def finish_reason(self, session: Session, *, project_id: str, owner: str, reasoned_version: int) -> bool:
        finished = session.exec(
            update(ProjectCoordinationState)
            .where(
                ProjectCoordinationState.project_id == project_id,
                ProjectCoordinationState.reason_lease_owner == owner,
            )
            .values(
                last_reasoned_version=reasoned_version,
                reason_lease_owner=None,
                reason_lease_expires_at=None,
                updated_at=now_utc(),
            )
            .execution_options(synchronize_session=False)
        )
        session.commit()
        return finished.rowcount == 1

    def release_reason(self, session: Session, *, project_id: str, owner: str) -> None:
        session.exec(
            update(ProjectCoordinationState)
            .where(
                ProjectCoordinationState.project_id == project_id,
                ProjectCoordinationState.reason_lease_owner == owner,
            )
            .values(reason_lease_owner=None, reason_lease_expires_at=None, updated_at=now_utc())
            .execution_options(synchronize_session=False)
        )
        session.commit()
