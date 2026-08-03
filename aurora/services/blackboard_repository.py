from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from aurora.models import Fact, Intent, WorkerEvent, now_utc


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def stable_json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
        source_intent_id: str | None = None,
        source_attempt_id: str | None = None,
    ) -> UpsertResult:
        normalized = normalize_text(statement)
        existing_facts = session.exec(select(Fact).where(Fact.project_id == project_id, Fact.status == "ACTIVE")).all()
        for fact in existing_facts:
            if normalize_text(fact.statement) == normalized:
                merged_refs = sorted(set(fact.evidence_refs + (evidence_refs or [])))
                fact.evidence_refs = merged_refs
                fact.confidence = max(fact.confidence, confidence)
                session.add(fact)
                session.add(
                    WorkerEvent(
                        project_id=project_id,
                        worker_id=None,
                        intent_id=source_intent_id,
                        attempt_id=source_attempt_id,
                        event_type="fact.merged",
                        payload_json={"fact_id": fact.id, "statement": fact.statement, "evidence_refs": merged_refs},
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
            evidence_refs=evidence_refs or [],
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
                payload_json={"statement": statement, "category": category, "confidence": confidence},
            )
        )
        session.commit()
        session.refresh(fact)
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
