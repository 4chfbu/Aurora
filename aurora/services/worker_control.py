from __future__ import annotations

import hashlib
import hmac
from typing import Any

from sqlmodel import Session, select

from aurora.models import Artifact, Attempt, AttemptCheckpoint, Fact, Worker, WorkerEvent, now_utc
from aurora.services.blackboard_repository import BlackboardRepository


class WorkerControlService:
    def authenticate(self, session: Session, *, worker_id: str, token: str) -> tuple[Worker, Attempt]:
        worker = session.get(Worker, worker_id)
        if worker is None or worker.status != "RUNNING":
            raise PermissionError("worker is not active")
        attempt = session.exec(
            select(Attempt).where(Attempt.worker_id == worker_id, Attempt.status == "RUNNING").order_by(Attempt.started_at.desc())
        ).first()
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""
        if attempt is None or not attempt.codex_control_token_hash or not hmac.compare_digest(digest, attempt.codex_control_token_hash):
            raise PermissionError("invalid worker control token")
        return worker, attempt

    def query(self, session: Session, *, worker: Worker, attempt: Attempt) -> dict[str, Any]:
        facts = session.exec(
            select(Fact).where(Fact.project_id == worker.project_id, Fact.status == "ACTIVE").order_by(Fact.created_at.desc()).limit(50)
        ).all()
        checkpoints = session.exec(
            select(AttemptCheckpoint)
            .where(AttemptCheckpoint.project_id == worker.project_id)
            .order_by(AttemptCheckpoint.created_at.desc())
            .limit(5)
        ).all()
        live = session.exec(
            select(WorkerEvent)
            .where(WorkerEvent.project_id == worker.project_id, WorkerEvent.event_type == "checkpoint.saved")
            .order_by(WorkerEvent.created_at.desc())
            .limit(5)
        ).all()
        return {
            "version": attempt.blackboard_version,
            "facts": [
                {
                    "id": fact.id,
                    "statement": fact.statement,
                    "category": fact.category,
                    "confidence": fact.confidence,
                    "evidence_refs": fact.evidence_refs,
                }
                for fact in facts
            ],
            "checkpoints": [
                {
                    "id": checkpoint.id,
                    "summary": checkpoint.summary,
                    "failed_routes": checkpoint.failed_routes,
                    "next_steps": checkpoint.next_steps,
                    "fact_refs": checkpoint.fact_refs,
                    "artifact_refs": checkpoint.artifact_refs,
                }
                for checkpoint in checkpoints
            ],
            "live_checkpoints": [event.payload_json for event in live],
        }

    def append_fact(
        self,
        session: Session,
        *,
        worker: Worker,
        attempt: Attempt,
        statement: str,
        category: str,
        confidence: float,
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        refs = list(dict.fromkeys(ref for ref in evidence_refs if isinstance(ref, str) and ref))
        artifacts = [session.get(Artifact, ref) for ref in refs]
        if not refs or any(artifact is None or artifact.project_id != worker.project_id for artifact in artifacts):
            raise ValueError("all fact evidence_refs must name artifacts from this project")
        result = BlackboardRepository().upsert_fact(
            session,
            project_id=worker.project_id,
            statement=statement.strip()[:4000],
            category=category.strip()[:100] or "analysis",
            confidence=max(0.0, min(float(confidence), 1.0)),
            evidence_refs=refs,
            source_intent_id=worker.intent_id,
            source_attempt_id=attempt.id,
        )
        self._advance(session, worker=worker, attempt=attempt, event_type="blackboard.fact_appended", payload={"fact_id": result.item.id, "created": result.created})
        return {"fact_id": result.item.id, "created": result.created, "version": attempt.blackboard_version}

    def save_checkpoint(
        self,
        session: Session,
        *,
        worker: Worker,
        attempt: Attempt,
        summary: str,
        completed_steps: list[str],
        failed_routes: list[str],
        next_step: str,
        artifact_refs: list[str],
    ) -> dict[str, Any]:
        refs = list(dict.fromkeys(ref for ref in artifact_refs if isinstance(ref, str) and ref))
        artifacts = [session.get(Artifact, ref) for ref in refs]
        if any(artifact is None or artifact.project_id != worker.project_id for artifact in artifacts):
            raise ValueError("checkpoint artifact_refs must belong to this project")
        payload = {
            "summary": summary.strip()[:2000],
            "completed_steps": self._strings(completed_steps),
            "failed_routes": self._strings(failed_routes),
            "next_step": next_step.strip()[:1000],
            "artifact_refs": refs,
        }
        self._advance(session, worker=worker, attempt=attempt, event_type="checkpoint.saved", payload=payload)
        return {"status": "saved", "version": attempt.blackboard_version}

    @staticmethod
    def _strings(values: list[str]) -> list[str]:
        return [str(value).strip()[:1000] for value in values if str(value).strip()][:20]

    @staticmethod
    def _advance(session: Session, *, worker: Worker, attempt: Attempt, event_type: str, payload: dict[str, Any]) -> None:
        attempt.blackboard_version += 1
        attempt.last_event_at = now_utc()
        session.add(attempt)
        session.add(
            WorkerEvent(
                project_id=worker.project_id,
                worker_id=worker.id,
                intent_id=worker.intent_id,
                attempt_id=attempt.id,
                event_type=event_type,
                payload_json={**payload, "blackboard_version": attempt.blackboard_version},
            )
        )
        session.commit()

