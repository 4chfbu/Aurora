from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import ValidationError, field_validator
from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.models import (
    Artifact,
    AttemptCheckpoint,
    ChallengeGroupItem,
    Fact,
    Intent,
    Project,
    ProjectRuntimePolicy,
    WorkerEvent,
)
from aurora.services.blackboard_repository import BlackboardRepository, normalize_text
from aurora.services.intent_dsl import IntentDSL
from aurora.services.llm_http import LLMRequestError, chat_completion
from aurora.services.mcp_registry import scheduling_capabilities
from aurora.services.blackboard_repository import normalize_fact_category
from aurora.services.project_coordination import ProjectCoordinationService
from aurora.services.flag_validator import FlagValidator
from aurora.services.solver_playbooks import select_playbook
from aurora.services.agent_runtime import agent_runtime_settings
from aurora.services.deadlines import execution_deadline


class PlannerIntentDSL(IntentDSL):
    """Accept unambiguous legacy model priorities without weakening the API DSL."""

    @field_validator("priority", mode="before")
    @classmethod
    def normalize_priority(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("priority must be a number, not a boolean")
        if isinstance(value, str):
            value = value.strip()
            return {"low": 1.0, "medium": 2.0, "high": 3.0}.get(value.lower(), value)
        return value


class ProjectReasoner:
    """Run one Cairn-style Reason pass for each newly committed graph state."""

    def run(self, session: Session, *, project_id: str, phase: int | None = None, deadline_at: datetime | None = None) -> dict[str, Any]:
        settings = get_settings()
        runtime = agent_runtime_settings(session)
        policy = session.exec(
            select(ProjectRuntimePolicy).where(ProjectRuntimePolicy.project_id == project_id)
        ).first()
        if not (
            runtime.multi_agent_exploration_enabled
            and policy is not None
            and policy.multi_agent_exploration_enabled
        ):
            return {"status": "disabled", "created_intent_ids": []}

        bootstrap = session.exec(
            select(Intent).where(Intent.project_id == project_id).order_by(Intent.created_at)
        ).first()
        if bootstrap is None or bootstrap.status not in {"COMPLETED", "FAILED", "TIMEOUT", "CANCELLED"}:
            return {"status": "waiting_for_bootstrap", "created_intent_ids": []}

        facts = session.exec(
            select(Fact).where(Fact.project_id == project_id, Fact.status == "ACTIVE").order_by(Fact.created_at)
        ).all()
        checkpoint = session.exec(
            select(AttemptCheckpoint)
            .where(AttemptCheckpoint.project_id == project_id)
            .order_by(AttemptCheckpoint.created_at.desc())
        ).first()
        # Preserve the bootstrap fast path. The first solver gets an uncluttered
        # attempt before the graph starts fanning out. A checkpoint without a
        # fact is still useful graph state and must be allowed to trigger the
        # next Reason pass.
        if not facts and checkpoint is None:
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
            created = self._reason(session, project_id=project_id, policy=policy, facts=facts, phase=phase, deadline_at=deadline_at)
            details = getattr(self, "_last_reason_details", {})
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
                        "details": details,
                    },
                )
            )
            session.commit()
            return {
                "status": "proposed" if created else "noop",
                "created_intent_ids": created,
                "details": details,
            }
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
        phase: int | None = None,
        deadline_at: datetime | None = None,
    ) -> list[str]:
        settings = get_settings()
        project = session.get(Project, project_id)
        if project is None:
            return []
        open_intents = session.exec(
            select(Intent).where(
                Intent.project_id == project_id,
                Intent.status.in_(["PENDING", "RUNNING", "CONCLUDING"]),
            )
        ).all()
        intent_history = session.exec(
            select(Intent).where(Intent.project_id == project_id).order_by(Intent.created_at.desc()).limit(50)
        ).all()
        checkpoints = session.exec(
            select(AttemptCheckpoint)
            .where(AttemptCheckpoint.project_id == project_id)
            .order_by(AttemptCheckpoint.created_at.desc())
            .limit(12)
        ).all()
        artifacts = session.exec(
            select(Artifact)
            .where(Artifact.project_id == project_id)
            .order_by(Artifact.created_at.desc())
            .limit(20)
        ).all()
        available = max(0, policy.max_pending_intents - len(open_intents))
        parallel_slots = max(0, policy.max_parallel_explorers - len(open_intents))
        limit = min(policy.max_reason_intents, available, parallel_slots)
        if limit <= 0:
            self._last_reason_details = {
                "requested_intents": 0,
                "open_intents": len(open_intents),
                "reason": "parallel_slots_full",
            }
            return []
        group_item = session.exec(
            select(ChallengeGroupItem)
            .where(ChallengeGroupItem.project_id == project_id)
            .order_by(ChallengeGroupItem.updated_at.desc())
        ).first()
        current_phase = self._current_phase(
            open_intents,
            checkpoints,
            preferred_phase=phase if phase is not None else (group_item.phase if group_item is not None else None),
        )
        allowed = scheduling_capabilities(settings)
        playbook = select_playbook(
            project.challenge_type,
            " ".join([project.goal, *(fact.statement for fact in facts[-20:])]),
        )
        default_capabilities = [
            capability
            for capability in dict.fromkeys([*playbook.capabilities, "blackboard.query"])
            if capability in allowed
        ]
        prompt = {
            "task": (
                "Read the shared exploration graph and propose only new, high-value, non-overlapping, "
                "parallelizable exploration directions. Fill the available peer slots while the project is active; "
                "do not return an empty list merely because one broad bootstrap intent already exists. Existing open "
                "or completed routes must not be duplicated. "
                f"Return JSON {{\"intents\": [...]}} with exactly {limit} item(s) when that many distinct routes remain. Each item has objective, "
                "capabilities, depends_on_facts, priority, and risk_level. Follow intent_schema: priority is a "
                "JSON number from 0 to 100 (default 1.0; larger means earlier), not a label such as high. "
                "risk_level is low, medium, or high. Return an empty list when existing "
                "work truly covers every useful direction. Assign one falsifiable experiment per item and include how "
                "its result complements the other branches. Do not include commands or chain-of-thought."
            ),
            "goal": project.goal,
            "challenge_type": project.challenge_type,
            "current_phase": current_phase,
            "intent_schema": IntentDSL.model_json_schema(),
            "allowed_capabilities": sorted(allowed),
            "default_capabilities": default_capabilities,
            "facts": [
                {"id": fact.id, "statement": fact.statement, "category": fact.category, "confidence": fact.confidence}
                for fact in facts[-100:]
            ],
            "open_intents": [
                {"id": intent.id, "objective": intent.objective, "status": intent.status}
                for intent in open_intents
            ],
            "completed_or_failed_routes": [
                {"id": intent.id, "objective": intent.objective, "status": intent.status}
                for intent in intent_history
                if intent.status in {"COMPLETED", "FAILED", "TIMEOUT", "CANCELLED"}
            ],
            "recent_checkpoints": [
                {
                    "summary": checkpoint.summary,
                    "conclusions": checkpoint.conclusions,
                    "hypotheses": checkpoint.hypotheses,
                    "failed_routes": checkpoint.failed_routes,
                    "next_steps": checkpoint.next_steps,
                    "fact_refs": checkpoint.fact_refs,
                    "artifact_refs": checkpoint.artifact_refs,
                }
                for checkpoint in checkpoints
            ],
            "recent_artifacts": [
                {"id": artifact.id, "type": artifact.type, "summary": artifact.summary}
                for artifact in artifacts
                if artifact.type not in {"codex-transcript", "subagent-transcript"}
            ],
        }
        planner_error: str | None = None
        planner_response_valid = False
        candidates: list[Any] = []
        if settings.llm_api_key:
            try:
                body = chat_completion(
                    settings=settings,
                    model=settings.model_for_role("planner"),
                    messages=[{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
                    timeout=settings.llm_timeout_seconds,
                    deadline_at=deadline_at or execution_deadline(session, project_id),
                )
                payload = json.loads(body["choices"][0]["message"]["content"])
                if not isinstance(payload, dict) or "intents" not in payload:
                    raise LLMRequestError("reason response must contain intents")
                parsed = payload["intents"]
                if not isinstance(parsed, list):
                    raise LLMRequestError("reason response intents must be a list")
                candidates = parsed
                planner_response_valid = True
            except (LLMRequestError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                planner_error = f"{type(exc).__name__}: {exc}"[:500]
        else:
            planner_error = "planner API key is not configured"

        fact_ids = {fact.id for fact in facts}
        historical_objectives = {normalize_text(intent.objective) for intent in intent_history}
        repository = BlackboardRepository()
        created: list[str] = []
        accepted_objectives: set[str] = set()
        created.extend(self._create_candidates(
            session, project_id=project_id,
            candidates=self._continuation_candidates(session, facts=facts, checkpoints=checkpoints),
            limit=min(1, limit), current_phase=current_phase, allowed=allowed,
            default_capabilities=default_capabilities, fact_ids=fact_ids,
            historical_objectives=historical_objectives, accepted_objectives=accepted_objectives,
            repository=repository,
        ))
        model_candidate_rejections: list[dict[str, Any]] = []
        model_created = self._create_candidates(
            session,
            project_id=project_id,
            candidates=candidates,
            limit=limit - len(created),
            current_phase=current_phase,
            allowed=allowed,
            default_capabilities=default_capabilities,
            fact_ids=fact_ids,
            historical_objectives=historical_objectives,
            accepted_objectives=accepted_objectives,
            repository=repository,
            rejection_diagnostics=model_candidate_rejections,
        )
        created.extend(model_created)
        fallback_created: list[str] = []
        contract_rejections = {"candidate_not_object", "validation_error"}
        if any(item["reason"] in contract_rejections for item in model_candidate_rejections):
            planner_response_valid = False
            planner_error = planner_error or "reason response contains invalid intent candidates"
        if not planner_response_valid and len(created) < limit:
            fallback_created = self._create_candidates(
                session,
                project_id=project_id,
                candidates=self._fallback_candidates(project, facts=facts, checkpoints=checkpoints),
                limit=limit - len(created),
                current_phase=current_phase,
                allowed=allowed,
                default_capabilities=default_capabilities,
                fact_ids=fact_ids,
                historical_objectives=historical_objectives,
                accepted_objectives=accepted_objectives,
                repository=repository,
            )
            created.extend(fallback_created)
        self._last_reason_details = {
            "requested_intents": limit,
            "open_intents": len(open_intents),
            "model_candidates": len(candidates),
            "model_created": len(model_created),
            "fallback_created": len(fallback_created),
            "current_phase": current_phase,
            "planner_error": planner_error,
            "model_candidate_rejections": model_candidate_rejections,
        }
        return created

    @staticmethod
    def _current_phase(
        open_intents: list[Intent],
        checkpoints: list[AttemptCheckpoint],
        *,
        preferred_phase: int | None = None,
    ) -> int:
        if preferred_phase is not None:
            try:
                return max(1, min(4, int(preferred_phase)))
            except (TypeError, ValueError):
                pass
        values: list[int] = []
        for budget in [*(intent.budget or {} for intent in open_intents), *(item.budget_json or {} for item in checkpoints)]:
            try:
                values.append(int(budget.get("phase", 1) or 1))
            except (TypeError, ValueError):
                continue
        return max(1, min(4, max(values, default=1)))

    @staticmethod
    def _create_candidates(
        session: Session,
        *,
        project_id: str,
        candidates: list[Any],
        limit: int,
        current_phase: int,
        allowed: set[str],
        default_capabilities: list[str],
        fact_ids: set[str],
        historical_objectives: set[str],
        accepted_objectives: set[str],
        repository: BlackboardRepository,
        rejection_diagnostics: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        created: list[str] = []
        for index, candidate in enumerate(candidates):
            if len(created) >= limit:
                break
            if not isinstance(candidate, dict):
                if rejection_diagnostics is not None:
                    rejection_diagnostics.append({"index": index, "reason": "candidate_not_object"})
                continue
            raw_capabilities = candidate.get("capabilities", [])
            capabilities = (
                list(
                    dict.fromkeys(
                        str(capability) for capability in raw_capabilities if str(capability) in allowed
                    )
                )
                if isinstance(raw_capabilities, list)
                else []
            )
            if not capabilities:
                capabilities = default_capabilities
            try:
                intent = PlannerIntentDSL(
                    objective=candidate.get("objective", ""),
                    capabilities=capabilities,
                    depends_on_facts=candidate.get("depends_on_facts", []),
                    priority=candidate.get("priority", 1.0),
                    risk_level=candidate.get("risk_level", "low"),
                )
            except ValidationError as exc:
                if rejection_diagnostics is not None:
                    rejection_diagnostics.append(
                        {"index": index, "reason": "validation_error", "detail": str(exc.errors()[0]["type"])[:120],
                         "fields": list(dict.fromkeys(".".join(map(str, error["loc"])) for error in exc.errors()))}
                    )
                continue
            if not intent.capabilities:
                if rejection_diagnostics is not None:
                    rejection_diagnostics.append({"index": index, "reason": "no_allowed_capabilities"})
                continue
            normalized = normalize_text(intent.objective)
            if normalized in historical_objectives or normalized in accepted_objectives:
                if rejection_diagnostics is not None:
                    rejection_diagnostics.append({"index": index, "reason": "duplicate_objective"})
                continue
            accepted_objectives.add(normalized)
            checkpoint_id = candidate.get("continuation_checkpoint_id")
            checkpoint = session.get(AttemptCheckpoint, checkpoint_id) if isinstance(checkpoint_id, str) else None
            if checkpoint is not None and checkpoint.project_id != project_id:
                checkpoint = None
            result = repository.upsert_intent(
                session,
                project_id=project_id,
                objective=intent.objective,
                capability_tags=intent.capabilities,
                dependency_fact_ids=[fact_id for fact_id in intent.depends_on_facts if fact_id in fact_ids],
                parent_intent_id=checkpoint.intent_id if checkpoint else None,
                priority=intent.priority,
                risk_level=intent.risk_level,
                budget={
                    "model_role": "triage" if current_phase == 1 else "reviewer" if current_phase >= 4 else "solver",
                    "phase": current_phase,
                    **({"continuation_attempt_id": checkpoint.attempt_id} if checkpoint else {}),
                },
            )
            if result.created:
                created.append(result.item.id)
            elif rejection_diagnostics is not None:
                rejection_diagnostics.append({"index": index, "reason": "repository_duplicate"})
        return created

    @staticmethod
    def _continuation_candidates(session: Session, *, facts: list[Fact], checkpoints: list[AttemptCheckpoint]) -> list[dict[str, Any]]:
        candidates = []
        validator = FlagValidator()
        for checkpoint in checkpoints:
            if checkpoint.status != "PARTIAL" or not checkpoint.next_steps:
                continue
            supporting = [
                fact for fact in facts
                if fact.source_attempt_id == checkpoint.attempt_id
                and normalize_fact_category(fact.category) in {"auth", "vuln", "exploit", "rce", "file_read", "privilege", "credential", "memory", "flag"}
                and fact.confidence >= 0.8 and fact.evidence_refs
            ]
            supported = []
            for fact in supporting:
                for ref in fact.evidence_refs:
                    artifact = session.get(Artifact, ref)
                    if (
                        artifact is not None
                        and artifact.project_id == checkpoint.project_id
                        and artifact.origin_kind in {"target_observation", "challenge_input", "worker_observation"}
                        and validator.is_current_evidence(session, artifact)
                    ):
                        supported.append(fact.id)
                        break
            if supported:
                candidates.append({
                    "objective": f"Continue from the evidenced breakthrough: {checkpoint.next_steps[0][:1200]}. Reuse the saved reproduction and verify the remaining step before expanding exploration.",
                    "depends_on_facts": supported,
                    "priority": 2.5,
                    "risk_level": "low",
                    "continuation_checkpoint_id": checkpoint.id,
                })
        return candidates

    @staticmethod
    def _fallback_candidates(
        project: Project,
        *,
        facts: list[Fact],
        checkpoints: list[AttemptCheckpoint],
    ) -> list[dict[str, Any]]:
        evidence_text = " ".join([project.goal, *(fact.statement for fact in facts[-20:])])
        playbook = select_playbook(project.challenge_type, evidence_text)
        capabilities = list(dict.fromkeys([*playbook.capabilities, "blackboard.query"]))
        dependencies = [fact.id for fact in facts[-5:]]
        candidates = [
            {
                "objective": (
                    "Run an independent, evidence-backed branch for this playbook step: "
                    f"{step}. Read the shared board first, avoid failed routes, publish decisive evidence immediately, "
                    "and stop after one falsifiable experiment."
                ),
                "capabilities": capabilities,
                "depends_on_facts": dependencies,
                "priority": max(1.0, 2.0 - index * 0.1),
                "risk_level": "low",
            }
            for index, step in enumerate(playbook.first_steps)
        ]
        next_steps = [
            str(step).strip()
            for checkpoint in checkpoints
            for step in checkpoint.next_steps
            if str(step).strip()
        ]
        candidates.extend(
            {
                "objective": (
                    "Independently validate this unresolved checkpoint direction against the shared evidence: "
                    f"{step[:1000]}. Report confirming or contradicting observations and do not repeat recorded failures."
                ),
                "capabilities": capabilities,
                "depends_on_facts": dependencies,
                "priority": 1.7,
                "risk_level": "low",
            }
            for step in next_steps[:6]
        )
        return candidates
