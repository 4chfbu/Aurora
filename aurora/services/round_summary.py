from __future__ import annotations

import json
from typing import Any

from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.services.llm_http import LLMRequestError, chat_completion
from pydantic import ValidationError

from aurora.models import Attempt, AttemptCheckpoint, Fact, Intent, Project, ProjectRuntimePolicy, WorkerEvent
from aurora.services.blackboard_repository import BlackboardRepository
from aurora.services.intent_dsl import IntentDSL
from aurora.services.mcp_registry import visible_mcp_tools
from aurora.services.project_coordination import ProjectCoordinationService


class RoundReflectionService:
    """Reflect on a primary solver round and author its follow-up intents."""

    def create(
        self,
        session: Session,
        *,
        attempt: Attempt,
        output: dict[str, Any],
        budget: dict[str, Any],
        skip_planner: bool = False,
        author_intents: bool = True,
    ) -> AttemptCheckpoint:
        existing = session.exec(select(AttemptCheckpoint).where(AttemptCheckpoint.attempt_id == attempt.id)).first()
        if existing is not None:
            return existing
        facts = session.exec(select(Fact).where(Fact.project_id == attempt.project_id, Fact.source_attempt_id == attempt.id)).all()
        parent = session.exec(
            select(AttemptCheckpoint)
            .where(AttemptCheckpoint.project_id == attempt.project_id)
            .order_by(AttemptCheckpoint.created_at.desc())
        ).first()
        fallback = self._fallback(session=session, attempt=attempt, output=output, facts=facts, budget=budget)
        planned = None if skip_planner else self._planner_summary(fallback)
        data = planned or fallback
        # Timeout recovery is operational control data. Preserve it when the
        # reflection model returns prose without a runnable continuation.
        if author_intents and not data.get("intents") and fallback.get("intents"):
            data["intents"] = fallback["intents"]
        if not data.get("next_steps") and fallback.get("next_steps"):
            data["next_steps"] = fallback["next_steps"]
        if author_intents and not data.get("intents") and data.get("next_steps") and attempt.status in {"PARTIAL", "FAILED", "TIMEOUT"}:
            current_intent = session.get(Intent, attempt.intent_id)
            next_step = str(data["next_steps"][0]).strip()
            if next_step:
                next_phase = min(4, int(budget.get("phase", 1) or 1) + 1)
                data["intents"] = [{
                    "objective": (
                        f"Run one evidence-backed continuation experiment: {next_step[:1200]}. "
                        "State the expected discriminating observation and save the result as a checkpoint."
                    ),
                    "capabilities": list(current_intent.capability_tags) if current_intent else ["blackboard.query", "sandbox.exec"],
                    "priority": 1.1,
                    "risk_level": "low",
                    "budget": {"model_role": "reviewer" if next_phase == 4 else "solver", "phase": next_phase},
                }]
        candidates = data.get("intents", [])
        handoff_limit = budget.get("max_handoff_intents")
        if isinstance(candidates, list) and isinstance(handoff_limit, int) and handoff_limit > 0:
            candidates = candidates[:handoff_limit]
        generated_intent_ids = (
            self._create_intents(session, attempt=attempt, candidates=candidates)
            if author_intents
            else []
        )
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
            generated_intent_ids=generated_intent_ids,
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
                event_type="reflection.completed" if planned else "reflection.fallback",
                payload_json={
                    "checkpoint_id": checkpoint.id,
                    "source": checkpoint.source,
                    "fact_refs": checkpoint.fact_refs,
                    "artifact_refs": checkpoint.artifact_refs,
                    "generated_intent_ids": checkpoint.generated_intent_ids,
                    "planner_error": getattr(self, "_last_planner_error", None),
                },
            )
        )
        session.add(
            WorkerEvent(
                project_id=attempt.project_id,
                worker_id=attempt.worker_id,
                intent_id=attempt.intent_id,
                attempt_id=attempt.id,
                event_type="attempt.checkpoint_created",
                payload_json={
                    "checkpoint_id": checkpoint.id,
                    "status": checkpoint.status,
                    "next_steps": checkpoint.next_steps,
                    "generated_intent_ids": checkpoint.generated_intent_ids,
                },
            )
        )
        session.commit()
        session.refresh(checkpoint)
        ProjectCoordinationService().record_graph_change(session, project_id=attempt.project_id)
        return checkpoint

    def _fallback(
        self,
        *,
        session: Session,
        attempt: Attempt,
        output: dict[str, Any],
        facts: list[Fact],
        budget: dict[str, Any],
    ) -> dict[str, Any]:
        fact_lines = [fact.statement for fact in facts]
        project = session.get(Project, attempt.project_id)
        current_intent = session.get(Intent, attempt.intent_id)
        recent = session.exec(
            select(AttemptCheckpoint)
            .where(AttemptCheckpoint.project_id == attempt.project_id)
            .order_by(AttemptCheckpoint.created_at.desc())
            .limit(3)
        ).all()
        active_facts = session.exec(
            select(Fact).where(Fact.project_id == attempt.project_id, Fact.status == "ACTIVE").limit(25)
        ).all()
        blockers = output.get("blockers") if isinstance(output.get("blockers"), list) else []
        blocker_steps = [
            str(item.get("next_step") or item.get("reason") or "").strip()
            for item in blockers[:4]
            if isinstance(item, dict) and str(item.get("next_step") or item.get("reason") or "").strip()
        ]
        return {
            "project_goal": project.goal if project else None,
            "current_intent": {
                "id": current_intent.id,
                "objective": current_intent.objective,
                "capability_tags": current_intent.capability_tags,
            } if current_intent else None,
            "attempt_status": attempt.status,
            "summary": str(output.get("summary") or attempt.result_summary or "Solver round completed without a summary."),
            "conclusions": fact_lines[:8],
            "hypotheses": [self._text(item) for item in output.get("hypotheses", [])[:6]],
            "failed_routes": [self._text(item) for item in output.get("failed_attempts", [])[:6]],
            "next_steps": list(dict.fromkeys([*self._next_steps(output), *blocker_steps]))[:6],
            "intents": output.get("suggested_intents", []) if isinstance(output.get("suggested_intents"), list) else [],
            "active_facts": [
                {"id": fact.id, "statement": fact.statement, "evidence_items": fact.evidence_items}
                for fact in active_facts
            ],
            "artifact_refs": list(dict.fromkeys(attempt.artifact_refs)),
            "recent_reflections": [
                {"summary": checkpoint.summary, "failed_routes": checkpoint.failed_routes, "next_steps": checkpoint.next_steps}
                for checkpoint in recent
            ],
            "budget": budget,
        }

    def _planner_summary(self, fallback: dict[str, Any]) -> dict[str, Any] | None:
        settings = get_settings()
        if not settings.llm_api_key:
            self._last_planner_error = None
            return None
        prompt = {
            "task": (
                "Reflect on this solver round for the next solver. Preserve only evidence-backed conclusions; "
                "keep hypotheses separate. Propose at most 3 concrete follow-up intents. Return strict JSON with "
                "summary, conclusions, hypotheses, failed_routes, next_steps, and intents. Each intent uses "
                "objective, capabilities, depends_on_facts, priority, risk_level, and tool_request."
            ),
            "round": fallback,
        }
        try:
            body = chat_completion(
                settings=settings,
                model=settings.model_for_role("reviewer"),
                messages=[{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
                timeout=settings.llm_timeout_seconds,
            )
            content = body["choices"][0]["message"]["content"]
            result = json.loads(content)
        except LLMRequestError as exc:
            self._last_planner_error = str(exc)
            return None
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            self._last_planner_error = f"planner response parse error: {type(exc).__name__}: {exc}"
            return None

        required_lists = ("conclusions", "hypotheses", "failed_routes", "next_steps", "intents")
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("summary"), str)
            or any(not isinstance(result.get(key), list) for key in required_lists)
        ):
            self._last_planner_error = "planner response did not match expected JSON schema"
            return None
        self._last_planner_error = None
        return {
            "summary": result["summary"][:2000],
            "conclusions": self._strings(result.get("conclusions")),
            "hypotheses": self._strings(result.get("hypotheses")),
            "failed_routes": self._strings(result.get("failed_routes")),
            "next_steps": self._strings(result.get("next_steps")),
            "intents": result.get("intents", []) if isinstance(result.get("intents"), list) else [],
        }

    @staticmethod
    def _text(value: Any) -> str:
        return value.get("statement", "") if isinstance(value, dict) else str(value)

    def _next_steps(self, output: dict[str, Any]) -> list[str]:
        decision = output.get("decision_summary") if isinstance(output.get("decision_summary"), dict) else {}
        values = decision.get("next_tool_plan", []) if isinstance(decision, dict) else []
        if values:
            return self._strings(values)
        suggestions = output.get("suggested_intents", [])
        if not isinstance(suggestions, list):
            return []
        return self._strings([
            item.get("objective", "") if isinstance(item, dict) else item
            for item in suggestions
        ])

    def _create_intents(self, session: Session, *, attempt: Attempt, candidates: Any) -> list[str]:
        project = session.get(Project, attempt.project_id)
        if project is None or project.status == "COMPLETED" or not isinstance(candidates, list):
            return []
        settings = get_settings()
        allowed_capabilities = {tool["name"] for tool in visible_mcp_tools(settings, allow_subagents=False, contract=None)}
        fact_ids = set(session.exec(select(Fact.id).where(Fact.project_id == attempt.project_id, Fact.status == "ACTIVE")).all())
        repository = BlackboardRepository()
        generated: list[str] = []
        policy = session.exec(
            select(ProjectRuntimePolicy).where(ProjectRuntimePolicy.project_id == attempt.project_id)
        ).first()
        multi_agent_enabled = bool(policy and policy.multi_agent_exploration_enabled)
        max_reason_intents = policy.max_reason_intents if multi_agent_enabled else 3
        if multi_agent_enabled:
            active_count = len(session.exec(
                select(Intent).where(
                    Intent.project_id == attempt.project_id,
                    Intent.status.in_(["PENDING", "RUNNING", "CONCLUDING"]),
                )
            ).all())
            available = max(0, policy.max_pending_intents - active_count)
        else:
            available = 3
        for candidate in candidates[:min(max_reason_intents, available)]:
            if not isinstance(candidate, dict):
                continue
            raw_budget = candidate.get("budget") if isinstance(candidate.get("budget"), dict) else {}
            capabilities = candidate.get("capabilities", candidate.get("capability_tags", []))
            depends_on = candidate.get("depends_on_facts", candidate.get("dependency_fact_ids", []))
            objective = candidate.get("objective", "")
            expected = str(candidate.get("expected_observation") or "").strip()
            if isinstance(objective, str) and expected:
                objective = f"{objective.rstrip()} Expected discriminating observation: {expected[:800]}"
            try:
                intent = IntentDSL(
                    objective=objective,
                    capabilities=capabilities if isinstance(capabilities, list) else [],
                    depends_on_facts=depends_on if isinstance(depends_on, list) else [],
                    priority=candidate.get("priority", 1.0),
                    risk_level=candidate.get("risk_level", "low"),
                    tool_request=candidate.get("tool_request", raw_budget.get("tool_request", {})),
                )
            except ValidationError:
                continue
            if not intent.capabilities or any(capability not in allowed_capabilities for capability in intent.capabilities):
                continue
            dependency_fact_ids = [fact_id for fact_id in intent.depends_on_facts if fact_id in fact_ids]
            budget = self._intent_budget(raw_budget, intent.tool_request)
            result = repository.upsert_intent(
                session,
                project_id=attempt.project_id,
                objective=intent.objective,
                capability_tags=intent.capabilities,
                dependency_fact_ids=dependency_fact_ids,
                parent_intent_id=attempt.intent_id,
                priority=intent.priority,
                risk_level=intent.risk_level,
                budget=budget,
            )
            if result.item.id not in generated:
                generated.append(result.item.id)
        return generated

    @staticmethod
    def _intent_budget(raw_budget: dict[str, Any], tool_request: dict[str, Any]) -> dict[str, Any]:
        """Keep only bounded scheduler budget fields from an untrusted proposal."""
        budget: dict[str, Any] = {}
        if tool_request:
            budget["tool_request"] = tool_request
        model_role = raw_budget.get("model_role")
        if model_role in {"triage", "solver", "planner", "reviewer"}:
            budget["model_role"] = model_role
        for key in ("phase", "soft_timeout_seconds", "hard_timeout_seconds", "max_tool_calls", "max_agent_actions", "max_route_repeats", "finalize_grace_seconds", "token_budget"):
            value = raw_budget.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                budget[key] = value
        phase = budget.get("phase")
        if phase is not None:
            budget["model_role"] = "triage" if phase == 1 else "reviewer" if phase >= 4 else "solver"
        repeat_failures = raw_budget.get("max_repeat_failures")
        if isinstance(repeat_failures, int) and not isinstance(repeat_failures, bool) and repeat_failures >= 0:
            budget["max_repeat_failures"] = repeat_failures
        return budget

    @staticmethod
    def _strings(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        return [str(value)[:1000] for value in values if isinstance(value, (str, int, float))][:6]


# Backwards-compatible import for callers outside the application package.
RoundSummaryService = RoundReflectionService
