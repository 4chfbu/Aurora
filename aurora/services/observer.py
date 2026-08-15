from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from aurora.models import Artifact, Attempt, AttemptCheckpoint, Fact, ToolTrace, Worker, WorkerEvent
from aurora.services.blackboard_repository import route_fingerprint


@dataclass
class ObserverDecision:
    decision: str
    reason: str
    severity: str = "info"
    references: dict[str, Any] = field(default_factory=dict)


class ObserverService:
    def analyze_project(self, session: Session, *, project_id: str) -> ObserverDecision:
        tool_traces = session.exec(
            select(ToolTrace).where(ToolTrace.project_id == project_id).order_by(ToolTrace.created_at.desc()).limit(10)
        ).all()
        attempts = session.exec(
            select(Attempt).where(Attempt.project_id == project_id).order_by(Attempt.started_at.desc()).limit(5)
        ).all()

        decision = (
            self._find_policy_denial(tool_traces)
            or self._find_route_budget(session, project_id, tool_traces)
            or self._find_duplicate_tool_call(tool_traces)
            or self._find_no_evidence_attempt(attempts)
        )
        if decision is None:
            decision = ObserverDecision("CONTINUE", "No immediate repetition, authorization, or evidence issue detected.")

        session.add(
            WorkerEvent(
                project_id=project_id,
                event_type="observer.decision",
                payload_json={
                    "decision": decision.decision,
                    "severity": decision.severity,
                    "reason": decision.reason,
                    "references": decision.references,
                },
            )
        )
        session.commit()
        return decision

    def _find_route_budget(self, session: Session, project_id: str, tool_traces: list[ToolTrace]) -> ObserverDecision | None:
        codex = [trace for trace in tool_traces if trace.tool_name == "codex.shell"]
        worker = session.exec(select(Worker).where(Worker.project_id == project_id).order_by(Worker.created_at.desc())).first()
        budget = worker.budgets if worker else {}
        max_actions = int((budget or {}).get("max_agent_actions", 0) or 0)
        if max_actions and len(codex) >= max_actions:
            return ObserverDecision(
                "STOP",
                f"Codex internal action budget exhausted ({len(codex)}/{max_actions}); finalize from the latest evidence.",
                severity="high",
                references={"action_count": len(codex), "max_agent_actions": max_actions},
            )
        if len(codex) >= 2:
            fingerprints = [route_fingerprint(trace.request_json) for trace in codex]
            latest = fingerprints[0]
            streak = 0
            for fingerprint, trace in zip(fingerprints, codex):
                if fingerprint != latest:
                    break
                if trace.exit_code not in (None, 0):
                    streak += 1
                else:
                    break
            max_repeats = int((budget or {}).get("max_route_repeats", 2) or 2)
            if streak >= max_repeats:
                return ObserverDecision(
                    "REDIRECT",
                    "The same failing Codex shell route repeated; conclude or choose a materially different route.",
                    severity="high",
                    references={"route_fingerprint": latest, "failed_streak": streak, "max_route_repeats": max_repeats},
                )
        return None

    def _find_policy_denial(self, tool_traces: list[ToolTrace]) -> ObserverDecision | None:
        for trace in tool_traces:
            if trace.policy_decision == "deny":
                return ObserverDecision(
                    "ESCALATE",
                    f"Tool request was denied by policy: {trace.summary or trace.tool_name}",
                    severity="high",
                    references={"tool_trace_id": trace.id, "tool_name": trace.tool_name},
                )
        return None

    def _find_duplicate_tool_call(self, tool_traces: list[ToolTrace]) -> ObserverDecision | None:
        if len(tool_traces) < 2:
            return None
        latest = tool_traces[0]
        previous = tool_traces[1]
        if previous.tool_name == latest.tool_name and route_fingerprint(previous.request_json) == route_fingerprint(latest.request_json):
            return ObserverDecision(
                "REDIRECT",
                "The two latest tool routes are identical; redirect before repeating more work.",
                severity="medium",
                references={"tool_trace_ids": [latest.id, previous.id], "tool_name": latest.tool_name},
            )
        return None

    def _find_no_evidence_attempt(self, attempts: list[Attempt]) -> ObserverDecision | None:
        for attempt in attempts:
            if attempt.status in {"FAILED", "PARTIAL"} and not attempt.artifact_refs:
                return ObserverDecision(
                    "REQUEST_EVIDENCE",
                    "Recent attempt did not produce artifact evidence; require evidence or conclude failure explicitly.",
                    severity="medium",
                    references={"attempt_id": attempt.id, "status": attempt.status},
                )
        return None
