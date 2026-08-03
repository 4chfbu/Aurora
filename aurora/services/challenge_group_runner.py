from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from aurora.db import engine
from aurora.config import get_settings
from aurora.models import Attempt, ChallengeGroup, ChallengeGroupEvent, ChallengeGroupItem, Finding, Intent, Project, Worker, WorkerEvent, now_utc
from aurora.services.autorunner import AutoRunLimits
from aurora.services.competition_adapter import CompetitionAdapter, LocalCompetitionAdapter
from aurora.services.container_control import stop_project_containers
from aurora.services.harvester_runner import AutoRunnerHarvester, HarvesterRunner
from aurora.services.project_repair import reopen_project_after_invalid_flag


@dataclass
class GroupRunState:
    group_id: str
    status: str = "running"
    stop_requested: bool = False
    current_project_id: str | None = None
    started_at: datetime = field(default_factory=now_utc)
    finished_at: datetime | None = None
    error: str | None = None


class ChallengeGroupRunner:
    def __init__(self, *, harvester: HarvesterRunner | None = None, competition: CompetitionAdapter | None = None) -> None:
        self.harvester = harvester or AutoRunnerHarvester()
        self.competition = competition or LocalCompetitionAdapter()

    def run(self, session: Session, *, group_id: str, should_stop: callable | None = None, on_project: callable | None = None) -> None:
        group = session.get(ChallengeGroup, group_id)
        if group is None:
            raise ValueError("challenge group not found")
        group.status = "RUNNING"
        group.updated_at = now_utc()
        self._event(session, group_id, None, "group.started", {})
        session.add(group)
        session.commit()

        while True:
            if should_stop and should_stop():
                group.status = "STOPPED"
                group.updated_at = now_utc()
                session.add(group)
                self._event(session, group_id, group.current_item_id, "group.stopped", {"reason": "manual_stop"})
                session.commit()
                return
            items = session.exec(
                select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == group_id).order_by(ChallengeGroupItem.position)
            ).all()
            unresolved = [item for item in items if item.fused_status not in {"COMPLETED", "FAILED"}]
            recoverable = [item for item in unresolved if item.fused_status in {"TIMEOUT", "CRASHED", "STUCK"}]
            if recoverable:
                for stale_item in recoverable:
                    self._resolve_phase(
                        session,
                        group=group,
                        item=stale_item,
                        project=session.get(Project, stale_item.project_id),
                        outcome=stale_item.fused_status,
                        reason=stale_item.stop_reason or "harvester_reported_terminal_failure",
                    )
                continue
            if not unresolved:
                group.status = "COMPLETED"
                group.current_item_id = None
                group.finished_at = now_utc()
                group.updated_at = now_utc()
                session.add(group)
                self._event(session, group_id, None, "group.completed", {"terminal_items": len(items)})
                session.commit()
                self._write_done_marker(group_id)
                return

            active_phase = min(item.phase for item in unresolved)
            candidates = [item for item in unresolved if item.phase == active_phase and item.fused_status == "PENDING"]
            deadline_fraction = self._deadline_fraction(group)
            if deadline_fraction is not None and deadline_fraction <= 0.03:
                group.status = "STOPPED"
                group.current_item_id = None
                group.updated_at = now_utc()
                session.add(group)
                self._event(session, group_id, None, "group.deadline_hold", {"remaining_fraction": deadline_fraction})
                session.commit()
                return
            if deadline_fraction is not None and deadline_fraction < 0.15:
                solved = [item for item in candidates if int((item.competition_meta or {}).get("solved_by_count", 0) or 0) > 0]
                if solved:
                    low_score = min(float((item.competition_meta or {}).get("points", (item.competition_meta or {}).get("score", 0)) or 0) for item in solved)
                    candidates = [item for item in solved if float((item.competition_meta or {}).get("points", (item.competition_meta or {}).get("score", 0)) or 0) == low_score]
                else:
                    candidates = []
                if not candidates:
                    group.status = "STOPPED"
                    group.current_item_id = None
                    group.updated_at = now_utc()
                    session.add(group)
                    self._event(session, group_id, None, "group.deadline_hold", {"remaining_fraction": deadline_fraction, "reason": "no_eligible_fast_score"})
                    session.commit()
                    return
            if not candidates:
                # A separate harvester may still be reporting.  Never turn a
                # live task into a failed one merely because this loop found no
                # immediately dispatchable work.
                if any(item.fused_status == "RUNNING" for item in unresolved):
                    return
                raise RuntimeError("group has unresolved items but no runnable phase candidate")

            item = min(candidates, key=self._priority_key)

            project = session.get(Project, item.project_id)
            item.status = "RUNNING"
            item.fused_status = "RUNNING"
            item.started_at = now_utc()
            item.updated_at = now_utc()
            phase_key = str(item.phase)
            item.phase_attempts = {**item.phase_attempts, phase_key: int(item.phase_attempts.get(phase_key, 0)) + 1}
            group.current_item_id = item.id
            group.updated_at = now_utc()
            session.add(item)
            session.add(group)
            self._event(session, group_id, item.id, "group.item.started", {"project_id": item.project_id, "position": item.position, "phase": item.phase})
            session.commit()
            if on_project:
                on_project(item.project_id)

            if project is None:
                outcome, reason = "CRASHED", "project_missing"
            elif project.status == "COMPLETED":
                outcome, reason = "COMPLETED", "project_terminal"
            else:
                # Phase budgets belong to the scheduler, not the solver.
                limits = AutoRunLimits(
                    max_iterations=0,
                    max_minutes=30 if item.phase == 1 else 60,
                    no_progress_limit=0,
                    stop_on_observer_escalate=True,
                )
                health = self.competition.ensure_environment(session, project_id=project.id)
                task = self._task_payload(session, item=item, project=project)
                if not health.available:
                    outcome, reason = "STUCK", health.reason or "environment_unavailable"
                else:
                    self._maybe_fetch_hint(session, item=item, project=project)
                    task = self._task_payload(session, item=item, project=project)
                    self._event(session, group_id, item.id, "group.item.dispatched", {"project_id": project.id, "phase": item.phase, "hint_taken": item.hint_taken})
                    session.commit()
                    result = self.harvester.run(session, project_id=project.id, task=task, limits=limits, should_stop=should_stop)
                    project = session.get(Project, project.id)
                    if project is not None and project.status == "COMPLETED":
                        outcome, reason = "COMPLETED", result.reason
                    else:
                        outcome, reason = self._failure_outcome(result.status), result.reason
            self._resolve_phase(session, group=group, item=item, project=project, outcome=outcome, reason=reason)
            # An unavailable flag-submission API requires a human decision.
            # Leave the group paused instead of immediately redispatching the
            # same item (or, worse, treating an unverified model answer as
            # solved).
            if item.fused_status == "AWAITING_MANUAL_VALIDATION":
                return

    @staticmethod
    def _priority_key(item: ChallengeGroupItem) -> tuple[int, float, int, int]:
        meta = item.competition_meta or {}
        solved = int(meta.get("solved_by_count", 0) or 0)
        points = float(meta.get("points", meta.get("score", 0)) or 0)
        return (-solved, points, 0 if item.hint_taken else 1, item.position)

    @staticmethod
    def _deadline_fraction(group: ChallengeGroup) -> float | None:
        if group.deadline_at is None:
            return None
        total = (group.deadline_at - group.created_at).total_seconds()
        if total <= 0:
            return 0.0
        return max(0.0, (group.deadline_at - now_utc()).total_seconds() / total)

    @staticmethod
    def _failure_outcome(status: str) -> str:
        return status if status in {"TIMEOUT", "CRASHED", "STUCK"} else "FAILED"

    @staticmethod
    def _write_done_marker(group_id: str) -> None:
        marker_dir = Path(get_settings().artifact_dir) / "groups" / group_id
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "done.flag").write_text("all challenge items reached a terminal state\n", encoding="utf-8")

    def _maybe_fetch_hint(self, session: Session, *, item: ChallengeGroupItem, project: Project) -> None:
        solved = int((item.competition_meta or {}).get("solved_by_count", 0) or 0)
        retrying = bool(item.failure_history)
        should_fetch = item.phase == 3 or (item.phase == 2 and (solved == 0 or retrying))
        if should_fetch and not item.hint_taken:
            hint = self.competition.fetch_hint(session, project_id=project.id)
            if hint:
                item.hint_content = hint
                item.hint_taken = True
                session.add(item)

    @staticmethod
    def _task_payload(session: Session, *, item: ChallengeGroupItem, project: Project) -> dict[str, Any]:
        # This is intentionally raw problem context, never scheduler-authored
        # solution advice.  Failure history contains only observable outcomes.
        return {
            "title": project.name,
            "statement": project.goal,
            "challenge_type": project.challenge_type,
            "phase": item.phase,
            "attachments": list((item.competition_meta or {}).get("attachments", [])),
            "target": project.target_url,
            "hint": item.hint_content,
            "previous_attempts": list(item.failure_history),
        }

    def _resolve_phase(self, session: Session, *, group: ChallengeGroup, item: ChallengeGroupItem, project: Project | None, outcome: str, reason: str) -> None:
        executed_phase = item.phase
        terminal = outcome == "COMPLETED"
        if terminal:
            submission = self._submit_pending_flag(session, item=item)
            if submission is False:
                terminal = False
                outcome = "FLAG_REJECTED"
                reason = "competition platform rejected the candidate flag"
                item.fused_status = "PENDING"
                item.status = "PENDING"
            elif submission is None and item.submission_status == "AWAITING_MANUAL_VALIDATION":
                terminal = False
                outcome = "AWAITING_MANUAL_VALIDATION"
                reason = "flag submission API unavailable; manual validation required"
                item.fused_status = "AWAITING_MANUAL_VALIDATION"
                item.status = "AWAITING_MANUAL_VALIDATION"
                group.status = "AWAITING_MANUAL_VALIDATION"
            else:
                item.fused_status = "COMPLETED"
                item.status = "COMPLETED"
        elif item.phase >= 3:
            item.fused_status = "FAILED"
            item.status = "FAILED"
            if project is not None:
                project.status = "FAILED"
                project.updated_at = now_utc()
                session.add(project)
        else:
            item.failure_history = [*item.failure_history, {"phase": executed_phase, "outcome": outcome, "reason": reason, "at": now_utc().isoformat()}]
            item.phase += 1
            item.fused_status = "PENDING"
            item.status = "PENDING"
            # A phase failure is not the project terminal state.  Re-enable a
            # legacy project row so the next harvester phase can be dispatched.
            if project is not None and project.status in {"FAILED", "CANCELLED"}:
                project.status = "ACTIVE"
                project.updated_at = now_utc()
                session.add(project)

        item.stop_reason = reason
        item.finished_at = now_utc() if item.fused_status in {"COMPLETED", "FAILED"} else None
        item.updated_at = now_utc()
        group.current_item_id = None
        group.updated_at = now_utc()
        session.add(item)
        session.add(group)
        if item.fused_status in {"COMPLETED", "FAILED"}:
            self.competition.close_environment(project_id=item.project_id)
        self._event(session, group.id, item.id, "group.item.phase_finished", {"project_id": item.project_id, "outcome": outcome, "reason": reason, "phase": executed_phase, "next_phase": None if item.fused_status in {"COMPLETED", "FAILED"} else item.phase, "fused_status": item.fused_status})
        session.commit()

    def _submit_pending_flag(self, session: Session, *, item: ChallengeGroupItem) -> bool | None:
        findings = session.exec(
            select(Finding).where(Finding.project_id == item.project_id, Finding.title.startswith("Candidate flag: "))
        ).all()
        if not findings:
            item.submission_status = "NO_CANDIDATE"
            return None
        finding = findings[-1]
        value = finding.title.removeprefix("Candidate flag: ")
        try:
            accepted = self.competition.submit_flag(session, project_id=item.project_id, value=value)
        except Exception as exc:
            # Platform integrations are an optional verification boundary.  A
            # broken/expired endpoint must not turn an unverified model answer
            # into either a success or a rejection.
            item.submission_status = "AWAITING_MANUAL_VALIDATION"
            self._event(
                session,
                item.group_id,
                item.id,
                "group.item.flag_submission_unavailable",
                {"project_id": item.project_id, "value": value, "reason": str(exc)[:500]},
            )
            return None
        if accepted is None:
            item.submission_status = "AWAITING_MANUAL_VALIDATION"
            self._event(
                session,
                item.group_id,
                item.id,
                "group.item.flag_submission_unavailable",
                {"project_id": item.project_id, "value": value, "reason": "adapter returned no validation result"},
            )
            return None
        if accepted:
            item.submission_status = "SUBMITTED"
            return True
        item.submission_status = "REJECTED"
        reopen_project_after_invalid_flag(
            session,
            project_id=item.project_id,
            finding_id=finding.id,
            reason="competition platform rejected the candidate flag",
        )
        return False

    def validate_flag_manually(self, session: Session, *, group_id: str, item_id: str, accepted: bool) -> ChallengeGroupItem:
        """Apply a human flag decision after the platform API was unavailable."""
        group = session.get(ChallengeGroup, group_id)
        item = session.get(ChallengeGroupItem, item_id)
        if group is None or item is None or item.group_id != group_id:
            raise ValueError("challenge group item not found")
        if item.submission_status != "AWAITING_MANUAL_VALIDATION":
            raise RuntimeError("flag is not awaiting manual validation")

        finding = session.exec(
            select(Finding).where(Finding.project_id == item.project_id, Finding.title.startswith("Candidate flag: "))
        ).all()
        candidate = finding[-1] if finding else None
        if candidate is None:
            raise RuntimeError("candidate flag not found")
        value = candidate.title.removeprefix("Candidate flag: ")

        if accepted:
            item.submission_status = "MANUALLY_ACCEPTED"
            item.fused_status = "COMPLETED"
            item.status = "COMPLETED"
            item.stop_reason = "candidate flag accepted by manual validation"
            item.finished_at = now_utc()
            project = session.get(Project, item.project_id)
            if project is not None:
                project.status = "COMPLETED"
                project.updated_at = now_utc()
                session.add(project)
        else:
            item.submission_status = "MANUALLY_REJECTED"
            item.fused_status = "PENDING"
            item.status = "PENDING"
            item.stop_reason = "candidate flag rejected by manual validation"
            item.finished_at = None
            reopen_project_after_invalid_flag(
                session,
                project_id=item.project_id,
                finding_id=candidate.id,
                reason="manual validation rejected the candidate flag",
            )

        remaining = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == group_id)).all()
        if accepted and all(current.id == item.id or current.fused_status in {"COMPLETED", "FAILED"} for current in remaining):
            group.status = "COMPLETED"
            group.finished_at = now_utc()
        else:
            group.status = "READY"
            group.finished_at = None
        group.current_item_id = None
        group.updated_at = now_utc()
        item.updated_at = now_utc()
        session.add(item)
        session.add(group)
        self._event(
            session,
            group_id,
            item.id,
            "group.item.flag_manually_validated",
            {"project_id": item.project_id, "value": value, "accepted": accepted},
        )
        session.commit()
        session.refresh(item)
        if accepted:
            close_environment = getattr(self.competition, "close_environment", None)
            if callable(close_environment):
                close_environment(project_id=item.project_id)
            if group.status == "COMPLETED":
                self._write_done_marker(group_id)
        return item

    @staticmethod
    def _event(session: Session, group_id: str, item_id: str | None, event_type: str, payload: dict[str, Any]) -> None:
        session.add(ChallengeGroupEvent(group_id=group_id, item_id=item_id, event_type=event_type, payload_json=payload))


