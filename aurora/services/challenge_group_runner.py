from __future__ import annotations

import threading
import time
import hashlib
import re
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from aurora.db import engine
from aurora.config import get_settings
from aurora.models import Attempt, ChallengeGroup, ChallengeGroupEvent, ChallengeGroupItem, Finding, FlagCandidate, Intent, Project, Worker, WorkerEvent, now_utc
from aurora.services.autorunner import AutoRunLimits, AutoRunnerService
from aurora.services.competition_adapter import (
    CompetitionAdapter,
    CompetitionSubmissionResult,
    LocalCompetitionAdapter,
    SlabMatchCompetitionAdapter,
    TSecBenchCompetitionAdapter,
    competition_platform,
    is_managed_competition_platform,
)
from aurora.services.container_control import stop_project_containers
from aurora.services.harvester_runner import AutoRunnerHarvester, HarvesterRunner
from aurora.services.flag_submission import FlagSubmissionService
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
        self._tsecbench_competition: TSecBenchCompetitionAdapter | None = None
        self._slab_match_competition: SlabMatchCompetitionAdapter | None = None

    def _competition_for(self, item: ChallengeGroupItem | None) -> CompetitionAdapter:
        platform = competition_platform(item)
        if platform == "tsecbench" and isinstance(self.competition, LocalCompetitionAdapter):
            if self._tsecbench_competition is None:
                self._tsecbench_competition = TSecBenchCompetitionAdapter()
            return self._tsecbench_competition
        if platform == "slab_match" and isinstance(self.competition, LocalCompetitionAdapter):
            if self._slab_match_competition is None:
                self._slab_match_competition = SlabMatchCompetitionAdapter()
            return self._slab_match_competition
        return self.competition

    def run(self, session: Session, *, group_id: str, should_stop: callable | None = None, on_project: callable | None = None) -> None:
        group = session.get(ChallengeGroup, group_id)
        if group is None:
            raise ValueError("challenge group not found")
        group.status = "RUNNING"
        group.updated_at = now_utc()
        self._event(session, group_id, None, "group.started", {})
        session.add(group)
        session.commit()
        self._reactivate_waiting_inputs(session, group_id=group_id)
        self._reactivate_waiting_resources(session, group_id=group_id)

        # Targets are allocated lazily by the competition adapter.  TSecBench
        # allows a bounded pool of live instances, so dispatch up to that
        # configured limit instead of requiring targets during import.
        if self._max_workers(session, group) > 1:
            self._run_concurrent(session, group_id=group_id, should_stop=should_stop, on_project=on_project)
            return

        while True:
            if should_stop and should_stop():
                self._close_running_environments(session, group_id=group_id)
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

            pending = [item for item in unresolved if item.fused_status == "PENDING"]
            if not pending:
                if any(item.fused_status == "WAITING_RESOURCE" for item in unresolved):
                    group.status = "WAITING_RESOURCE"
                    group.current_item_id = None
                    group.updated_at = now_utc()
                    session.add(group)
                    self._event(session, group_id, None, "group.waiting_resource", {"reason": "competition capacity is temporarily unavailable"})
                    session.commit()
                    return
                if any(item.fused_status == "WAITING_INPUT" for item in unresolved):
                    group.status = "WAITING_INPUT"
                    group.current_item_id = None
                    group.updated_at = now_utc()
                    session.add(group)
                    self._event(session, group_id, None, "group.waiting_input", {"reason": "one or more challenges require external input"})
                    session.commit()
                    return
                if any(item.fused_status == "RUNNING" for item in unresolved):
                    return
                raise RuntimeError("group has unresolved items but no runnable phase candidate")
            active_phase = min(item.phase for item in pending)
            candidates = [item for item in pending if item.phase == active_phase]
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

            attempts_before: int | None = None
            project = session.get(Project, item.project_id)
            self._ensure_phase_window(item)
            item.status = "RUNNING"
            item.fused_status = "RUNNING"
            item.started_at = item.started_at or now_utc()
            item.stop_reason = None
            item.updated_at = now_utc()
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
            elif self._is_explicit_no_flag_challenge(project):
                # Some CTF platforms include attendance/VM onboarding tasks
                # whose statement explicitly says that no flag is required.
                # They are terminal analysis tasks, not failed flag recovery.
                outcome, reason = "NO_FLAG_COMPLETED", "statement_explicitly_requires_no_flag"
            elif self._requires_target(project, item):
                item.fused_status = "WAITING_INPUT"
                item.status = "WAITING_INPUT"
                item.stop_reason = "target_required_before_web_actions"
                item.finished_at = None
                project.status = "WAITING_INPUT"
                project.updated_at = now_utc()
                session.add(project)
                self._event(session, group_id, item.id, "group.item.waiting_input", {"project_id": project.id, "blocker": "target", "resume_when": "project.target_url is set"})
                session.add(item)
                group.current_item_id = None
                group.updated_at = now_utc()
                session.add(group)
                session.commit()
                continue
            elif project.status in {"COMPLETED", "FLAG_READY"}:
                outcome, reason = "COMPLETED", "project_terminal"
            elif project.status in {"FAILED", "CANCELLED"}:
                outcome, reason = "PROJECT_TERMINAL_FAILED", "project_terminal"
            elif not self._ensure_runnable_phase_intent(session, item=item, project=project):
                outcome, reason = "CRASHED", "no_runnable_intent_before_environment_allocation"
            else:
                # Phase budgets belong to the scheduler, not the solver.
                limits = self._autorun_limits(item)
                self._apply_phase_attempt_budget(
                    session,
                    project_id=project.id,
                    phase=item.phase,
                    deadline_at=limits.deadline_at,
                )
                environment_warning: str | None = None
                try:
                    health = self._competition_for(item).ensure_environment(session, project_id=project.id)
                    if not health.available:
                        environment_warning = health.reason or "environment_unavailable"
                except Exception as exc:
                    environment_warning = str(exc)[:1000] or type(exc).__name__
                    self._release_managed_environment_after_failed_start(session, item=item)
                if environment_warning:
                    self._event(
                        session,
                        group_id,
                        item.id,
                        "group.item.environment_unavailable",
                        {"project_id": project.id, "phase": item.phase, "warning": environment_warning, "solver_continues": not self._is_managed_platform_item(item)},
                    )
                if environment_warning and self._is_managed_platform_item(item):
                    platform = competition_platform(item)
                    waiting_for_capacity = self._is_capacity_exhausted(environment_warning)
                    wait_status = "WAITING_RESOURCE" if waiting_for_capacity else "WAITING_INPUT"
                    item.fused_status = wait_status
                    item.status = wait_status
                    item.stop_reason = environment_warning
                    project.status = wait_status
                    project.updated_at = now_utc()
                    group.current_item_id = None
                    group.updated_at = now_utc()
                    session.add(project)
                    session.add(item)
                    session.add(group)
                    event_type = "group.item.waiting_resource" if waiting_for_capacity else "group.item.waiting_input"
                    self._event(session, group_id, item.id, event_type, {"project_id": project.id, "blocker": f"{platform or 'platform'}_environment", "reason": environment_warning})
                    session.commit()
                    continue
                self._maybe_fetch_hint(session, item=item, project=project)
                task = self._task_payload(session, item=item, project=project)
                self._event(session, group_id, item.id, "group.item.dispatched", {"project_id": project.id, "phase": item.phase, "hint_taken": item.hint_taken, "environment_warning": environment_warning})
                session.commit()
                attempts_before = self._attempt_count(session, project.id)
                try:
                    result = self.harvester.run(session, project_id=project.id, task=task, limits=limits, should_stop=should_stop)
                except Exception as exc:
                    # A provider/runtime parser failure belongs to this
                    # challenge attempt.  Let the phase scheduler retry or
                    # retire the item instead of aborting the whole group.
                    reason = self._runtime_exception_reason(exc)
                    self._interrupt_project_execution(session, project_id=project.id, reason=reason)
                    outcome = "CRASHED"
                    result = None
                    self._event(
                        session,
                        group_id,
                        item.id,
                        "group.item.solver_crashed",
                        {"project_id": project.id, "phase": item.phase, "error": reason},
                    )
                project = session.get(Project, project.id)
                if result is None:
                    pass
                elif project is not None and project.status in {"COMPLETED", "FLAG_READY"}:
                    outcome, reason = ("COMPLETED" if project.status == "COMPLETED" else "CANDIDATE_READY"), result.reason
                else:
                    outcome, reason = self._failure_outcome(result.status), result.reason
            if attempts_before is not None:
                self._record_phase_attempts(session, item=item, attempts_before=attempts_before)
            self._resolve_phase(session, group=group, item=item, project=project, outcome=outcome, reason=reason)
            # An unavailable flag-submission API requires a human decision.
            # Leave the group paused instead of immediately redispatching the
            # same item (or, worse, treating an unverified model answer as
            # solved).
            if item.fused_status == "AWAITING_MANUAL_VALIDATION":
                return

    def _run_concurrent(self, session: Session, *, group_id: str, should_stop: callable | None, on_project: callable | None) -> None:
        """Keep a bounded, rolling set of projects running while preserving phase order."""
        group = session.get(ChallengeGroup, group_id)
        if group is None:
            raise ValueError("challenge group not found")
        max_workers = self._max_workers(session, group)
        capacity_waiters: set[str] = set()
        stop_requested = False

        def run_item(item_id: str) -> tuple[str, str, str]:
            with Session(engine) as worker_session:
                current = worker_session.get(ChallengeGroupItem, item_id)
                current_group = worker_session.get(ChallengeGroup, group_id)
                project = worker_session.get(Project, current.project_id) if current else None
                attempts_before: int | None = None
                if current is None or current_group is None:
                    return item_id, "CRASHED", "group_item_missing"
                self._ensure_phase_window(current)
                if project is None:
                    outcome, reason = "CRASHED", "project_missing"
                elif self._is_explicit_no_flag_challenge(project):
                    outcome, reason = "NO_FLAG_COMPLETED", "statement_explicitly_requires_no_flag"
                elif self._requires_target(project, current):
                    current.fused_status = "WAITING_INPUT"
                    current.status = "WAITING_INPUT"
                    current.stop_reason = "target_required_before_web_actions"
                    project.status = "WAITING_INPUT"
                    project.updated_at = now_utc()
                    worker_session.add(project)
                    worker_session.add(current)
                    self._event(worker_session, group_id, current.id, "group.item.waiting_input", {"project_id": project.id, "blocker": "target"})
                    worker_session.commit()
                    return item_id, "WAITING_INPUT", "target_required_before_web_actions"
                elif project.status in {"COMPLETED", "FLAG_READY"}:
                    outcome, reason = "COMPLETED", "project_terminal"
                elif project.status in {"FAILED", "CANCELLED"}:
                    outcome, reason = "PROJECT_TERMINAL_FAILED", "project_terminal"
                elif not self._ensure_runnable_phase_intent(worker_session, item=current, project=project):
                    outcome, reason = "CRASHED", "no_runnable_intent_before_environment_allocation"
                else:
                    limits = self._autorun_limits(current)
                    self._apply_phase_attempt_budget(
                        worker_session,
                        project_id=project.id,
                        phase=current.phase,
                        deadline_at=limits.deadline_at,
                    )
                    warning = None
                    try:
                        health = self._competition_for(current).ensure_environment(worker_session, project_id=project.id)
                        if not health.available:
                            warning = health.reason or "environment_unavailable"
                    except Exception as exc:
                        warning = str(exc)[:1000]
                        self._release_managed_environment_after_failed_start(worker_session, item=current)
                    if warning and self._is_managed_platform_item(current):
                        platform = competition_platform(current)
                        waiting_for_capacity = self._is_capacity_exhausted(warning)
                        wait_status = "WAITING_RESOURCE" if waiting_for_capacity else "WAITING_INPUT"
                        current.fused_status = wait_status
                        current.status = wait_status
                        current.stop_reason = warning
                        project.status = wait_status
                        project.updated_at = now_utc()
                        worker_session.add(project)
                        worker_session.add(current)
                        event_type = "group.item.waiting_resource" if waiting_for_capacity else "group.item.waiting_input"
                        self._event(worker_session, group_id, current.id, event_type, {"project_id": project.id, "blocker": f"{platform or 'platform'}_environment", "reason": warning})
                        worker_session.commit()
                        return item_id, wait_status, warning
                    self._maybe_fetch_hint(worker_session, item=current, project=project)
                    task = self._task_payload(worker_session, item=current, project=project)
                    self._event(worker_session, group_id, current.id, "group.item.dispatched", {"project_id": project.id, "phase": current.phase, "environment_warning": warning, "max_concurrent": max_workers})
                    worker_session.commit()
                    attempts_before = self._attempt_count(worker_session, project.id)
                    result = self.harvester.run(worker_session, project_id=project.id, task=task, limits=limits, should_stop=should_stop)
                    project = worker_session.get(Project, project.id)
                    outcome, reason = (("COMPLETED" if project and project.status == "COMPLETED" else "CANDIDATE_READY", result.reason) if project and project.status in {"COMPLETED", "FLAG_READY"} else (self._failure_outcome(result.status), result.reason))
                current_group = worker_session.get(ChallengeGroup, group_id)
                current = worker_session.get(ChallengeGroupItem, item_id)
                if attempts_before is not None:
                    self._record_phase_attempts(worker_session, item=current, attempts_before=attempts_before)
                self._resolve_phase(worker_session, group=current_group, item=current, project=project, outcome=outcome, reason=reason)
                return item_id, outcome, reason

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"aurora-group-{group_id}") as pool:
            futures: dict[Future[tuple[str, str, str]], str] = {}
            while True:
                stop_requested = stop_requested or bool(should_stop and should_stop())
                session.expire_all()
                group = session.get(ChallengeGroup, group_id)
                if group is None:
                    raise ValueError("challenge group not found")

                if stop_requested and not futures:
                    self._close_running_environments(session, group_id=group_id)
                    group.status = "STOPPED"
                    group.current_item_id = None
                    group.updated_at = now_utc()
                    session.add(group)
                    self._event(session, group_id, None, "group.stopped", {"reason": "manual_stop"})
                    session.commit()
                    return

                items = session.exec(
                    select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == group_id).order_by(ChallengeGroupItem.position)
                ).all()
                unresolved = [item for item in items if item.fused_status not in {"COMPLETED", "FAILED"}]
                if not unresolved and not futures:
                    group.status = "COMPLETED"
                    group.current_item_id = None
                    group.finished_at = now_utc()
                    group.updated_at = now_utc()
                    session.add(group)
                    self._event(session, group_id, None, "group.completed", {"terminal_items": len(items), "max_concurrent": max_workers})
                    session.commit()
                    self._write_done_marker(group_id)
                    return

                if not stop_requested and len(futures) < max_workers:
                    pending = [
                        item for item in unresolved
                        if item.fused_status == "PENDING" and item.id not in capacity_waiters
                    ]
                    if pending:
                        active_phase = min(item.phase for item in pending)
                        candidates = sorted(
                            (item for item in pending if item.phase == active_phase),
                            key=self._priority_key,
                        )[:max_workers - len(futures)]
                        dispatch: list[str] = []
                        for item in candidates:
                            self._ensure_phase_window(item)
                            item.status = "RUNNING"
                            item.fused_status = "RUNNING"
                            item.started_at = item.started_at or now_utc()
                            item.stop_reason = None
                            item.updated_at = now_utc()
                            group.current_item_id = item.id
                            group.updated_at = now_utc()
                            session.add(item)
                            self._event(session, group_id, item.id, "group.item.started", {"project_id": item.project_id, "position": item.position, "phase": item.phase, "max_concurrent": max_workers})
                            dispatch.append(item.id)
                            if on_project:
                                on_project(item.project_id)
                        session.add(group)
                        session.commit()
                        for item_id in dispatch:
                            future = pool.submit(run_item, item_id)
                            futures[future] = item_id

                if futures:
                    completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                    released_or_finished = False
                    for future in completed:
                        item_id = futures.pop(future)
                        try:
                            _, outcome, reason = future.result()
                            if outcome == "WAITING_RESOURCE" and self._is_capacity_exhausted(reason):
                                capacity_waiters.add(item_id)
                            else:
                                released_or_finished = True
                        except Exception as exc:
                            # Each worker owns an independent Session, so recover
                            # with another fresh Session even when the failed one
                            # was left in a transaction-error state.
                            with Session(engine) as recovery_session:
                                self._recover_crashed_item(
                                    recovery_session,
                                    group_id=group_id,
                                    item_id=item_id,
                                    error=exc,
                                )
                            released_or_finished = True
                    if released_or_finished and capacity_waiters:
                        self._reactivate_capacity_waiters(session, item_ids=capacity_waiters)
                        capacity_waiters.clear()
                    continue

                pending = [item for item in unresolved if item.fused_status == "PENDING"]
                if pending:
                    # State can change between a worker commit and the main
                    # Session refresh. Re-enter dispatch instead of pausing a
                    # group that has runnable work.
                    continue
                if any(item.fused_status == "WAITING_RESOURCE" for item in unresolved):
                    group.status = "WAITING_RESOURCE"
                    group.current_item_id = None
                    group.updated_at = now_utc()
                    session.add(group)
                    self._event(session, group_id, None, "group.waiting_resource", {"reason": "competition capacity is temporarily unavailable"})
                    session.commit()
                    return
                if any(item.fused_status == "WAITING_INPUT" for item in unresolved):
                    group.status = "WAITING_INPUT"
                    group.current_item_id = None
                    group.updated_at = now_utc()
                    session.add(group)
                    self._event(session, group_id, None, "group.waiting_input", {"reason": "one or more challenges require external input"})
                    session.commit()
                    return
                if any(item.fused_status == "RUNNING" for item in unresolved):
                    return
                raise RuntimeError("group has unresolved items but no runnable phase candidate")

    @staticmethod
    def _phase_max_minutes(item: ChallengeGroupItem) -> int:
        evaluation = str((item.competition_meta or {}).get("provenance") or "") == "evaluation_snapshot"
        evaluation_minutes = {1: 5, 2: 20, 3: 25}
        return evaluation_minutes.get(item.phase, 25) if evaluation else (30 if item.phase == 1 else 60)

    @classmethod
    def _ensure_phase_window(cls, item: ChallengeGroupItem) -> None:
        started_at = item.phase_started_at or now_utc()
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        item.phase_started_at = started_at
        if item.phase_deadline_at is None:
            item.phase_deadline_at = started_at + timedelta(minutes=cls._phase_max_minutes(item))

    @classmethod
    def _autorun_limits(cls, item: ChallengeGroupItem) -> AutoRunLimits:
        cls._ensure_phase_window(item)
        max_minutes = cls._phase_max_minutes(item)
        deadline_at = item.phase_deadline_at
        if deadline_at is not None and deadline_at.tzinfo is None:
            deadline_at = deadline_at.replace(tzinfo=timezone.utc)
        return AutoRunLimits(
            # A phase is bounded by its deadline, not by an arbitrary number
            # of Solver turns. AutoRunner treats zero as unlimited and still
            # stops for completion, runtime failures, manual stop, or timeout.
            max_iterations=0,
            # The Worker hard timeout is clamped to the same phase envelope in
            # _apply_phase_attempt_budget, so this outer deadline is effective
            # even while a model turn is still running.
            max_minutes=max_minutes,
            no_progress_limit=0,
            stop_on_observer_escalate=True,
            phase=item.phase,
            deadline_at=deadline_at,
        )

    @classmethod
    def _ensure_runnable_phase_intent(
        cls,
        session: Session,
        *,
        item: ChallengeGroupItem,
        project: Project,
    ) -> bool:
        limits = cls._autorun_limits(item)
        return AutoRunnerService.ensure_runnable_intent(
            session,
            project_id=project.id,
            phase=item.phase,
            deadline_at=limits.deadline_at,
        )

    @staticmethod
    def _priority_key(item: ChallengeGroupItem) -> tuple[int, int, float, int]:
        meta = item.competition_meta or {}
        solved = int(meta.get("solved_by_count", 0) or 0)
        points = float(meta.get("points", meta.get("score", 0)) or 0)
        no_environment = competition_platform(item) == "slab_match" and meta.get("requires_environment") is False
        attachment_first = no_environment and bool(meta.get("attachments"))
        return (0 if attachment_first else 1 if no_environment else 2, -solved, points, item.position)

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
    def _is_explicit_no_flag_challenge(project: Project) -> bool:
        statement = project.goal or ""
        return bool(re.search(
            r"(?:you\s+don['’]t\s+need\s+to\s+(?:input|submit|enter)\s+(?:a\s+)?flag|no\s+flag\s+(?:is\s+)?required|无需(?:输入|提交|填写).{0,12}flag)",
            statement,
            re.IGNORECASE,
        ))

    @staticmethod
    def _requires_target(project: Project, item: ChallengeGroupItem | None = None) -> bool:
        if item is not None and is_managed_competition_platform(competition_platform(item)):
            return False
        return (project.challenge_type or "").strip().lower() in {"web", "webapp", "web_app"} and not bool(project.target_url)

    @staticmethod
    def _is_tsecbench_item(item: ChallengeGroupItem | None) -> bool:
        return competition_platform(item) == "tsecbench"

    @staticmethod
    def _is_managed_platform_item(item: ChallengeGroupItem | None) -> bool:
        return is_managed_competition_platform(competition_platform(item))

    @classmethod
    def _is_tsecbench_group(cls, session: Session, group_id: str) -> bool:
        items = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == group_id)).all()
        return any(cls._is_tsecbench_item(item) for item in items)

    @classmethod
    def _is_slab_match_group(cls, session: Session, group_id: str) -> bool:
        items = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == group_id)).all()
        return any(competition_platform(item) == "slab_match" for item in items)

    @classmethod
    def _max_workers(cls, session: Session, group: ChallengeGroup) -> int:
        settings = get_settings()
        if cls._is_tsecbench_group(session, group.id):
            return min(
                max(1, int(group.max_concurrent or 1)),
                max(1, int(settings.tsecbench_max_concurrent or 1)),
            )
        if cls._is_slab_match_group(session, group.id):
            return min(
                max(1, int(group.max_concurrent or 1)),
                max(1, int(settings.slab_match_max_concurrent or 1)),
            )
        return min(
            max(1, int(group.max_concurrent or 1)),
            max(1, int(settings.max_challenge_group_concurrent or 1)),
        )

    @staticmethod
    def _close_environment(adapter: CompetitionAdapter, session: Session, *, project_id: str) -> None:
        close_environment = getattr(adapter, "close_environment", None)
        if not callable(close_environment):
            return
        try:
            close_environment(project_id=project_id, session=session)
        except TypeError as exc:
            # Keep third-party adapters implementing the older protocol
            # operational while they migrate to session-aware cleanup.
            if "session" not in str(exc):
                raise
            close_environment(project_id=project_id)

    @classmethod
    def _close_environment_with_retry(cls, adapter: CompetitionAdapter, session: Session, *, project_id: str, attempts: int = 2) -> str | None:
        """Close a managed target, retrying ambiguous control-plane failures."""
        last_error: Exception | None = None
        for index in range(max(1, attempts)):
            try:
                cls._close_environment(adapter, session, project_id=project_id)
                session.commit()
                return None
            except Exception as exc:
                last_error = exc
                if index + 1 < attempts:
                    continue
        if last_error is not None:
            # Platform adapters persist a release-pending state on ambiguous
            # failures. Keep it durable so admission control cannot reuse a slot
            # that the platform may still consider active.
            try:
                session.commit()
            except Exception:
                pass
        return str(last_error or "").strip() or None

    def _close_running_environments(self, session: Session, *, group_id: str) -> None:
        items = session.exec(
            select(ChallengeGroupItem).where(
                ChallengeGroupItem.group_id == group_id,
                ChallengeGroupItem.fused_status == "RUNNING",
            )
        ).all()
        for item in items:
            error = self._close_environment_with_retry(self._competition_for(item), session, project_id=item.project_id)
            if error is None:
                self._event(
                    session,
                    group_id,
                    item.id,
                    "group.item.environment_closed",
                    {"project_id": item.project_id, "reason": "group_stopped"},
                )
            else:
                self._event(
                    session,
                    group_id,
                    item.id,
                    "group.item.environment_cleanup_failed",
                    {"project_id": item.project_id, "error": error[:1000]},
                )

    def _release_managed_environment_after_failed_start(self, session: Session, *, item: ChallengeGroupItem) -> None:
        platform = competition_platform(item)
        if platform not in {"tsecbench", "slab_match"}:
            return
        error = self._close_environment_with_retry(self._competition_for(item), session, project_id=item.project_id)
        if error is None:
            self._event(
                session,
                item.group_id,
                item.id,
                "group.item.environment_closed",
                {"project_id": item.project_id, "platform": platform, "reason": "environment_start_failed"},
            )
        else:
            self._event(
                session,
                item.group_id,
                item.id,
                "group.item.environment_cleanup_failed",
                {"project_id": item.project_id, "platform": platform, "reason": "environment_start_failed", "error": error[:1000]},
            )
        session.commit()

    def _release_managed_environment_after_phase(
        self,
        session: Session,
        *,
        item: ChallengeGroupItem,
        executed_phase: int,
        outcome: str,
    ) -> None:
        """Release a platform target only after its phase state is resolved."""
        platform = competition_platform(item)
        if platform not in {"tsecbench", "slab_match"}:
            return
        active_workers = session.exec(
            select(Worker).where(Worker.project_id == item.project_id, Worker.status == "RUNNING")
        ).all()
        if active_workers:
            self._event(
                session,
                item.group_id,
                item.id,
                "group.item.environment_release_deferred",
                {
                    "project_id": item.project_id,
                    "platform": platform,
                    "reason": "active_workers",
                    "worker_ids": [worker.id for worker in active_workers],
                    "phase": executed_phase,
                    "outcome": outcome,
                },
            )
            return
        error = self._close_environment_with_retry(self._competition_for(item), session, project_id=item.project_id)
        if error is None:
            self._event(
                session,
                item.group_id,
                item.id,
                "group.item.environment_closed",
                {
                    "project_id": item.project_id,
                    "platform": platform,
                    "reason": "phase_finished",
                    "phase": executed_phase,
                    "outcome": outcome,
                },
            )
        else:
            self._event(
                session,
                item.group_id,
                item.id,
                "group.item.environment_cleanup_failed",
                {
                    "project_id": item.project_id,
                    "platform": platform,
                    "reason": "phase_finished",
                    "phase": executed_phase,
                    "outcome": outcome,
                    "error": error[:1000],
                },
            )

    @staticmethod
    def _runtime_exception_reason(exc: Exception) -> str:
        detail = str(exc).strip() or type(exc).__name__
        return f"solver_runtime_exception: {type(exc).__name__}: {detail}"[:1000]

    @staticmethod
    def _interrupt_project_execution(session: Session, *, project_id: str, reason: str) -> None:
        """Make a crashed Solver attempt safely resumable."""
        stop_project_containers(project_id)
        workers = session.exec(
            select(Worker).where(Worker.project_id == project_id, Worker.status == "RUNNING")
        ).all()
        attempts = session.exec(
            select(Attempt).where(Attempt.project_id == project_id, Attempt.status == "RUNNING")
        ).all()
        intents = session.exec(
            select(Intent).where(Intent.project_id == project_id, Intent.status == "RUNNING")
        ).all()
        for worker in workers:
            worker.status = "INTERRUPTED"
            worker.lease = {}
            worker.heartbeat = now_utc()
            worker.updated_at = now_utc()
            session.add(worker)
        for attempt in attempts:
            attempt.status = "INTERRUPTED"
            attempt.failure_reason = reason
            attempt.finished_at = now_utc()
            session.add(attempt)
        for intent in intents:
            intent.status = "PENDING"
            intent.lease_owner = None
            intent.lease_expires_at = None
            intent.updated_at = now_utc()
            session.add(intent)
        session.add(
            WorkerEvent(
                project_id=project_id,
                event_type="project.recovered_after_solver_crash",
                payload_json={
                    "reason": reason,
                    "workers": [worker.id for worker in workers],
                    "attempts": [attempt.id for attempt in attempts],
                    "intents": [intent.id for intent in intents],
                },
            )
        )

    def _recover_crashed_item(
        self,
        session: Session,
        *,
        group_id: str,
        item_id: str,
        error: Exception,
    ) -> None:
        """Contain a concurrent Solver exception to its challenge item."""
        group = session.get(ChallengeGroup, group_id)
        item = session.get(ChallengeGroupItem, item_id)
        if group is None or item is None or item.fused_status != "RUNNING":
            return
        project = session.get(Project, item.project_id)
        reason = self._runtime_exception_reason(error)
        if project is not None:
            self._interrupt_project_execution(session, project_id=project.id, reason=reason)
        self._event(
            session,
            group_id,
            item_id,
            "group.item.solver_crashed",
            {"project_id": item.project_id, "phase": item.phase, "error": reason},
        )
        self._resolve_phase(
            session,
            group=group,
            item=item,
            project=project,
            outcome="CRASHED",
            reason=reason,
        )

    @classmethod
    def _reactivate_waiting_inputs(cls, session: Session, *, group_id: str) -> None:
        items = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == group_id, ChallengeGroupItem.fused_status == "WAITING_INPUT")).all()
        changed = False
        for item in items:
            project = session.get(Project, item.project_id)
            if project is not None and not cls._requires_target(project, item):
                item.status = "PENDING"
                item.fused_status = "PENDING"
                item.stop_reason = "external_input_satisfied"
                item.updated_at = now_utc()
                if project.status == "WAITING_INPUT":
                    project.status = "ACTIVE"
                    project.updated_at = now_utc()
                    session.add(project)
                session.add(item)
                changed = True
        if changed:
            session.commit()

    @staticmethod
    def _reactivate_waiting_resources(session: Session, *, group_id: str) -> None:
        items = session.exec(
            select(ChallengeGroupItem).where(
                ChallengeGroupItem.group_id == group_id,
                ChallengeGroupItem.fused_status == "WAITING_RESOURCE",
            )
        ).all()
        for item in items:
            item.status = "PENDING"
            item.fused_status = "PENDING"
            item.stop_reason = "resource_retry_requested"
            item.updated_at = now_utc()
            project = session.get(Project, item.project_id)
            if project is not None and project.status == "WAITING_RESOURCE":
                project.status = "ACTIVE"
                project.updated_at = now_utc()
                session.add(project)
            session.add(item)
        if items:
            session.commit()

    @staticmethod
    def _is_capacity_exhausted(reason: str | None) -> bool:
        return str(reason or "").strip().lower() in {
            "tsecbench_capacity_exhausted",
            "slab_match_capacity_exhausted",
        }

    @classmethod
    def _reactivate_capacity_waiters(cls, session: Session, *, item_ids: set[str]) -> None:
        """Retry target-capacity waiters after another running item releases resources."""
        session.expire_all()
        changed = False
        for item_id in item_ids:
            item = session.get(ChallengeGroupItem, item_id)
            if (
                item is None
                or item.fused_status != "WAITING_RESOURCE"
                or not cls._is_capacity_exhausted(item.stop_reason)
            ):
                continue
            item.status = "PENDING"
            item.fused_status = "PENDING"
            item.updated_at = now_utc()
            project = session.get(Project, item.project_id)
            if project is not None and project.status == "WAITING_RESOURCE":
                project.status = "ACTIVE"
                project.updated_at = now_utc()
                session.add(project)
            session.add(item)
            changed = True
        if changed:
            session.commit()

    @staticmethod
    def _attempt_count(session: Session, project_id: str) -> int:
        return len(session.exec(select(Attempt).where(Attempt.project_id == project_id)).all())

    @staticmethod
    def _record_phase_attempts(session: Session, *, item: ChallengeGroupItem, attempts_before: int) -> None:
        """Record only solver attempts actually created during this dispatch."""
        actual_attempts = max(0, ChallengeGroupRunner._attempt_count(session, item.project_id) - attempts_before)
        if actual_attempts:
            phase_key = str(item.phase)
            item.phase_attempts = {**item.phase_attempts, phase_key: int(item.phase_attempts.get(phase_key, 0)) + actual_attempts}
            item.updated_at = now_utc()
            session.add(item)

    @staticmethod
    def _apply_phase_attempt_budget(
        session: Session,
        *,
        project_id: str,
        phase: int,
        deadline_at: datetime | None = None,
    ) -> None:
        group_item = session.exec(
            select(ChallengeGroupItem)
            .where(ChallengeGroupItem.project_id == project_id)
            .order_by(ChallengeGroupItem.updated_at.desc())
        ).first()
        evaluation = bool(
            group_item
            and str((group_item.competition_meta or {}).get("provenance") or "") == "evaluation_snapshot"
        )
        if evaluation:
            phase_defaults = {
                1: (240, 300, 0, 3),
                2: (1_140, 1_200, 0, 3),
                3: (1_440, 1_500, 0, 4),
            }.get(phase, (1_440, 1_500, 0, 4))
        else:
            phase_defaults = {
                1: (1_500, 1_800, 0, 3),
                2: (3_300, 3_600, 0, 3),
                3: (3_300, 3_600, 0, 4),
            }.get(phase, (3_300, 3_600, 0, 3))
        cap_seconds = phase_defaults[1]
        if deadline_at is not None:
            remaining_seconds = int((deadline_at - now_utc()).total_seconds())
            cap_seconds = max(2, min(cap_seconds, remaining_seconds))
        intents = session.exec(
            select(Intent).where(Intent.project_id == project_id, Intent.status == "PENDING")
        ).all()
        for intent in intents:
            budget = dict(intent.budget or {})
            configured = budget.get("hard_timeout_seconds")
            try:
                configured_seconds = int(configured) if configured is not None else cap_seconds
            except (TypeError, ValueError):
                configured_seconds = cap_seconds
            hard_timeout = max(2, min(configured_seconds, phase_defaults[1], cap_seconds))
            configured_soft = budget.get("soft_timeout_seconds")
            try:
                configured_soft_seconds = int(configured_soft) if configured_soft is not None else phase_defaults[0]
            except (TypeError, ValueError):
                configured_soft_seconds = phase_defaults[0]
            budget["phase"] = phase
            budget["soft_timeout_seconds"] = max(1, min(configured_soft_seconds, phase_defaults[0], hard_timeout - 1))
            budget["hard_timeout_seconds"] = hard_timeout
            # The per-attempt shell-action budget is disabled by default: a
            # fixed command count cuts off legitimate multi-step analysis
            # (extract + explore + solve) before the wall-clock timeout does.
            # soft/hard timeout + max_route_repeats still bound stuck agents.
            budget.setdefault("max_agent_actions", 0)
            budget.setdefault("max_route_repeats", phase_defaults[3])
            budget["max_no_progress_actions"] = 0
            budget["model_role"] = "solver"
            budget.setdefault("finalize_grace_seconds", 60)
            intent.budget = budget
            intent.updated_at = now_utc()
            session.add(intent)
        session.commit()

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
            adapter = self._competition_for(item)
            try:
                hint = adapter.fetch_hint(session, project_id=project.id)
                item.hint_content = hint
                item.hint_taken = True
                session.add(item)
            except Exception as exc:
                item.hint_taken = True
                session.add(item)
                self._event(session, item.group_id, item.id, "group.item.hint_unavailable", {"project_id": project.id, "reason": str(exc)[:500]})

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
            "notices": list((item.competition_meta or {}).get("notices", [])),
            "target": project.target_url,
            "targets": list((item.competition_meta or {}).get("container_addr", [])) if isinstance((item.competition_meta or {}).get("container_addr"), list) else ([project.target_url] if project.target_url else []),
            "environment_notes": (item.competition_meta or {}).get("environment_notes"),
            "hint": item.hint_content,
            "previous_attempts": list(item.failure_history),
        }

    def _resolve_phase(self, session: Session, *, group: ChallengeGroup, item: ChallengeGroupItem, project: Project | None, outcome: str, reason: str) -> None:
        executed_phase = item.phase
        terminal = outcome in {"COMPLETED", "CANDIDATE_READY", "NO_FLAG_COMPLETED"}
        if outcome == "PROJECT_TERMINAL_FAILED":
            item.fused_status = "FAILED"
            item.status = "FAILED"
        elif terminal:
            if outcome == "NO_FLAG_COMPLETED":
                item.fused_status = "COMPLETED"
                item.status = "COMPLETED"
                if project is not None:
                    project.status = "COMPLETED"
                    project.updated_at = now_utc()
                    session.add(project)
                submission = True
            else:
            # A COMPLETED project has already passed platform/manual validation.
            # FLAG_READY/CANDIDATE_READY is the only state that may submit.
                submission = True if outcome == "COMPLETED" and project is not None and project.status == "COMPLETED" else self._submit_pending_flag(session, item=item)
            submission_correct = submission.correct if isinstance(submission, CompetitionSubmissionResult) else submission
            submission_completed = submission.completed if isinstance(submission, CompetitionSubmissionResult) else submission is True
            if outcome != "NO_FLAG_COMPLETED" and submission_correct is False:
                terminal = False
                outcome = "FLAG_REJECTED"
                reason = "competition platform rejected the candidate flag"
                item.fused_status = "PENDING"
                item.status = "PENDING"
            elif outcome != "NO_FLAG_COMPLETED" and submission is None and item.submission_status == "AWAITING_MANUAL_VALIDATION":
                terminal = False
                outcome = "AWAITING_MANUAL_VALIDATION"
                reason = "flag submission API unavailable; manual validation required"
                item.fused_status = "AWAITING_MANUAL_VALIDATION"
                item.status = "AWAITING_MANUAL_VALIDATION"
                group.status = "AWAITING_MANUAL_VALIDATION"
                if project is not None:
                    project.status = "AWAITING_MANUAL_VALIDATION"
                    project.updated_at = now_utc()
                    session.add(project)
            elif outcome != "NO_FLAG_COMPLETED" and submission is None:
                terminal = False
                outcome = "NO_VERIFIED_CANDIDATE"
                reason = "no locally verified flag candidate is available"
                item.fused_status = "PENDING"
                item.status = "PENDING"
            elif outcome != "NO_FLAG_COMPLETED" and submission_correct is True and not submission_completed:
                terminal = False
                outcome = "FLAG_PARTIAL"
                reason = "competition platform accepted one flag; additional flags remain"
                item.fused_status = "PENDING"
                item.status = "PENDING"
                item.phase = 1
                item.phase_started_at = None
                item.phase_deadline_at = None
            elif outcome != "NO_FLAG_COMPLETED":
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
            item.phase_started_at = None
            item.phase_deadline_at = None
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
        self._event(session, group.id, item.id, "group.item.phase_finished", {"project_id": item.project_id, "outcome": outcome, "reason": reason, "phase": executed_phase, "next_phase": None if item.fused_status in {"COMPLETED", "FAILED"} else item.phase, "fused_status": item.fused_status})
        # Target lifetime belongs to the phase, not an individual Worker
        # invocation. At this point the item has left RUNNING and all phase
        # output (including flag submission) has been folded into its state.
        if outcome != "FLAG_PARTIAL":
            self._release_managed_environment_after_phase(
                session,
                item=item,
                executed_phase=executed_phase,
                outcome=outcome,
            )
        session.commit()

    def _submit_pending_flag(self, session: Session, *, item: ChallengeGroupItem) -> bool | None | CompetitionSubmissionResult:
        candidates = session.exec(
            select(FlagCandidate).where(
                FlagCandidate.project_id == item.project_id,
                FlagCandidate.status.in_(["LOCAL_VERIFIED", "SUBMITTED"]),
            ).order_by(FlagCandidate.created_at.desc())
        ).all()
        if not candidates:
            item.submission_status = "NO_CANDIDATE"
            return None
        candidate = candidates[0]
        if candidate.submission_count >= 1:
            item.submission_status = "AWAITING_MANUAL_VALIDATION"
            candidate.status = "AWAITING_MANUAL_VALIDATION"
            candidate.updated_at = now_utc()
            session.add(candidate)
            return None
        outcome = FlagSubmissionService().submit(
            session,
            project_id=item.project_id,
            candidate_id=candidate.id,
            adapter=self._competition_for(item),
        )
        if outcome.accepted is None:
            return None
        if isinstance(outcome.detail, dict) and outcome.detail:
            return CompetitionSubmissionResult(
                correct=bool(outcome.accepted),
                completed=outcome.completed,
                detail=outcome.detail,
            )
        return bool(outcome.accepted)

    def validate_flag_manually(
        self,
        session: Session,
        *,
        group_id: str,
        item_id: str,
        accepted: bool,
        candidate_id: str | None = None,
    ) -> ChallengeGroupItem:
        """Apply a human flag decision after the platform API was unavailable."""
        group = session.get(ChallengeGroup, group_id)
        item = session.get(ChallengeGroupItem, item_id)
        if group is None or item is None or item.group_id != group_id:
            raise ValueError("challenge group item not found")
        if item.submission_status != "AWAITING_MANUAL_VALIDATION":
            raise RuntimeError("flag is not awaiting manual validation")

        candidate = session.get(FlagCandidate, candidate_id) if candidate_id else None
        if candidate is not None and candidate.project_id != item.project_id:
            raise RuntimeError("candidate flag does not belong to the challenge group item")
        if candidate is None and candidate_id is None:
            candidates = session.exec(
                select(FlagCandidate)
                .where(
                    FlagCandidate.project_id == item.project_id,
                    FlagCandidate.status.in_(["SUBMITTED", "LOCAL_VERIFIED", "AWAITING_MANUAL_VALIDATION"]),
                )
                .order_by(FlagCandidate.created_at)
            ).all()
            candidate = candidates[-1] if candidates else None
        if candidate is None:
            # Existing installations may have paused legacy Finding rows from
            # before provenance-aware candidates were introduced.  They remain
            # ineligible for automatic submission but can be decided manually.
            finding = session.exec(
                select(Finding).where(Finding.project_id == item.project_id, Finding.title.startswith("Candidate flag: "))
            ).all()
            legacy = finding[-1] if finding else None
            if legacy is not None:
                value = legacy.title.removeprefix("Candidate flag: ").strip()
                value_hash = hashlib.sha256(value.encode("utf-8")).hexdigest()
                candidate = session.exec(
                    select(FlagCandidate).where(
                        FlagCandidate.project_id == item.project_id,
                        FlagCandidate.value_hash == value_hash,
                    )
                ).first()
                if candidate is None:
                    candidate = FlagCandidate(
                        project_id=item.project_id,
                        value=value,
                        value_hash=value_hash,
                        status="AWAITING_MANUAL_VALIDATION",
                        provenance_kind="LEGACY_UNVERIFIED",
                        artifact_refs=legacy.evidence_refs,
                    )
                    session.add(candidate)
                    session.flush()
                elif candidate.status not in {"ACCEPTED", "REJECTED"}:
                    candidate.status = "AWAITING_MANUAL_VALIDATION"
                    session.add(candidate)
        if candidate is None:
            raise RuntimeError("candidate flag not found")
        already_decided = candidate.status == ("ACCEPTED" if accepted else "REJECTED")
        if candidate.status not in {"SUBMITTED", "LOCAL_VERIFIED", "AWAITING_MANUAL_VALIDATION"} and not already_decided:
            raise RuntimeError("candidate flag is not awaiting manual validation")
        value = candidate.value

        if accepted:
            item.submission_status = "MANUALLY_ACCEPTED"
            candidate.status = "ACCEPTED"
            candidate.updated_at = now_utc()
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
            candidate.status = "REJECTED"
            candidate.rejection_reason = "manual validation rejected the candidate flag"
            candidate.updated_at = now_utc()
            item.fused_status = "PENDING"
            item.status = "PENDING"
            item.stop_reason = "candidate flag rejected by manual validation"
            item.finished_at = None
            if not already_decided:
                finding = session.exec(select(Finding).where(Finding.project_id == item.project_id, Finding.title == f"Candidate flag: {value}")).first()
                if finding is None:
                    raise RuntimeError("candidate finding not found")
                reopen_project_after_invalid_flag(
                    session,
                    project_id=item.project_id,
                    finding_id=finding.id,
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
            {"project_id": item.project_id, "value_hash": hashlib.sha256(value.encode("utf-8")).hexdigest(), "accepted": accepted},
        )
        session.commit()
        session.refresh(item)
        if accepted:
            try:
                self._close_environment(self._competition_for(item), session, project_id=item.project_id)
                session.commit()
            except Exception as exc:
                # The validation decision is already durable. Environment
                # cleanup is best-effort and must not turn it into an HTTP 500.
                self._event(
                    session,
                    group_id,
                    item.id,
                    "group.item.environment_cleanup_failed",
                    {"project_id": item.project_id, "error": str(exc)[:1000]},
                )
                session.commit()
            if group.status == "COMPLETED":
                try:
                    self._write_done_marker(group_id)
                except Exception as exc:
                    self._event(
                        session,
                        group_id,
                        item.id,
                        "group.done_marker_write_failed",
                        {"project_id": item.project_id, "error": str(exc)[:1000]},
                    )
                    session.commit()
        return item

    @staticmethod
    def _event(session: Session, group_id: str, item_id: str | None, event_type: str, payload: dict[str, Any]) -> None:
        session.add(ChallengeGroupEvent(group_id=group_id, item_id=item_id, event_type=event_type, payload_json=payload))


