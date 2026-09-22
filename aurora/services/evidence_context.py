from __future__ import annotations

from sqlmodel import Session, select

from aurora.models import Attempt, ChallengeGroupItem


def current_environment_id(session: Session, project_id: str) -> str | None:
    item = session.exec(
        select(ChallengeGroupItem)
        .where(ChallengeGroupItem.project_id == project_id)
        .order_by(ChallengeGroupItem.created_at.desc())
        .execution_options(populate_existing=True)
    ).first()
    return (item.competition_meta or {}).get("environment_id") if item else None


def artifact_environment_context(
    session: Session, *, project_id: str, source_attempt_id: str | None, origin_kind: str
) -> dict:
    if origin_kind == "challenge_input":
        return {}
    attempt = session.get(Attempt, source_attempt_id) if source_attempt_id else None
    environment_id = attempt.environment_id if attempt else current_environment_id(session, project_id)
    return {"environment_id": environment_id} if environment_id else {}
