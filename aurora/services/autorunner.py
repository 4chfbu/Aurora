from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from collections.abc import Callable
from typing import Any

from sqlmodel import Session, select

from aurora.models import Artifact, Fact, Finding, Intent, Project, ToolTrace, WorkerEvent, now_utc
from aurora.services.demo import run_one_demo_step
from aurora.services.manager import ManagerService
from aurora.services.observer import ObserverService
from aurora.services.blackboard_repository import stable_json


@dataclass
class AutoRunLimits:
    max_iterations: int = 20
    max_minutes: int = 0
    no_progress_limit: int = 4
    stop_on_observer_escalate: bool = True


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
    ) -> AutoRunResult:
        limits = limits or AutoRunLimits()
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError("project not found")
        if project.status in {"COMPLETED", "FAILED", "CANCELLED", "FLAG_READY"}:
            return AutoRunResult(project.status.lower(), "project_terminal", 0, project_id)

        started_at = now_utc()
        no_progress_count = 0
        events: list[dict[str, Any]] = []
        self._event(session, project_id, "autorun.started", {"limits": limits.__dict__})

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
            if limits.max_minutes > 0 and now_utc() - started_at > timedelta(minutes=limits.max_minutes):
                self._event(session, project_id, "autorun.stopped", {"reason": "max_minutes", "iteration": iteration - 1})
                return AutoRunResult("stopped", "max_minutes", iteration - 1, project_id, events)

            before = self._counts(session, project_id)
            self._event(session, project_id, "autorun.iteration.started", {"iteration": iteration})

            observer_decision = ObserverService().analyze_project(session, project_id=project_id)
            if limits.stop_on_observer_escalate and observer_decision.decision in {"ESCALATE", "STOP", "PAUSE"}:
                payload = {
                    "reason": "observer_escalate",
                    "iteration": iteration,
                    "observer_decision": observer_decision.__dict__,
                }
                self._event(session, project_id, "autorun.blocked", payload)
                events.append(payload)
                return AutoRunResult("blocked", "observer_escalate", iteration, project_id, events)
            if (
                limits.no_progress_limit > 0
                and observer_decision.decision == "REDIRECT"
                and self._duplicate_streak(session, project_id) >= max(2, limits.no_progress_limit)
            ):
                payload = {
                    "reason": "duplicate_tool_streak",
                    "iteration": iteration,
                    "observer_decision": observer_decision.__dict__,
                }
                self._event(session, project_id, "autorun.blocked", payload)
                events.append(payload)
                return AutoRunResult("blocked", "duplicate_tool_streak", iteration, project_id, events)

            manager_decision = ManagerService().run_project(session, project_id=project_id)
            run_result = run_one_demo_step(session, project_id=project_id)
            after = self._counts(session, project_id)
            progress = self._progress(before, after)
            if progress["new_facts"] or progress["new_artifacts"] or progress["new_findings"]:
                no_progress_count = 0
            else:
                no_progress_count += 1

            payload = {
                "iteration": iteration,
                "observer": observer_decision.__dict__,
                "manager": manager_decision.__dict__,
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
            if run_result.get("status") == "idle" and manager_decision.status == "NOOP":
                self._event(session, project_id, "autorun.stopped", {"reason": "no_runnable_work", "iteration": iteration})
                return AutoRunResult("stopped", "no_runnable_work", iteration, project_id, events)
            if run_result.get("status") == "runtime_error":
                self._event(session, project_id, "autorun.blocked", {"reason": "runtime_error", "iteration": iteration, "message": run_result.get("message")})
                return AutoRunResult("blocked", "runtime_error", iteration, project_id, events)
            if limits.no_progress_limit > 0 and no_progress_count >= limits.no_progress_limit:
                self._event(session, project_id, "autorun.blocked", {"reason": "no_progress", "iteration": iteration})
                return AutoRunResult("blocked", "no_progress", iteration, project_id, events)

        self._event(session, project_id, "autorun.stopped", {"reason": "max_iterations", "iterations": limits.max_iterations})
        return AutoRunResult("stopped", "max_iterations", limits.max_iterations, project_id, events)

    def step(self, session: Session, *, project_id: str) -> dict[str, Any]:
        observer_decision = ObserverService().analyze_project(session, project_id=project_id)
        if observer_decision.decision in {"ESCALATE", "STOP", "PAUSE"}:
            payload = {"status": "blocked", "reason": "observer_escalate", "observer": observer_decision.__dict__}
            self._event(session, project_id, "autorun.blocked", payload)
            return payload
        manager_decision = ManagerService().run_project(session, project_id=project_id)
        run_result = run_one_demo_step(session, project_id=project_id)
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
            "artifacts": len(session.exec(select(Artifact).where(Artifact.project_id == project_id, Artifact.type != "codex-transcript")).all()),
            "findings": len(session.exec(select(Finding).where(Finding.project_id == project_id)).all()),
            "attempts": len(session.exec(select(Intent).where(Intent.project_id == project_id, Intent.status.in_(["COMPLETED", "FAILED"]))).all()),
        }

    def _progress(self, before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
        return {
            "new_facts": after["facts"] - before["facts"],
            "new_artifacts": after["artifacts"] - before["artifacts"],
            "new_findings": after["findings"] - before["findings"],
            "new_attempts": after["attempts"] - before["attempts"],
        }

    def _duplicate_streak(self, session: Session, project_id: str) -> int:
        traces = session.exec(
            select(ToolTrace).where(ToolTrace.project_id == project_id).order_by(ToolTrace.created_at.desc()).limit(10)
        ).all()
        if len(traces) < 2:
            return 0
        latest = traces[0]
        latest_request = stable_json(latest.request_json)
        streak = 0
        for trace in traces:
            if trace.tool_name != latest.tool_name or stable_json(trace.request_json) != latest_request:
                break
            streak += 1
        return streak

    def _event(self, session: Session, project_id: str, event_type: str, payload: dict[str, Any]) -> None:
        session.add(WorkerEvent(project_id=project_id, event_type=event_type, payload_json=payload))
        session.commit()