def recover_interrupted_groups(session: Session) -> list[str]:
    """Make persisted group state runnable again after an API process restart.

    Group workers run in daemon threads, whose state is intentionally local to
    the API process.  A restart used to leave the database row for the active
    item in ``RUNNING`` forever even though no thread could finish it.  Older
    runners also marked the whole group ``FAILED`` after one concurrent item
    crashed; those explicitly recovered pending items are safe to resume too.
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
            # Daemon threads may leave a Docker worker alive after the API
            # process exits. Stop it before re-queueing the persisted intent;
            # otherwise the old and recovered workers can solve concurrently.
            stop_project_containers(project.id)
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

    failed_groups = session.exec(
        select(ChallengeGroup).where(ChallengeGroup.status == "FAILED")
    ).all()
    for group in failed_groups:
        items = session.exec(
            select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == group.id)
        ).all()
        if not any(
            item.fused_status == "PENDING" and item.stop_reason == "recovered_after_runner_failure"
            for item in items
        ):
            continue
        group.status = "READY"
        group.current_item_id = None
        group.finished_at = None
        group.updated_at = now_utc()
        session.add(group)
        ChallengeGroupRunner._event(
            session,
            group.id,
            None,
            "group.recovered_after_runner_failure",
            {"pending_items": sum(item.fused_status == "PENDING" for item in items)},
        )
        recovered_groups.add(group.id)
    session.commit()
    return sorted(recovered_groups)


def recover_legacy_target_blocked_groups(session: Session) -> list[str]:
    """Requeue groups failed by the removed target-verification preflight.

    The legacy path failed before claiming an Intent, so a project with any
    persisted Worker is deliberately excluded. This keeps the repair from
    reopening genuine runtime failures that happened after Solver execution.
    """
    recovered_groups: set[str] = set()
    failed_items = session.exec(
        select(ChallengeGroupItem).where(
            ChallengeGroupItem.fused_status == "FAILED",
            ChallengeGroupItem.stop_reason == "runtime_error",
        )
    ).all()
    for item in failed_items:
        project = session.get(Project, item.project_id)
        group = session.get(ChallengeGroup, item.group_id)
        if project is None or group is None or project.target_verification_status == "VERIFIED":
            continue
        if session.exec(select(Worker).where(Worker.project_id == project.id)).first() is not None:
            continue
        history = list(item.failure_history or [])
        if item.phase < 3 or not history or any(entry.get("reason") != "runtime_error" for entry in history):
            continue
        preflight_events = session.exec(
            select(WorkerEvent).where(
                WorkerEvent.project_id == project.id,
                WorkerEvent.event_type == "worker.preflight_blocked",
            )
        ).all()
        if not any(event.payload_json.get("kind") == "target" for event in preflight_events):
            continue

        item.status = "PENDING"
        item.fused_status = "PENDING"
        item.phase = 1
        item.phase_attempts = {}
        item.failure_history = []
        item.stop_reason = "recovered_after_optional_target_migration"
        item.started_at = None
        item.finished_at = None
        item.updated_at = now_utc()
        project.status = "ACTIVE"
        project.updated_at = now_utc()
        group.status = "READY"
        group.current_item_id = None
        group.finished_at = None
        group.updated_at = now_utc()
        session.add(item)
        session.add(project)
        session.add(group)
        session.add(
            WorkerEvent(
                project_id=project.id,
                event_type="project.recovered_after_optional_target_migration",
                payload_json={"group_id": group.id, "item_id": item.id, "solver_continues_without_target": True},
            )
        )
        ChallengeGroupRunner._event(
            session,
            group.id,
            item.id,
            "group.item.recovered_after_optional_target_migration",
            {"project_id": project.id, "new_status": "PENDING"},
        )
        recovered_groups.add(group.id)

    session.commit()
    for group_id in recovered_groups:
        marker = Path(get_settings().artifact_dir) / "groups" / group_id / "done.flag"
        marker.unlink(missing_ok=True)
    return sorted(recovered_groups)


def fail_group_run(session: Session, *, group_id: str, error: str) -> None:
    """Persist a runner failure without leaving active items or leases live."""
    group = session.get(ChallengeGroup, group_id)
    if group is None:
        return

    # ``current_item_id`` can represent only one item, while TSecBench runs up
    # to three. Recover every active row so a group-level infrastructure fault
    # cannot strand the other concurrent projects forever.
    running_items = session.exec(
        select(ChallengeGroupItem).where(
            ChallengeGroupItem.group_id == group_id,
            ChallengeGroupItem.status == "RUNNING",
        )
    ).all()
    for item in running_items:
        project = session.get(Project, item.project_id)
        if project is not None:
            reason = f"Challenge group runner failed; recovered for retry: {error[:500]}"
            ChallengeGroupRunner._interrupt_project_execution(session, project_id=project.id, reason=reason)
        item.status = "PENDING"
        item.fused_status = "PENDING"
        item.started_at = None
        item.finished_at = None
        item.stop_reason = "recovered_after_runner_failure"
        item.updated_at = now_utc()
        session.add(item)
        ChallengeGroupRunner._event(
            session,
            group.id,
            item.id,
            "group.item.recovered_after_runner_failure",
            {"project_id": item.project_id, "new_status": item.status},
        )

    group.status = "FAILED"
    group.current_item_id = None
    group.updated_at = now_utc()
    session.add(group)
    ChallengeGroupRunner._event(session, group.id, None, "group.failed", {"error": error[:1000]})
    session.commit()


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

    def resume_after_manual_validation(self, group_id: str) -> GroupRunState | None:
        """Resume a group whose runner returned while waiting for a flag decision."""
        with self._lock:
            existing = self._runs.get(group_id)
            if existing is not None and existing.stop_requested:
                return None
            # Manual validation is only possible after the runner committed its
            # paused item. The old thread may still be unwinding, so replace its
            # state instead of letting start() mistake it for active work.
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
                if state.stop_requested or (group is not None and group.status == "STOPPED"):
                    state.status = "stopped"
                elif group is not None and group.status == "WAITING_INPUT":
                    state.status = "waiting_input"
                else:
                    state.status = "completed"
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
                fail_group_run(session, group_id=state.group_id, error=str(exc))


challenge_group_registry = ChallengeGroupRegistry()
