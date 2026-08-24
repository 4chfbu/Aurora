from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections.abc import Callable
from typing import Any

from sqlmodel import Session, select

from aurora.models import Artifact, Fact, Finding, Intent, Project, ToolTrace, WorkerEvent, now_utc
from aurora.services.demo import run_one_demo_step
from aurora.services.blackboard_repository import BlackboardRepository
from aurora.services.manager import ManagerDecision, ManagerService
from aurora.services.observer import ObserverService
from aurora.services.project_run_control import project_run_control


@dataclass
class AutoRunLimits:
    max_iterations: int = 0
    max_minutes: int = 0
    no_progress_limit: int = 2
    stop_on_observer_escalate: bool = True
    phase: int | None = None
    deadline_at: datetime | None = None


@dataclass
class AutoRunResult:
    status: str
    stop_reason: str
    iterations: int
    project_id: str
    events: list[dict[str, Any]] = field(default_factory=list)


class AutoRunnerService:
    def run_until_stop(
        self,
        session: Session,
        *,
        project_id: str,
        limits: AutoRunLimits | None = None,
        should_stop: Callable[[], bool] | None = None,
        run_id: str | None = None,
    ) -> AutoRunResult:
        claim = None
        if run_id is None:
            claim = project_run_control.acquire(project_id=project_id, owner="autorunner")
            if claim is None:
                return AutoRunResult("busy", "project_run_active", 0, project_id)
            run_id = claim.run_id
        elif not project_run_control.owns(project_id=project_id, run_id=run_id):
            return AutoRunResult("busy", "project_run_active", 0, project_id)
        try:
            return self._run_until_stop_claimed(
                session,
                project_id=project_id,
                limits=limits,
                should_stop=lambda: project_run_control.should_stop(project_id=project_id, run_id=run_id) or bool(should_stop and should_stop()),
                run_id=run_id,
            )
        finally:
            project_run_control.release(project_id=project_id, run_id=run_id)

    def _run_until_stop_claimed(
        self,
        session: Session,
        *,
        project_id: str,
        limits: AutoRunLimits | None,
        should_stop: Callable[[], bool] | None,
        run_id: str,
    ) -> AutoRunResult:
        limits = limits or AutoRunLimits()
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError("project not found")
        if project.status in {"COMPLETED", "FAILED", "CANCELLED", "FLAG_READY"}:
            return AutoRunResult(project.status.lower(), "project_terminal", 0, project_id)

        started_at = now_utc()
        deadline_at = limits.deadline_at
        if deadline_at is None and limits.max_minutes > 0:
            deadline_at = started_at + timedelta(minutes=limits.max_minutes)
        no_progress_count = 0
        recovery_intent_seeded = False
        events: list[dict[str, Any]] = []
        limits_payload = {
            **limits.__dict__,
            "deadline_at": deadline_at.isoformat() if deadline_at is not None else None,
        }
        self._event(session, project_id, "autorun.started", {"limits": limits_payload, "run_id": run_id})

        iteration = 0
        while limits.max_iterations <= 0 or iteration < limits.max_iterations:
            iteration += 1
            if should_stop and should_stop():
                self._event(session, project_id, "autorun.stopped", {"reason": "manual_stop", "iteration": iteration - 1})
                return AutoRunResult("stopped", "manual_stop", iteration - 1, project_id, events)
            project = session.get(Project, project_id)
            if project is None:
                return AutoRunResult("failed", "project_missing", iteration - 1, project_id, events)
            if project.status in {"COMPLETED", "FAILED", "CANCELLED", "FLAG_READY"}:
                self._event(session, project_id, "autorun.completed", {"iteration": iteration - 1, "project_status": project.status})
                reason = "candidate_ready" if project.status == "FLAG_READY" else "project_terminal"
                return AutoRunResult(project.status.lower(), reason, iteration - 1, project_id, events)
            if deadline_at is not None and now_utc() >= deadline_at:
                self._event(session, project_id, "autorun.stopped", {"reason": "max_minutes", "iteration": iteration - 1})
                return AutoRunResult("stopped", "max_minutes", iteration - 1, project_id, events)

            # The outer loop is deliberately a dispatcher, not another solver.
            # Keep the one hard safety gate (a recorded policy denial), but do
            # not let Observer route heuristics or Manager-authored advice run
            # in front of every model turn. Those previously stopped valid
            # resumed sessions before the Solver could use their checkpoints.
            policy_denial = self._latest_policy_denial(session, project_id) if limits.stop_on_observer_escalate else None
            if policy_denial is not None:
                payload = {
                    "reason": "observer_escalate",
                    "iteration": iteration - 1,
                    "observer_decision": policy_denial,
                }
                self._event(session, project_id, "autorun.blocked", payload)
                events.append(payload)
                return AutoRunResult("blocked", "observer_escalate", iteration - 1, project_id, events)

            manager_decision = None
            if not self._has_pending_intent(session, project_id):
                manager_decision = ManagerService().run_project(session, project_id=project_id)
                if not self._has_pending_intent(session, project_id) and not recovery_intent_seeded:
                    self._seed_fallback_intent(
                        session,
                        project_id=project_id,
                        phase=limits.phase or 1,
                        deadline_at=deadline_at,
                    )
                    recovery_intent_seeded = True
                    manager_decision = ManagerDecision("PROPOSED", "Scheduler seeded one fallback continuation intent to avoid an empty phase.")
                if not self._has_pending_intent(session, project_id):
                    self._event(session, project_id, "autorun.stopped", {"reason": "no_runnable_work", "iteration": iteration - 1})
                    return AutoRunResult("stopped", "no_runnable_work", iteration - 1, project_id, events)

            if deadline_at is not None and not self._clamp_pending_intents_to_deadline(
                session,
                project_id=project_id,
                phase=limits.phase,
                deadline_at=deadline_at,
            ):
                self._event(session, project_id, "autorun.stopped", {"reason": "max_minutes", "iteration": iteration - 1})
                return AutoRunResult("stopped", "max_minutes", iteration - 1, project_id, events)

            before = self._counts(session, project_id)
            self._event(session, project_id, "autorun.iteration.started", {"iteration": iteration})
            run_result = run_one_demo_step(session, project_id=project_id, run_id=run_id)
            after = self._counts(session, project_id)
            progress = self._progress(before, after)
            if progress["new_facts"] or progress["new_artifacts"] or progress["new_findings"]:
                no_progress_count = 0
            else:
                no_progress_count += 1

            payload = {
                "iteration": iteration,
                "observer": {"decision": "DEFERRED", "reason": "The Solver owns route selection inside this turn."},
                "manager": manager_decision.__dict__ if manager_decision is not None else {"status": "SKIPPED", "reason": "A runnable Solver intent already exists.", "proposed_intents": []},
                "run_result": run_result,
                "progress": progress,
                "no_progress_count": no_progress_count,
            }
            self._event(session, project_id, "autorun.iteration.completed", payload)
            events.append(payload)

            project = session.get(Project, project_id)
            if project is not None and project.status in {"COMPLETED", "FLAG_READY"}:
                self._event(session, project_id, "autorun.completed", {"iteration": iteration, "project_status": project.status})
                if project.status == "FLAG_READY":
                    return AutoRunResult("candidate_ready", "candidate_ready", iteration, project_id, events)
                return AutoRunResult("completed", "project_completed", iteration, project_id, events)
            if run_result.get("status") == "idle":
                self._event(session, project_id, "autorun.stopped", {"reason": "no_runnable_work", "iteration": iteration})
                return AutoRunResult("stopped", "no_runnable_work", iteration, project_id, events)
            if run_result.get("status") in {"runtime_error", "runtime_preflight_failed"}:
                self._event(session, project_id, "autorun.blocked", {"reason": "runtime_error", "iteration": iteration, "message": run_result.get("message")})
                return AutoRunResult("blocked", "runtime_error", iteration, project_id, events)
            if limits.no_progress_limit > 0 and no_progress_count >= limits.no_progress_limit:
                self._event(session, project_id, "autorun.blocked", {"reason": "no_progress", "iteration": iteration})
                return AutoRunResult("blocked", "no_progress", iteration, project_id, events)

        self._event(session, project_id, "autorun.stopped", {"reason": "max_iterations", "iterations": limits.max_iterations})
        return AutoRunResult("stopped", "max_iterations", limits.max_iterations, project_id, events)

    def step(self, session: Session, *, project_id: str) -> dict[str, Any]:
        claim = project_run_control.acquire(project_id=project_id, owner="autorun.step")
        if claim is None:
            return {"status": "busy", "reason": "project_run_active"}
        try:
            return self._step_claimed(session, project_id=project_id, run_id=claim.run_id)
        finally:
            project_run_control.release(project_id=project_id, run_id=claim.run_id)

    def _step_claimed(self, session: Session, *, project_id: str, run_id: str) -> dict[str, Any]:
        observer_decision = ObserverService().analyze_project(session, project_id=project_id)
        if observer_decision.decision in {"ESCALATE", "STOP", "PAUSE"}:
            payload = {"status": "blocked", "reason": "observer_escalate", "observer": observer_decision.__dict__}
            self._event(session, project_id, "autorun.blocked", payload)
            return payload
        manager_decision = ManagerService().run_project(session, project_id=project_id)
        run_result = run_one_demo_step(session, project_id=project_id, run_id=run_id)
        payload = {"status": "stepped", "observer": observer_decision.__dict__, "manager": manager_decision.__dict__, "run_result": run_result}
        self._event(session, project_id, "autorun.iteration.completed", payload)
        return payload

    def status(self, session: Session, *, project_id: str) -> dict[str, Any]:
        project = session.get(Project, project_id)
        latest = session.exec(
            select(WorkerEvent)
            .where(WorkerEvent.project_id == project_id, WorkerEvent.event_type.in_(["autorun.started", "autorun.stopped", "autorun.blocked", "autorun.completed"]))
            .order_by(WorkerEvent.created_at.desc())
        ).first()
        return {"project_status": project.status if project else "missing", "latest_autorun_event": latest}

    def _counts(self, session: Session, project_id: str) -> dict[str, int]:
        return {
            "facts": len(session.exec(select(Fact).where(Fact.project_id == project_id)).all()),
            "artifacts": len(
                session.exec(
                    select(Artifact).where(
                        Artifact.project_id == project_id,
                        Artifact.origin_kind.in_(["challenge_input", "target_observation", "operator_observation", "verified_derivation"]),
                    )
                ).all()
            ),
            "findings": len(session.exec(select(Finding).where(Finding.project_id == project_id)).all()),
            "attempts": len(session.exec(select(Intent).where(Intent.project_id == project_id, Intent.status.in_(["COMPLETED", "FAILED"]))).all()),
        }

    @staticmethod
    def _has_pending_intent(session: Session, project_id: str) -> bool:
        return session.exec(
            select(Intent).where(Intent.project_id == project_id, Intent.status == "PENDING")
        ).first() is not None

    @staticmethod
    def _seed_fallback_intent(
        session: Session,
        *,
        project_id: str,
        phase: int = 1,
        deadline_at: datetime | None = None,
    ) -> None:
        """Create one explicit continuation Intent when the Manager has none.

        An empty intent queue is not evidence of task completion.  Without this
        fallback, a model turn that produced no validated checkpoints consumed a
        scheduler phase and could retire a challenge without any runnable work.
        """
        project = session.get(Project, project_id)
        objective = (
            "Continue the current challenge using the retained artifacts, facts, "
            "checkpoints, and prior failure feedback; choose one different evidence-backed "
            "route and produce a reproducible result."
        )
        tags = ["blackboard.query", "codex.shell"]
        budget = {"model_role": "solver", "phase": phase, "max_tool_calls": 5, "max_route_repeats": 3, "finalize_grace_seconds": 60}
        if deadline_at is not None:
            budget["phase_deadline_at"] = deadline_at.isoformat()
        if project is not None and (project.target_url or project.challenge_type == "web"):
            tags.append("http.request")
        repository = BlackboardRepository()
        repository.upsert_intent(
            session,
            project_id=project_id,
            objective=objective,
            capability_tags=tags,
            priority=1.0,
            risk_level="low",
            budget=budget,
        )
        session.add(
            WorkerEvent(
                project_id=project_id,
                event_type="manager.fallback_intent_seeded",
                payload_json={"objective": objective, "capability_tags": tags, "phase": phase},
            )
        )
        session.commit()

    @classmethod
    def ensure_runnable_intent(
        cls,
        session: Session,
        *,
        project_id: str,
        phase: int,
        deadline_at: datetime | None,
    ) -> bool:
        """Materialize and budget work before a scarce target is allocated."""
        if not cls._has_pending_intent(session, project_id):
            ManagerService().run_project(session, project_id=project_id)
        if not cls._has_pending_intent(session, project_id):
            cls._seed_fallback_intent(
                session,
                project_id=project_id,
                phase=phase,
                deadline_at=deadline_at,
            )
        if not cls._has_pending_intent(session, project_id):
            return False
        if deadline_at is None:
            return True
        return cls._clamp_pending_intents_to_deadline(
            session,
            project_id=project_id,
            phase=phase,
            deadline_at=deadline_at,
        )

    @staticmethod
    def _clamp_pending_intents_to_deadline(
        session: Session,
        *,
        project_id: str,
        phase: int | None,
        deadline_at: datetime,
    ) -> bool:
        """Apply the remaining absolute phase budget to every new runnable Intent."""
        remaining_seconds = int((deadline_at - now_utc()).total_seconds())
        if remaining_seconds < 2:
            return False
        intents = session.exec(
            select(Intent).where(Intent.project_id == project_id, Intent.status == "PENDING")
        ).all()
        for intent in intents:
            budget = dict(intent.budget or {})
            configured_hard = budget.get("hard_timeout_seconds")
            try:
                hard_timeout = min(int(configured_hard), remaining_seconds) if configured_hard is not None else remaining_seconds
            except (TypeError, ValueError):
                hard_timeout = remaining_seconds
            hard_timeout = max(2, hard_timeout)
            configured_grace = budget.get("finalize_grace_seconds", 60)
            try:
                finalize_grace = max(1, min(int(configured_grace), hard_timeout - 1))
            except (TypeError, ValueError):
                finalize_grace = min(60, hard_timeout - 1)
            configured_soft = budget.get("soft_timeout_seconds")
            default_soft = max(1, hard_timeout - finalize_grace)
            try:
                soft_timeout = min(int(configured_soft), default_soft) if configured_soft is not None else default_soft
            except (TypeError, ValueError):
                soft_timeout = default_soft
            budget["hard_timeout_seconds"] = hard_timeout
            budget["soft_timeout_seconds"] = max(1, min(soft_timeout, hard_timeout - 1))
            budget["finalize_grace_seconds"] = finalize_grace
            budget["phase_deadline_at"] = deadline_at.isoformat()
            if phase is not None:
                budget["phase"] = phase
            intent.budget = budget
            intent.updated_at = now_utc()
            session.add(intent)
        session.commit()
        return bool(intents)

    @staticmethod
    def _latest_policy_denial(session: Session, project_id: str) -> dict[str, Any] | None:
        denial = session.exec(
            select(ToolTrace)
            .where(ToolTrace.project_id == project_id, ToolTrace.policy_decision == "deny")
            .order_by(ToolTrace.created_at.desc())
        ).first()
        if denial is None:
            return None
        return {
            "decision": "ESCALATE",
            "reason": f"Tool request was denied by policy: {denial.summary or denial.tool_name}",
            "severity": "high",
            "references": {"tool_trace_id": denial.id, "tool_name": denial.tool_name},
        }

    def _progress(self, before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
        return {
            "new_facts": after["facts"] - before["facts"],
            "new_artifacts": after["artifacts"] - before["artifacts"],
            "new_findings": after["findings"] - before["findings"],
            "new_attempts": after["attempts"] - before["attempts"],
        }

    def _event(self, session: Session, project_id: str, event_type: str, payload: dict[str, Any]) -> None:
        session.add(WorkerEvent(project_id=project_id, event_type=event_type, payload_json=payload))
        session.commit()