def recover_interrupted_groups(session: Session) -> list[str]:
    """Make persisted group state runnable again after an API process restart.

    Group workers run in daemon threads, whose state is intentionally local to
    the API process.  A restart used to leave the database row for the active
    item in ``RUNNING`` forever even though no thread could finish it.
    """
    recovered_groups: set[str] = set()
    running_items = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.status == "RUNNING")).all()
    for item in running_items:
        group = session.get(ChallengeGroup, item.group_id)
        project = session.get(Project, item.project_id)
        if group is None or project is None:
            continue
        if project.status in {"COMPLETED", "FAILED", "CANCELLED"}:
            item.status = project.status
            item.fused_status = project.status if project.status in {"COMPLETED", "FAILED"} else "PENDING"
            item.stop_reason = "recovered_terminal_project"
            item.finished_at = now_utc()
        else:
            workers = session.exec(select(Worker).where(Worker.project_id == project.id, Worker.status == "RUNNING")).all()
            intents = session.exec(select(Intent).where(Intent.project_id == project.id, Intent.status == "RUNNING")).all()
            attempts = session.exec(select(Attempt).where(Attempt.project_id == project.id, Attempt.status == "RUNNING")).all()
            for worker in workers:
                worker.status = "INTERRUPTED"
                worker.lease = {}
                worker.heartbeat = now_utc()
                worker.updated_at = now_utc()
                session.add(worker)
            for attempt in attempts:
                attempt.status = "INTERRUPTED"
                attempt.failure_reason = "Interrupted by API restart; recovered for retry."
                attempt.finished_at = now_utc()
                session.add(attempt)
            for intent in intents:
                intent.status = "PENDING"
                intent.lease_owner = None
                intent.lease_expires_at = None
                intent.updated_at = now_utc()
                session.add(intent)
            item.status = "PENDING"
            item.fused_status = "PENDING"
            item.started_at = None
            item.finished_at = None
            item.stop_reason = "recovered_after_api_restart"
            session.add(
                WorkerEvent(
                    project_id=project.id,
                    event_type="project.recovered_after_api_restart",
                    payload_json={"workers": [worker.id for worker in workers], "attempts": [attempt.id for attempt in attempts]},
                )
            )
        item.updated_at = now_utc()
        group.current_item_id = None
        group.updated_at = now_utc()
        session.add(item)
        session.add(group)
        ChallengeGroupRunner._event(
            session,
            group.id,
            item.id,
            "group.item.recovered_after_api_restart",
            {"project_id": project.id, "new_status": item.status},
        )
        recovered_groups.add(group.id)
    session.commit()
    return sorted(recovered_groups)


class ChallengeGroupRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, GroupRunState] = {}

    def start(self, group_id: str) -> GroupRunState:
        with self._lock:
            existing = self._runs.get(group_id)
            if existing is not None and existing.status == "running":
                return existing
            state = GroupRunState(group_id=group_id)
            self._runs[group_id] = state
        threading.Thread(target=self._run, args=(state,), daemon=True).start()
        return state

    def stop(self, group_id: str) -> GroupRunState | None:
        with self._lock:
            state = self._runs.get(group_id)
            if state is not None:
                state.stop_requested = True
                if state.current_project_id:
                    stop_project_containers(state.current_project_id)
            return state

    def status(self, group_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._runs.get(group_id)
            return asdict(state) if state else None

    def resume_interrupted_groups(self) -> list[str]:
        """Recover persisted RUNNING group items and restart their daemon threads."""
        with Session(engine) as session:
            group_ids = recover_interrupted_groups(session)
        for group_id in group_ids:
            self.start(group_id)
        return group_ids

    def wait_for_stop(self, group_id: str, *, timeout_seconds: float = 10) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            with self._lock:
                state = self._runs.get(group_id)
                if state is None or state.status not in {"running", "stopping"}:
                    return True
            time.sleep(0.1)
        return False

    def _run(self, state: GroupRunState) -> None:
        try:
            with Session(engine) as session:
                ChallengeGroupRunner().run(
                    session,
                    group_id=state.group_id,
                    should_stop=lambda: state.stop_requested,
                    on_project=lambda project_id: setattr(state, "current_project_id", project_id),
                )
                group = session.get(ChallengeGroup, state.group_id)
            with self._lock:
                state.status = "stopped" if state.stop_requested or (group is not None and group.status == "STOPPED") else "completed"
                state.current_project_id = None
                state.finished_at = now_utc()
        except Exception as exc:
            with self._lock:
                state.status = "failed"
                state.error = str(exc)
                state.finished_at = now_utc()
            # A daemon-thread exception must be visible after a restart too;
            # keeping it only in this in-memory state would strand the group.
            with Session(engine) as session:
                group = session.get(ChallengeGroup, state.group_id)
                if group is not None:
                    group.status = "FAILED"
                    group.current_item_id = None
                    group.updated_at = now_utc()
                    session.add(group)
                    ChallengeGroupRunner._event(
                        session,
                        group.id,
                        None,
                        "group.failed",
                        {"error": str(exc)[:1000]},
                    )
                    session.commit()


challenge_group_registry = ChallengeGroupRegistry()
