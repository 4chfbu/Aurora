from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.models import (
    ChallengeGroup,
    ChallengeGroupEvent,
    ChallengeGroupItem,
    Finding,
    FlagCandidate,
    Intent,
    Project,
    WorkerEvent,
    now_utc,
)
from aurora.services.competition_adapter import (
    CompetitionAdapter,
    CompetitionSubmissionResult,
    LocalCompetitionAdapter,
    SlabMatchCompetitionAdapter,
    TSecBenchCompetitionAdapter,
    competition_platform,
)
from aurora.services.flag_rejection import record_flag_rejection
from aurora.services.flag_validator import FlagValidator
from aurora.services.project_repair import reopen_project_after_invalid_flag


@dataclass(frozen=True)
class FlagSubmissionOutcome:
    status: str
    candidate_id: str | None
    accepted: bool | None
    completed: bool
    summary: str
    detail: dict[str, Any]


class FlagSubmissionService:
    """Submit only provenance-bearing candidates through a platform adapter."""

    def submit(
        self,
        session: Session,
        *,
        project_id: str,
        candidate_id: str | None = None,
        value: str | None = None,
        adapter: CompetitionAdapter | None = None,
        worker_id: str | None = None,
        intent_id: str | None = None,
        attempt_id: str | None = None,
    ) -> FlagSubmissionOutcome:
        item = session.exec(
            select(ChallengeGroupItem)
            .where(ChallengeGroupItem.project_id == project_id)
            .order_by(ChallengeGroupItem.created_at.desc())
        ).first()
        if item is None:
            return FlagSubmissionOutcome(
                "unavailable", None, None, False,
                "flag.submit requires a challenge-group item with a competition adapter", {},
            )

        if bool(candidate_id) == bool(value):
            return FlagSubmissionOutcome(
                "invalid_candidate", None, None, False,
                "flag.submit requires exactly one of candidate_id or value", {},
            )

        if value is not None:
            candidate = self._candidate_for_value(session, project_id=project_id, value=value)
        else:
            candidate = self._candidate(session, project_id=project_id, candidate_id=candidate_id or "", attempt_id=attempt_id)
        if candidate is None:
            return FlagSubmissionOutcome(
                "invalid_candidate", None, None, False,
                "submission target does not identify an eligible candidate in this project", {},
            )
        if candidate.submission_count >= 1 and candidate.status in {"ACCEPTED", "REJECTED", "SUBMITTED", "AWAITING_MANUAL_VALIDATION"}:
            return FlagSubmissionOutcome(
                "duplicate", candidate.id, None, False,
                "candidate has already been decided by the competition platform", {},
            )

        candidate.submission_count += 1
        candidate.status = "SUBMITTED"
        candidate.updated_at = now_utc()
        session.add(candidate)
        session.flush()

        competition = adapter or self._configured_adapter(item)
        try:
            result = competition.submit_flag(session, project_id=project_id, value=candidate.value)
        except Exception as exc:
            return self._unavailable(
                session, item=item, candidate=candidate,
                reason=str(exc)[:500] or type(exc).__name__,
            )
        if result is None:
            return self._unavailable(
                session, item=item, candidate=candidate,
                reason="adapter returned no validation result",
            )

        accepted = result.correct if isinstance(result, CompetitionSubmissionResult) else bool(result)
        completed = result.completed if isinstance(result, CompetitionSubmissionResult) else bool(result)
        detail = dict(result.detail) if isinstance(result, CompetitionSubmissionResult) else {}
        if accepted:
            outcome = self._accepted(
                session, item=item, candidate=candidate, completed=completed, detail=detail,
            )
        else:
            outcome = self._rejected(
                session,
                item=item,
                candidate=candidate,
                reason=str(detail.get("reason") or detail.get("message") or "competition platform rejected the candidate flag")[:500],
                worker_id=worker_id,
                intent_id=intent_id,
                attempt_id=attempt_id,
                detail=detail,
            )
        session.commit()
        return outcome

    @staticmethod
    def _candidate(
        session: Session,
        *,
        project_id: str,
        candidate_id: str,
        attempt_id: str | None,
    ) -> FlagCandidate | None:
        if candidate_id == "latest_verified":
            if not attempt_id:
                return None
            statement = select(FlagCandidate).where(
                FlagCandidate.project_id == project_id,
                FlagCandidate.status == "LOCAL_VERIFIED",
            )
            return session.exec(
                statement.where(FlagCandidate.source_attempt_id == attempt_id).order_by(FlagCandidate.updated_at.desc())
            ).first()
        candidate = session.get(FlagCandidate, candidate_id)
        if candidate is None or candidate.project_id != project_id:
            return None
        return candidate

    @staticmethod
    def _configured_adapter(item: ChallengeGroupItem) -> CompetitionAdapter:
        settings = get_settings()
        platform = competition_platform(item)
        if platform == "tsecbench" and settings.tsecbench_configured:
            return TSecBenchCompetitionAdapter(settings=settings)
        if platform == "slab_match" and settings.slab_match_configured:
            return SlabMatchCompetitionAdapter(settings=settings)
        return LocalCompetitionAdapter()

    @staticmethod
    def _event(session: Session, item: ChallengeGroupItem, event_type: str, payload: dict[str, Any]) -> None:
        session.add(ChallengeGroupEvent(group_id=item.group_id, item_id=item.id, event_type=event_type, payload_json=payload))

    def _unavailable(
        self,
        session: Session,
        *,
        item: ChallengeGroupItem,
        candidate: FlagCandidate,
        reason: str,
    ) -> FlagSubmissionOutcome:
        item.submission_status = "AWAITING_MANUAL_VALIDATION"
        candidate.status = "AWAITING_MANUAL_VALIDATION"
        candidate.updated_at = now_utc()
        session.add_all([item, candidate])
        self._event(
            session, item, "group.item.flag_submission_unavailable",
            {"project_id": item.project_id, "candidate_id": candidate.id, "value_hash": candidate.value_hash, "reason": reason},
        )
        session.commit()
        return FlagSubmissionOutcome("unavailable", candidate.id, None, False, reason, {})

    def _accepted(
        self,
        session: Session,
        *,
        item: ChallengeGroupItem,
        candidate: FlagCandidate,
        completed: bool,
        detail: dict[str, Any],
    ) -> FlagSubmissionOutcome:
        accepted_at = now_utc()
        item.submission_status = "ACCEPTED" if completed else "PARTIAL"
        candidate.status = "ACCEPTED"
        candidate.updated_at = accepted_at
        project = session.get(Project, item.project_id)
        if project is not None:
            project.status = "COMPLETED" if completed else "WORKING"
            project.updated_at = accepted_at
            session.add(project)
        if completed:
            item.status = "COMPLETED"
            item.fused_status = "COMPLETED"
            item.stop_reason = "competition platform accepted flag"
            item.finished_at = accepted_at
            item.updated_at = accepted_at
            group = session.get(ChallengeGroup, item.group_id)
            if group is not None:
                group_items = session.exec(
                    select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == item.group_id)
                ).all()
                if all(current.id == item.id or current.fused_status in {"COMPLETED", "FAILED"} for current in group_items):
                    group.status = "COMPLETED"
                    group.finished_at = accepted_at
                    group.current_item_id = None
                else:
                    running_items = [
                        current
                        for current in group_items
                        if current.id != item.id and current.fused_status == "RUNNING"
                    ]
                    group.status = "RUNNING" if running_items else "READY"
                    group.finished_at = None
                    if not running_items:
                        group.current_item_id = None
                    elif group.current_item_id == item.id or not any(
                        current.id == group.current_item_id for current in running_items
                    ):
                        group.current_item_id = running_items[0].id
                group.updated_at = accepted_at
                session.add(group)
            pending = session.exec(select(Intent).where(Intent.project_id == item.project_id, Intent.status == "PENDING")).all()
            for intent in pending:
                intent.status = "CANCELLED"
                intent.updated_at = now_utc()
                session.add(intent)
            session.add(WorkerEvent(
                project_id=item.project_id,
                event_type="project.completed",
                payload_json={
                    "reason": "competition platform accepted flag",
                    "candidate_id": candidate.id,
                    "value_hash": candidate.value_hash,
                    "cancelled_intent_ids": [intent.id for intent in pending],
                },
            ))
        else:
            pending = session.exec(select(Intent).where(Intent.project_id == item.project_id, Intent.status == "PENDING")).first()
            if pending is None:
                session.add(Intent(
                    project_id=item.project_id,
                    objective=(
                        "Continue solving the remaining platform flags after "
                        f"{detail.get('correct_flag_count', 0)}/{detail.get('total_flag_count', '?')} were accepted."
                    ),
                    capability_tags=["sandbox.exec", "blackboard.query"],
                    priority=2.0,
                    risk_level="low",
                    budget={"model_role": "solver"},
                ))
            self._event(
                session, item, "group.item.flag_progress",
                {"project_id": item.project_id, "candidate_id": candidate.id, **detail},
            )
        session.add_all([item, candidate])
        return FlagSubmissionOutcome(
            "accepted" if completed else "partial",
            candidate.id,
            True,
            completed,
            "competition platform accepted the candidate flag" if completed else "platform accepted one flag; additional flags remain",
            detail,
        )

    def _rejected(
        self,
        session: Session,
        *,
        item: ChallengeGroupItem,
        candidate: FlagCandidate,
        reason: str,
        worker_id: str | None,
        intent_id: str | None,
        attempt_id: str | None,
        detail: dict[str, Any],
    ) -> FlagSubmissionOutcome:
        item.submission_status = "REJECTED"
        candidate.status = "REJECTED"
        candidate.rejection_reason = reason
        candidate.updated_at = now_utc()
        session.add_all([item, candidate])
        finding = session.exec(
            select(Finding).where(
                Finding.project_id == item.project_id,
                Finding.title == f"Candidate flag: {candidate.value}",
            )
        ).first()
        if finding is not None:
            reopen_project_after_invalid_flag(
                session,
                project_id=item.project_id,
                finding_id=finding.id,
                reason=reason,
            )
        else:
            project = session.get(Project, item.project_id)
            if project is not None:
                project.status = "WORKING"
                project.updated_at = now_utc()
                session.add(project)
            record_flag_rejection(
                session,
                project_id=item.project_id,
                value=candidate.value,
                reason=reason,
                evidence_refs=candidate.artifact_refs,
                worker_id=worker_id,
                intent_id=intent_id,
                attempt_id=attempt_id,
                event_type="finding.flag_submission_rejected",
            )
        self._event(
            session, item, "group.item.flag_submission_rejected",
            {"project_id": item.project_id, "candidate_id": candidate.id, "value_hash": candidate.value_hash, "reason": reason, **detail},
            )
        return FlagSubmissionOutcome("rejected", candidate.id, False, False, reason, detail)

    @staticmethod
    def _candidate_for_value(session: Session, *, project_id: str, value: str) -> FlagCandidate | None:
        normalized = value.strip()
        if not FlagValidator.is_valid_flag_value(normalized):
            return None
        value_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        candidate = session.exec(
            select(FlagCandidate).where(
                FlagCandidate.project_id == project_id,
                FlagCandidate.value_hash == value_hash,
            )
        ).first()
        if candidate is not None:
            return candidate
        candidate = FlagCandidate(
            project_id=project_id,
            value=normalized,
            value_hash=value_hash,
            status="PROPOSED",
            provenance_kind="DIRECT_SUBMISSION",
        )
        session.add(candidate)
        session.flush()
        return candidate
