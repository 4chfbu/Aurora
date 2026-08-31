from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from aurora.models import Fact, Intent, WorkerEvent, now_utc
from aurora.services.project_coordination import ProjectCoordinationService


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def stable_json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


TRANSIENT_ROUTE_KEYS = {
    "timeout_seconds",
    "navigation_timeout_seconds",
    "total_timeout_seconds",
    "wait_seconds",
    "max_output_bytes",
}


def route_fingerprint(value: Any) -> str:
    def normalize(item: Any) -> Any:
        if isinstance(item, dict):
            normalized = {key: normalize(child) for key, child in item.items() if key not in TRANSIENT_ROUTE_KEYS}
            command = normalized.get("command")
            if isinstance(command, str):
                normalized["command"] = re.sub(r"\s+", " ", command.strip())
            return normalized
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if isinstance(item, str):
            return item.strip()
        return item

    return stable_json(normalize(value))


def normalize_evidence_items(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in (items or [])[:10]:
        if not isinstance(item, dict) or not isinstance(item.get("description"), str):
            continue
        description = item["description"].strip()[:2000]
        refs = item.get("artifact_refs")
        if not description or not isinstance(refs, list):
            continue
        artifact_refs = list(dict.fromkeys(str(ref) for ref in refs if isinstance(ref, str) and ref.strip()))
        if not artifact_refs:
            continue
        key = normalize_text(description)
        if key in merged:
            merged[key]["artifact_refs"] = sorted(set(merged[key]["artifact_refs"] + artifact_refs))
        else:
            merged[key] = {"description": description, "artifact_refs": artifact_refs}
    return list(merged.values())


@dataclass
class UpsertResult:
    item: Fact | Intent
    created: bool


class BlackboardRepository:
    def upsert_fact(
        self,
        session: Session,
        *,
        project_id: str,
        statement: str,
        category: str = "general",
        confidence: float = 0.5,
        evidence_refs: list[str] | None = None,
        evidence_items: list[dict[str, Any]] | None = None,
        source_intent_id: str | None = None,
        source_attempt_id: str | None = None,
    ) -> UpsertResult:
        normalized = normalize_text(statement)
        normalized_items = normalize_evidence_items(evidence_items)
        item_refs = [ref for item in normalized_items for ref in item["artifact_refs"]]
        all_refs = sorted(set((evidence_refs or []) + item_refs))
        existing_facts = session.exec(select(Fact).where(Fact.project_id == project_id, Fact.status == "ACTIVE")).all()
        for fact in existing_facts:
            if normalize_text(fact.statement) == normalized:
                merged_refs = sorted(set(fact.evidence_refs + all_refs))
                fact.evidence_refs = merged_refs
                fact.evidence_items = normalize_evidence_items((fact.evidence_items or []) + normalized_items)
                fact.confidence = max(fact.confidence, confidence)
                session.add(fact)
                session.add(
                    WorkerEvent(
                        project_id=project_id,
                        worker_id=None,
                        intent_id=source_intent_id,
                        attempt_id=source_attempt_id,
                        event_type="fact.merged",
                        payload_json={"fact_id": fact.id, "statement": fact.statement, "evidence_refs": merged_refs, "evidence_items": fact.evidence_items},
                    )
                )
                session.commit()
                session.refresh(fact)
                return UpsertResult(fact, created=False)

        fact = Fact(
            project_id=project_id,
            statement=statement,
            category=category,
            confidence=confidence,
            evidence_refs=all_refs,
            evidence_items=normalized_items,
            source_intent_id=source_intent_id,
            source_attempt_id=source_attempt_id,
        )
        session.add(fact)
        session.add(
            WorkerEvent(
                project_id=project_id,
                worker_id=None,
                intent_id=source_intent_id,
                attempt_id=source_attempt_id,
                event_type="fact.created",
                payload_json={"statement": statement, "category": category, "confidence": confidence, "evidence_items": normalized_items},
            )
        )
        session.commit()
        session.refresh(fact)
        ProjectCoordinationService().record_graph_change(session, project_id=project_id)
        return UpsertResult(fact, created=True)

    def upsert_intent(
        self,
        session: Session,
        *,
        project_id: str,
        objective: str,
        capability_tags: list[str] | None = None,
        dependency_fact_ids: list[str] | None = None,
        parent_intent_id: str | None = None,
        priority: float = 1.0,
        risk_level: str = "low",
        budget: dict[str, Any] | None = None,
    ) -> UpsertResult:
        normalized_objective = normalize_text(objective)
        normalized_tags = sorted(capability_tags or [])
        normalized_budget = stable_json(budget)
        candidates = session.exec(
            select(Intent).where(
                Intent.project_id == project_id,
                Intent.status.in_(["PENDING", "CLAIMED", "RUNNING", "CONCLUDING"]),
            )
        ).all()
        for intent in candidates:
            if (
                normalize_text(intent.objective) == normalized_objective
                and sorted(intent.capability_tags) == normalized_tags
                and stable_json(intent.budget) == normalized_budget
            ):
                if priority > intent.priority:
                    intent.priority = priority
                    intent.updated_at = now_utc()
                    session.add(intent)
                session.add(
                    WorkerEvent(
                        project_id=project_id,
                        worker_id=None,
                        intent_id=intent.id,
                        event_type="intent.duplicate_suppressed",
                        payload_json={"objective": objective, "existing_intent_id": intent.id},
                    )
                )
                session.commit()
                session.refresh(intent)
                return UpsertResult(intent, created=False)

        intent = Intent(
            project_id=project_id,
            objective=objective,
            capability_tags=capability_tags or [],
            dependency_fact_ids=dependency_fact_ids or [],
            parent_intent_id=parent_intent_id,
            priority=priority,
            risk_level=risk_level,
            budget=budget or {},
        )
        session.add(intent)
        session.add(
            WorkerEvent(
                project_id=project_id,
                worker_id=None,
                intent_id=intent.id,
                event_type="intent.created",
                payload_json={"objective": objective, "capability_tags": capability_tags or [], "priority": priority},
            )
        )
        session.commit()
        session.refresh(intent)
        return UpsertResult(intent, created=True)
