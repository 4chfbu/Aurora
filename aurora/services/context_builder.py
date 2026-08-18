from __future__ import annotations

import json
import re
from typing import Any

from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.models import Artifact, AttemptCheckpoint, AuthorizationScope, ChallengeGroupItem, ContextSnapshot, Fact, Intent, Project, ProjectRuntimePolicy, Worker, WorkerEvent
from aurora.services.mcp_registry import visible_mcp_tools
from aurora.services.tool_profiles import tool_environment


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
    "suggested_intents": [],
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
}


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def redact(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(lambda m: f"{m.group(1)}=<redacted>", redacted)
    return redacted


class ContextBuilder:
    def build(self, session: Session, *, project_id: str, intent_id: str, worker_id: str | None = None) -> ContextSnapshot:
        settings = get_settings()
        project = session.get(Project, project_id)
        intent = session.get(Intent, intent_id)
        if project is None or intent is None:
            raise ValueError("project or intent not found")

        scope = session.exec(select(AuthorizationScope).where(AuthorizationScope.project_id == project_id)).first()
        policy = session.exec(select(ProjectRuntimePolicy).where(ProjectRuntimePolicy.project_id == project_id)).first()
        worker = session.get(Worker, worker_id) if worker_id else None
        allow_subagents = bool(
            settings.subagents_enabled
            and settings.worker_runtime.strip().lower() in {"codex", "codex_harness", "harness"}
            and policy is not None
            and policy.subagents_enabled
            and (worker is None or worker.execution_kind == "primary")
        )
        facts = session.exec(select(Fact).where(Fact.project_id == project_id, Fact.status == "ACTIVE").limit(25)).all()
        artifacts = session.exec(select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.created_at.desc()).limit(10)).all()
        checkpoints = session.exec(
            select(AttemptCheckpoint).where(AttemptCheckpoint.project_id == project_id).order_by(AttemptCheckpoint.created_at.desc()).limit(3)
        ).all()
        group_item = session.exec(
            select(ChallengeGroupItem).where(ChallengeGroupItem.project_id == project_id, ChallengeGroupItem.fused_status == "RUNNING")
        ).first()
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

        sections: dict[str, Any] = {
            "project_goal": project.goal,
            "target_access": {
                "status": project.target_verification_status,
                "url": project.target_url,
                "reason": project.target_verification_reason,
                "required_for_solver_start": False,
            },
            "tool_environment": tool_environment(settings, project.challenge_type),
            "competition_context": {
                "phase": group_item.phase,
                "attachments": list((group_item.competition_meta or {}).get("attachments", [])),
                "target": project.target_url,
                "targets": list((group_item.competition_meta or {}).get("container_addr", [])) if isinstance((group_item.competition_meta or {}).get("container_addr"), list) else ([project.target_url] if project.target_url else []),
                "hint": group_item.hint_content,
                "previous_attempts": list(group_item.failure_history),
            } if group_item else None,
            "harvester_task": harvester_task.payload_json if harvester_task else None,
            "current_intent": {
                "id": intent.id,
                "objective": intent.objective,
                "capability_tags": intent.capability_tags,
                "risk_level": intent.risk_level,
                "tool_request": intent.budget.get("tool_request") if intent.budget else None,
                # Worker budgets contain the effective defaults applied by the
                # scheduler. Expose them so the solver can honor the soft
                # finalization deadline even when the Intent omitted limits.
                "budget": {**(intent.budget or {}), **(worker.budgets if worker else {})},
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
            "operating_mode": {
                "tooling": "Kali native tools first; MCP only when Kali lacks the capability.",
                "context_policy": "intent-first; raw tool output stays in Artifact Store unless explicitly read by id.",
                "target_policy": "A target is optional. If none is active, continue with imported artifacts and local analysis; never invent a target URL. Network requests remain authorization-gated.",
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
            sections = json.loads(redact(json.dumps(sections, ensure_ascii=False)))

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
            visible_tools_json=visible_mcp_tools(settings, allow_subagents=allow_subagents),
            output_schema_json=OUTPUT_SCHEMA,
            total_chars=len(serialized),
            estimated_tokens=estimate_tokens(serialized),
            truncation_report_json=truncation_report,
        )
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)
        return snapshot
