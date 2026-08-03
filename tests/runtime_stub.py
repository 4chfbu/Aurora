from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlmodel import Session

from aurora.models import ContextSnapshot, LLMTrace, Worker
from aurora.services.worker_runtime import EXECUTABLE_TOOLS, RuntimeOutput


class TestWorkerRuntime:
    """Deterministic test double; never selectable through application config."""

    model = "test-worker-runtime"

    def execute(self, session: Session, *, worker: Worker, snapshot: ContextSnapshot) -> RuntimeOutput:
        intent = snapshot.sections_json.get("current_intent", {})
        objective = intent.get("objective", "unknown objective")
        selected_tool = self._select_tool(intent)
        tool_request = self._tool_request(intent, selected_tool)
        decision_summary = {
            "selected_intent": objective,
            "reason_summary": f"Test runtime selected {selected_tool} from the current intent.",
            "expected_information_gain": "medium",
            "risk_assessment": intent.get("risk_level", "low"),
            "next_tool_plan": [selected_tool],
        }
        structured = {
            "status": "partial",
            "summary": f"TestWorkerRuntime processed intent: {objective}",
            "fact_candidates": [{
                "statement": f"Intent was processed in intent-first mode: {objective}",
                "confidence": 0.7,
                "category": "execution",
                "evidence_refs": [],
            }],
            "hypotheses": [],
            "artifact_refs": [],
            "failed_attempts": [],
            "suggested_intents": [{
                "objective": "Review generated artifacts and decide the next concrete security hypothesis.",
                "capability_tags": ["blackboard.query"],
                "priority": 0.5,
                "risk_level": "low",
            }],
            "fork_recommendations": [],
            "candidate_flags": [],
            "decision_summary": decision_summary,
            "tool_requests": [{"tool_name": selected_tool, "request": tool_request}],
        }
        output_json = json.dumps(structured, ensure_ascii=False, sort_keys=True)
        prompt_json = json.dumps(snapshot.sections_json, ensure_ascii=False, sort_keys=True)
        trace = LLMTrace(
            project_id=snapshot.project_id,
            worker_id=worker.id,
            intent_id=worker.intent_id,
            context_snapshot_id=snapshot.id,
            prompt_hash=hashlib.sha256(prompt_json.encode("utf-8")).hexdigest(),
            model=self.model,
            input_chars=snapshot.total_chars,
            estimated_input_tokens=snapshot.estimated_tokens,
            output_chars=len(output_json),
            estimated_output_tokens=max(1, len(output_json) // 4),
            provider_usage_json={"test_double": True},
            decision_summary=decision_summary,
            structured_output=structured,
        )
        session.add(trace)
        session.commit()
        session.refresh(trace)
        return RuntimeOutput("partial", structured["summary"], structured, trace)

    @staticmethod
    def _select_tool(intent: dict[str, Any]) -> str:
        for tag in intent.get("capability_tags") or []:
            if tag in EXECUTABLE_TOOLS:
                return tag
        return "sandbox.exec"

    @staticmethod
    def _tool_request(intent: dict[str, Any], selected_tool: str) -> dict[str, Any]:
        explicit = intent.get("tool_request")
        if isinstance(explicit, dict) and explicit:
            return explicit
        if selected_tool == "sandbox.exec":
            return {
                "command": "printf 'Aurora Kali-first sandbox smoke test\\n'",
                "cwd": ".",
                "timeout_seconds": 5,
            }
        return {"cwd": ".", "timeout_seconds": 10}
