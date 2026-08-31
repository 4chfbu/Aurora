from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError
from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.models import Fact, Intent, Project, ProjectRuntimePolicy, WorkerEvent
from aurora.services.blackboard_repository import BlackboardRepository
from aurora.services.intent_dsl import IntentDSL
from aurora.services.llm_http import LLMRequestError, chat_completion
from aurora.services.mcp_registry import visible_mcp_tools
from aurora.services.project_coordination import ProjectCoordinationService


class ProjectReasoner:
    """Run one Cairn-style Reason pass for each newly committed graph state."""

    def run(self, session: Session, *, project_id: str) -> dict[str, Any]:
        settings = get_settings()
        policy = session.exec(
            select(ProjectRuntimePolicy).where(ProjectRuntimePolicy.project_id == project_id)
        ).first()
        if not (
            settings.multi_agent_exploration_enabled
            and policy is not None
            and policy.multi_agent_exploration_enabled
        ):
            return {"status": "disabled", "created_intent_ids": []}

        facts = session.exec(
            select(Fact).where(Fact.project_id == project_id, Fact.status == "ACTIVE").order_by(Fact.created_at)
        ).all()
        # Preserve the bootstrap fast path. The first solver gets an uncluttered
        # attempt before the graph starts fanning out.
        if not facts:
            return {"status": "waiting_for_first_fact", "created_intent_ids": []}

        coordination = ProjectCoordinationService()
        claim = coordination.claim_reason(
            session,
            project_id=project_id,
            lease_seconds=max(120, settings.llm_timeout_seconds * 3 + 30),
        )
        if claim is None:
            return {"status": "unchanged", "created_intent_ids": []}
        owner, graph_version = claim
        session.add(
            WorkerEvent(
                project_id=project_id,
                event_type="reason.started",
                payload_json={"owner": owner, "graph_version": graph_version},
            )
        )
        session.commit()

        try:
            facts = session.exec(
                select(Fact).where(Fact.project_id == project_id, Fact.status == "ACTIVE").order_by(Fact.created_at)
            ).all()
            created = self._reason(session, project_id=project_id, policy=policy, facts=facts)
            coordination.finish_reason(
                session,
                project_id=project_id,
                owner=owner,
                reasoned_version=graph_version,
            )
            session.add(
                WorkerEvent(
                    project_id=project_id,
                    event_type="reason.completed",
                    payload_json={
                        "owner": owner,
                        "graph_version": graph_version,
                        "created_intent_ids": created,
                        "status": "proposed" if created else "noop",
                    },
                )
            )
            session.commit()
            return {"status": "proposed" if created else "noop", "created_intent_ids": created}
        except Exception as exc:
            # A failed planning pass must not spin on every dispatcher tick.
            # A later Fact/Hint increments graph_version and makes it eligible again.
            coordination.finish_reason(
                session,
                project_id=project_id,
                owner=owner,
                reasoned_version=graph_version,
            )
            session.add(
                WorkerEvent(
                    project_id=project_id,
                    event_type="reason.fallback",
                    payload_json={"owner": owner, "graph_version": graph_version, "error": str(exc)[:500]},
                )
            )
            session.commit()
            return {"status": "fallback", "created_intent_ids": [], "error": str(exc)[:500]}

    def _reason(
        self,
        session: Session,
        *,
        project_id: str,
        policy: ProjectRuntimePolicy,
        facts: list[Fact],
    ) -> list[str]:
        settings = get_settings()
        project = session.get(Project, project_id)
        if project is None or not settings.llm_api_key:
            return []
        open_intents = session.exec(
            select(Intent).where(
                Intent.project_id == project_id,
                Intent.status.in_(["PENDING", "RUNNING", "CONCLUDING"]),
            )
        ).all()
        available = max(0, policy.max_pending_intents - len(open_intents))
        limit = min(policy.max_reason_intents, available)
        if limit <= 0:
            return []
        prompt = {
            "task": (
                "Read the shared exploration graph and propose only new, high-value, non-overlapping, "
                "parallelizable exploration directions. Existing open intents must not be duplicated. "
                f"Return JSON {{\"intents\": [...]}} with at most {limit} items. Each item has objective, "
                "capabilities, depends_on_facts, priority, and risk_level. Return an empty list when existing "
                "work already covers the useful directions. Do not include commands or chain-of-thought."
            ),
            "goal": project.goal,
            "facts": [
                {"id": fact.id, "statement": fact.statement, "category": fact.category, "confidence": fact.confidence}
                for fact in facts[-100:]
            ],
            "open_intents": [
                {"id": intent.id, "objective": intent.objective, "status": intent.status}
                for intent in open_intents
            ],
        }
        body = chat_completion(
            settings=settings,
            model=settings.model_for_role("planner"),
            messages=[{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
            timeout=settings.llm_timeout_seconds,
        )
        try:
            payload = json.loads(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMRequestError(f"reason response parse error: {exc}") from exc
        candidates = payload.get("intents", []) if isinstance(payload, dict) else []
        if not isinstance(candidates, list):
            raise LLMRequestError("reason response intents must be a list")

        allowed = {tool["name"] for tool in visible_mcp_tools(settings, allow_subagents=False)}
        fact_ids = {fact.id for fact in facts}
        repository = BlackboardRepository()
        created: list[str] = []
        for candidate in candidates[:limit]:
            if not isinstance(candidate, dict):
                continue
            try:
                intent = IntentDSL(
                    objective=candidate.get("objective", ""),
                    capabilities=candidate.get("capabilities", []),
                    depends_on_facts=candidate.get("depends_on_facts", []),
                    priority=candidate.get("priority", 1.0),
                    risk_level=candidate.get("risk_level", "low"),
                )
            except ValidationError:
                continue
            if not intent.capabilities or any(capability not in allowed for capability in intent.capabilities):
                continue
            result = repository.upsert_intent(
                session,
                project_id=project_id,
                objective=intent.objective,
                capability_tags=intent.capabilities,
                dependency_fact_ids=[fact_id for fact_id in intent.depends_on_facts if fact_id in fact_ids],
                priority=intent.priority,
                risk_level=intent.risk_level,
                budget={"model_role": "solver", "phase": 2},
            )
            if result.created:
                created.append(result.item.id)
        return created
