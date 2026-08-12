from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from aurora.models import Attempt, ToolTrace, WorkerEvent
from aurora.services.blackboard_repository import stable_json


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

        decision = self._find_policy_denial(tool_traces) or self._find_duplicate_tool_call(tool_traces) or self._find_no_evidence_attempt(attempts)
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
        if previous.tool_name == latest.tool_name and stable_json(previous.request_json) == stable_json(latest.request_json):
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
