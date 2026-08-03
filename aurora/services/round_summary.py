from __future__ import annotations

import json
import urllib.request
from typing import Any

from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.models import Attempt, AttemptCheckpoint, Fact, WorkerEvent


class RoundSummaryService:
    """Persist an evidence-bound handoff after every solver attempt."""

    def create(self, session: Session, *, attempt: Attempt, output: dict[str, Any], budget: dict[str, Any]) -> AttemptCheckpoint:
        existing = session.exec(select(AttemptCheckpoint).where(AttemptCheckpoint.attempt_id == attempt.id)).first()
        if existing is not None:
            return existing
        facts = session.exec(select(Fact).where(Fact.project_id == attempt.project_id, Fact.source_attempt_id == attempt.id)).all()
        parent = session.exec(
            select(AttemptCheckpoint)
            .where(AttemptCheckpoint.project_id == attempt.project_id)
            .order_by(AttemptCheckpoint.created_at.desc())
        ).first()
        fallback = self._fallback(attempt=attempt, output=output, facts=facts, budget=budget)
        planned = self._planner_summary(fallback)
        data = planned or fallback
        checkpoint = AttemptCheckpoint(
            project_id=attempt.project_id,
            intent_id=attempt.intent_id,
            worker_id=attempt.worker_id,
            attempt_id=attempt.id,
            parent_checkpoint_id=parent.id if parent else None,
            status=attempt.status,
            summary=data["summary"],
            conclusions=data["conclusions"],
            hypotheses=data["hypotheses"],
            failed_routes=data["failed_routes"],
            next_steps=data["next_steps"],
            fact_refs=[fact.id for fact in facts],
            artifact_refs=list(dict.fromkeys(attempt.artifact_refs)),
            budget_json=budget,
            source="planner" if planned else "fallback",
        )
        session.add(checkpoint)
        session.add(
            WorkerEvent(
                project_id=attempt.project_id,
                worker_id=attempt.worker_id,
                intent_id=attempt.intent_id,
                attempt_id=attempt.id,
                event_type="planner.round_summarized" if planned else "checkpoint.summary_fallback",
                payload_json={"checkpoint_id": checkpoint.id, "source": checkpoint.source, "fact_refs": checkpoint.fact_refs, "artifact_refs": checkpoint.artifact_refs},
            )
        )
        session.add(
            WorkerEvent(
                project_id=attempt.project_id,
                worker_id=attempt.worker_id,
                intent_id=attempt.intent_id,
                attempt_id=attempt.id,
                event_type="attempt.checkpoint_created",
                payload_json={"checkpoint_id": checkpoint.id, "status": checkpoint.status, "next_steps": checkpoint.next_steps},
            )
        )
        session.commit()
        session.refresh(checkpoint)
        return checkpoint

    def _fallback(self, *, attempt: Attempt, output: dict[str, Any], facts: list[Fact], budget: dict[str, Any]) -> dict[str, Any]:
        fact_lines = [fact.statement for fact in facts]
        return {
            "summary": str(output.get("summary") or attempt.result_summary or "Solver round completed without a summary."),
            "conclusions": fact_lines[:8],
            "hypotheses": [self._text(item) for item in output.get("hypotheses", [])[:6]],
            "failed_routes": [self._text(item) for item in output.get("failed_attempts", [])[:6]],
            "next_steps": self._next_steps(output),
        }

    def _planner_summary(self, fallback: dict[str, Any]) -> dict[str, Any] | None:
        settings = get_settings()
        if not settings.llm_api_key:
            return None
        prompt = {
            "task": "Summarize this solver round for the next solver. Preserve only evidence-backed conclusions; keep hypotheses separate. Return strict JSON with summary, conclusions, hypotheses, failed_routes, next_steps, each list capped at 6.",
            "round": fallback,
        }
        try:
            base = settings.llm_base_url.rstrip("/")
            url = f"{base}/chat/completions"
            payload = json.dumps({"model": settings.model_for_role("planner"), "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}], "response_format": {"type": "json_object"}}).encode()
            request = urllib.request.Request(url, data=payload, headers={"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=settings.llm_timeout_seconds) as response:
                body = json.loads(response.read())
            content = body["choices"][0]["message"]["content"]
            result = json.loads(content)
            if not isinstance(result, dict) or not isinstance(result.get("summary"), str):
                return None
            return {
                "summary": result["summary"][:2000],
                "conclusions": self._strings(result.get("conclusions")),
                "hypotheses": self._strings(result.get("hypotheses")),
                "failed_routes": self._strings(result.get("failed_routes")),
                "next_steps": self._strings(result.get("next_steps")),
            }
        except Exception:
            return None

    @staticmethod
    def _text(value: Any) -> str:
        return value.get("statement", "") if isinstance(value, dict) else str(value)

    def _next_steps(self, output: dict[str, Any]) -> list[str]:
        decision = output.get("decision_summary") if isinstance(output.get("decision_summary"), dict) else {}
        values = decision.get("next_tool_plan", []) if isinstance(decision, dict) else []
        return self._strings(values or output.get("suggested_intents", []))

    @staticmethod
    def _strings(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        return [str(value)[:1000] for value in values if isinstance(value, (str, int, float))][:6]
