from __future__ import annotations

import json
import re
from typing import Any

from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.models import Artifact, AttemptCheckpoint, AuthorizationScope, ChallengeGroupItem, ContextSnapshot, Fact, FlagCandidate, Intent, Project, ProjectCoordinationState, ProjectRuntimePolicy, ToolTrace, Worker, WorkerEvent
from aurora.services.flag_rejection import is_authoritative_flag_rejection
from aurora.services.mcp_registry import visible_mcp_tools
from aurora.services.tool_contract import tools_for_runtime
from aurora.services.tool_profiles import tool_environment
from aurora.services.solver_playbooks import select_playbook
from aurora.services.agent_runtime import agent_runtime_settings


SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+"),
]


OUTPUT_SCHEMA: dict[str, Any] = {
    "status": "success | partial | failed",
    "summary": "short operator-visible result",
    "fact_candidates": [{
        "statement": "evidence-backed conclusion",
        "confidence": 0.0,
        "category": "...",
        "evidence_refs": [],
        "evidence_items": [{"description": "observable supporting evidence", "artifact_refs": ["artifact_id"]}],
    }],
    "hypotheses": [],
    "artifact_refs": [],
    "failed_attempts": [],
    "suggested_intents": [{
        "objective": "one evidence-backed experiment",
        "expected_observation": "the result that distinguishes the active hypothesis",
        "capabilities": [],
        "priority": 1.0,
        "risk_level": "low",
    }],
    "fork_recommendations": [],
    "subagent_reports": [],
    "candidate_flags": [{
        "value": "prefix{payload}",
        "artifact_ref": "trusted artifact containing the exact value",
        "provenance_kind": "observed | derived_replay",
    }],
    "blockers": [{"kind": "target | session | paid_confirmation | missing_evidence", "reason": "", "next_step": ""}],
    "decision_summary": {
        "selected_intent": "...",
        "reason_summary": "observable non-chain-of-thought rationale",
        "next_tool_plan": [],
    },
    "tool_requests": [{"tool_name": "visible capability name", "request": {}, "activity_label": "short safe label"}],
}


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def redact(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(lambda m: f"{m.group(1)}=<redacted>", redacted)
    return redacted


def _redact_sections(value: Any) -> Any:
    """Redact secret-like text inside an already-decoded JSON object.

    JSON structure is preserved because the regex only runs on string leaf
    values, never on the serialized representation.
    """
    if isinstance(value, dict):
        return {key: _redact_sections(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_sections(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


class ContextBuilder:
    def build(self, session: Session, *, project_id: str, intent_id: str, worker_id: str | None = None) -> ContextSnapshot:
        settings = get_settings()
        project = session.get(Project, project_id)
        intent = session.get(Intent, intent_id)
        if project is None or intent is None:
            raise ValueError("project or intent not found")

        scope = session.exec(select(AuthorizationScope).where(AuthorizationScope.project_id == project_id)).first()
        policy = session.exec(select(ProjectRuntimePolicy).where(ProjectRuntimePolicy.project_id == project_id)).first()
        agent_runtime = agent_runtime_settings(session)
        worker = session.get(Worker, worker_id) if worker_id else None
        coordination = session.exec(
            select(ProjectCoordinationState).where(ProjectCoordinationState.project_id == project_id)
        ).first()
        open_intents = session.exec(
            select(Intent)
            .where(Intent.project_id == project_id, Intent.status.in_(["PENDING", "RUNNING", "CONCLUDING"]))
            .order_by(Intent.priority.desc(), Intent.created_at)
        ).all()
        active_workers = session.exec(
            select(Worker)
            .where(Worker.project_id == project_id, Worker.status.in_(["STARTING", "RUNNING", "CONCLUDING"]))
            .order_by(Worker.created_at)
        ).all()
        allow_subagents = bool(
            agent_runtime.subagents_enabled
            and settings.worker_runtime.strip().lower() in {"codex", "codex_harness", "harness"}
            and policy is not None
            and policy.subagents_enabled
            and (worker is None or worker.execution_kind == "primary")
        )
        facts = session.exec(select(Fact).where(Fact.project_id == project_id, Fact.status == "ACTIVE").order_by(Fact.created_at.desc()).limit(25)).all()
        artifacts = session.exec(select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.created_at.desc()).limit(10)).all()
        checkpoints = session.exec(
            select(AttemptCheckpoint).where(AttemptCheckpoint.project_id == project_id).order_by(AttemptCheckpoint.created_at.desc()).limit(3)
        ).all()
        handoff_artifact_refs = list(dict.fromkeys(
            artifact_ref
            for checkpoint in checkpoints
            for artifact_ref in checkpoint.artifact_refs
            if isinstance(artifact_ref, str)
        ))
        handoff_artifacts: list[Artifact] = []
        for artifact_ref in handoff_artifact_refs:
            artifact = session.get(Artifact, artifact_ref)
            if artifact is not None and artifact.project_id == project_id:
                handoff_artifacts.append(artifact)
        group_item = session.exec(
            select(ChallengeGroupItem)
            .where(ChallengeGroupItem.project_id == project_id, ChallengeGroupItem.fused_status != "COMPLETED")
            .order_by(ChallengeGroupItem.updated_at.desc())
        ).first()
        evidence_text = " ".join(str(value or "") for value in (
            project.name,
            project.goal,
            group_item.hint_content if group_item else "",
            group_item.competition_meta if group_item else "",
        ))
        playbook = select_playbook(project.challenge_type, evidence_text)
        harvester_task = session.exec(
            select(WorkerEvent)
            .where(WorkerEvent.project_id == project_id, WorkerEvent.event_type == "harvester.task_dispatched")
            .order_by(WorkerEvent.created_at.desc())
        ).first()
        live_checkpoint = session.exec(
            select(WorkerEvent)
            .where(WorkerEvent.project_id == project_id, WorkerEvent.event_type == "checkpoint.saved")
            .order_by(WorkerEvent.created_at.desc())
        ).first()
        rejected_candidates = session.exec(
            select(FlagCandidate)
            .where(FlagCandidate.project_id == project_id, FlagCandidate.status == "REJECTED")
            .order_by(FlagCandidate.updated_at.desc())
            .limit(50)
        ).all()
        rejected_candidates = [candidate for candidate in rejected_candidates if is_authoritative_flag_rejection(candidate)][:10]
        verified_candidates = session.exec(
            select(FlagCandidate)
            .where(FlagCandidate.project_id == project_id, FlagCandidate.status == "LOCAL_VERIFIED")
            .order_by(FlagCandidate.updated_at.desc())
            .limit(10)
        ).all()
        failed_verifications = session.exec(
            select(ToolTrace)
            .where(ToolTrace.project_id == project_id, ToolTrace.tool_name == "flag.verify", ToolTrace.exit_code != 0)
            .order_by(ToolTrace.created_at.desc())
            .limit(5)
        ).all()

        visible_tools = visible_mcp_tools(settings, allow_subagents=allow_subagents, contract=tools_for_runtime(settings))
        visible_tool_names = {tool["name"] for tool in visible_tools}

        sections: dict[str, Any] = {
            "project_goal": project.goal,
            "target_access": {
                "status": project.target_verification_status,
                "url": project.target_url,
                "reason": project.target_verification_reason,
                "required_for_solver_start": False,
            },
            "tool_environment": tool_environment(settings, project.challenge_type),
            "solver_playbook": {
                "challenge_type": playbook.challenge_type,
                "confidence": playbook.confidence,
                "first_steps": list(playbook.first_steps),
                "stop_conditions": list(playbook.stop_conditions),
                "capabilities": list(playbook.capabilities),
                "discipline": "每轮只验证一个可证伪假设；5-10 分钟无新权限、Artifact 或候选空间收敛就保存 checkpoint 并止损。",
            },
            "competition_context": {
                "platform": str((group_item.competition_meta or {}).get("platform") or ""),
                "phase": group_item.phase,
                "attachments": list((group_item.competition_meta or {}).get("attachments", [])),
                "notices": list((group_item.competition_meta or {}).get("notices", [])),
                "target": project.target_url,
                "targets": list((group_item.competition_meta or {}).get("container_addr", [])) if isinstance((group_item.competition_meta or {}).get("container_addr"), list) else ([project.target_url] if project.target_url else []),
                "hint": group_item.hint_content,
                "previous_attempts": list(group_item.failure_history),
                "progress": {
                    "correct_flag_count": (group_item.competition_meta or {}).get("correct_flag_count", 0),
                    "total_flag_count": (group_item.competition_meta or {}).get("flag_count"),
                    "is_completed": (group_item.competition_meta or {}).get("is_completed", False),
                },
            } if group_item else None,
            "flag_submission": {
                "eligible_candidates": [
                    {
                        "candidate_id": candidate.id,
                        "value": candidate.value,
                        "provenance_kind": candidate.provenance_kind,
                        "submission_count": candidate.submission_count,
                    }
                    for candidate in verified_candidates
                ],
                "same_batch_candidate_id": "latest_verified",
            },
            "harvester_task": harvester_task.payload_json if harvester_task else None,
            "current_intent": {
                "id": intent.id,
                "objective": intent.objective,
                "capability_tags": [tag for tag in intent.capability_tags if tag in visible_tool_names],
                "risk_level": intent.risk_level,
                "tool_request": intent.budget.get("tool_request") if intent.budget else None,
                # Worker budgets contain the effective defaults applied by the
                # scheduler. Expose them so the solver can honor the soft
                # finalization deadline even when the Intent omitted limits.
                "budget": {**(intent.budget or {}), **(worker.budgets if worker else {})},
            },
            "exploration_graph": {
                "multi_agent_enabled": bool(policy and policy.multi_agent_exploration_enabled),
                "graph_version": coordination.graph_version if coordination else 0,
                "last_reasoned_version": coordination.last_reasoned_version if coordination else 0,
                "open_intents": [
                    {
                        "id": item.id,
                        "objective": item.objective,
                        "status": item.status,
                        "priority": item.priority,
                        "parent_intent_id": item.parent_intent_id,
                        "dependency_fact_ids": item.dependency_fact_ids,
                        "claimed_by": item.lease_owner,
                    }
                    for item in open_intents
                ],
                "active_workers": [
                    {"id": item.id, "intent_id": item.intent_id, "status": item.status}
                    for item in active_workers
                ],
                "coordination": "Peers coordinate only through committed facts, artifacts, checkpoints, and intents.",
                "collaboration_cycle": [
                    "Query the live blackboard before selecting an experiment and before any expensive operation.",
                    "Avoid work already owned by another open intent or disproved in a checkpoint.",
                    "Append evidence-backed facts immediately so running peers can consume them.",
                    "Publish contradictions and failed routes, not only successful findings.",
                    "Save a checkpoint with one concrete next step before final output.",
                ],
            },
            "authorization_scope": scope.model_dump(mode="json") if scope else None,
            "facts": [
                {
                    "id": fact.id,
                    "statement": fact.statement,
                    "category": fact.category,
                    "confidence": fact.confidence,
                    "evidence_refs": fact.evidence_refs,
                    "evidence_items": fact.evidence_items,
                }
                for fact in facts
            ],
            "artifact_summaries": [
                {"id": artifact.id, "type": artifact.type, "summary": artifact.summary, "sensitivity": artifact.sensitivity}
                for artifact in artifacts
            ],
            "handoff_artifacts": [
                {"id": artifact.id, "type": artifact.type, "summary": artifact.summary, "sensitivity": artifact.sensitivity}
                for artifact in handoff_artifacts
            ],
            "recent_checkpoints": [
                {
                    "id": checkpoint.id,
                    "status": checkpoint.status,
                    "summary": checkpoint.summary,
                    "conclusions": checkpoint.conclusions,
                    "hypotheses": checkpoint.hypotheses,
                    "failed_routes": checkpoint.failed_routes,
                    "next_steps": checkpoint.next_steps,
                    "fact_refs": checkpoint.fact_refs,
                    "artifact_refs": checkpoint.artifact_refs,
                    "generated_intent_ids": checkpoint.generated_intent_ids,
                }
                for checkpoint in checkpoints
            ],
            "live_checkpoint": live_checkpoint.payload_json if live_checkpoint else None,
            "flag_validation_feedback": {
                "rejected_values": [candidate.value for candidate in rejected_candidates],
                "verification_errors": [
                    {"summary": trace.summary, "artifact_refs": trace.artifact_refs}
                    for trace in failed_verifications
                ],
            } if rejected_candidates or failed_verifications else None,
            "operating_mode": {
                "tooling": "Kali native tools first; MCP only when Kali lacks the capability.",
                "context_policy": "intent-first; raw tool output stays in Artifact Store unless explicitly read by id.",
                "target_policy": "A target is optional. If none is active, continue with imported artifacts and local analysis; never invent a target URL. Network requests are routed through the tool gateway and are no longer pre-authorized.",
                "hidden_chain_of_thought": "not captured; only observable decision summaries are stored.",
                "subagents": {
                    "enabled": allow_subagents,
                    "same_container": allow_subagents,
                    "max_concurrent": policy.max_subagents_concurrent if policy else 0,
                    "max_per_worker": policy.max_subagents_per_worker if policy else 0,
                },
            },
        }
        if settings.debug.redact_secrets:
            sections = _redact_sections(sections)

        serialized = json.dumps(sections, ensure_ascii=False)
        truncation_report = {"truncated": False, "original_chars": len(serialized)}
        if len(serialized.encode("utf-8")) > settings.debug.max_context_snapshot_bytes:
            keep_chars = settings.debug.max_context_snapshot_bytes // 2
            sections["facts"] = sections["facts"][:10]
            sections["artifact_summaries"] = sections["artifact_summaries"][:5]
            truncation_report = {"truncated": True, "original_chars": len(serialized), "kept_chars_approx": keep_chars}
            serialized = json.dumps(sections, ensure_ascii=False)

        section_metrics = {
            name: {"chars": len(json.dumps(value, ensure_ascii=False)), "estimated_tokens": estimate_tokens(json.dumps(value, ensure_ascii=False))}
            for name, value in sections.items()
        }
        snapshot = ContextSnapshot(
            project_id=project_id,
            intent_id=intent_id,
            worker_id=worker_id,
            sections_json=sections,
            section_metrics_json=section_metrics,
            visible_tools_json=visible_tools,
            output_schema_json=OUTPUT_SCHEMA,
            total_chars=len(serialized),
            estimated_tokens=estimate_tokens(serialized),
            truncation_report_json=truncation_report,
        )
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)
        return snapshot
