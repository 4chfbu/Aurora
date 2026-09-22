from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, select

from aurora.models import ChallengeGroup, ChallengeGroupItem, Worker, now_utc


def as_utc(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def remaining_seconds(deadline_at: datetime | str | None) -> float | None:
    return max(0.0, (as_utc(deadline_at) - now_utc()).total_seconds()) if deadline_at else None


def timeout_configuration(budget: dict) -> dict:
    previous = budget.get("scheduler_timeouts")
    previous = previous if isinstance(previous, dict) else {}
    configured = dict(previous["configured"]) if isinstance(previous.get("configured"), dict) else {}
    applied = previous.get("applied") if isinstance(previous.get("applied"), dict) else {}
    for key in ("hard_timeout_seconds", "soft_timeout_seconds", "finalize_grace_seconds"):
        if key not in applied or budget.get(key) != applied[key]:
            configured[key] = budget.get(key)
    return configured


def record_timeout_configuration(budget: dict, configured: dict) -> None:
    budget["scheduler_timeouts"] = {
        "configured": configured,
        "applied": {key: budget.get(key) for key in configured},
    }


def execution_deadline(session: Session, project_id: str, worker_id: str | None = None) -> datetime | None:
    deadlines = []
    worker = session.get(Worker, worker_id) if worker_id else None
    if worker and worker.project_id == project_id and worker.budgets.get("phase_deadline_at"):
        deadlines.append(as_utc(worker.budgets["phase_deadline_at"]))
    item = session.exec(select(ChallengeGroupItem).where(
        ChallengeGroupItem.project_id == project_id,
        ChallengeGroupItem.fused_status == "RUNNING",
    ).order_by(ChallengeGroupItem.updated_at.desc())).first()
    if item:
        if item.phase_deadline_at:
            deadlines.append(as_utc(item.phase_deadline_at))
        group = session.get(ChallengeGroup, item.group_id)
        if group and group.deadline_at:
            deadlines.append(as_utc(group.deadline_at))
    return min(deadlines) if deadlines else None
