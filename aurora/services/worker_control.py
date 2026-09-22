from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.models import Artifact, Attempt, Intent, Worker, WorkerEvent, now_utc
from aurora.services.artifact_store import ArtifactStore
from aurora.services.blackboard_repository import BlackboardRepository
from aurora.services.project_coordination import ProjectCoordinationService
from aurora.services.evidence_context import current_environment_id
from aurora.services.flag_validator import FlagValidator
from aurora.services.progress import evidence_progress_counts
from aurora.services.context_memory import select_context_memory


class WorkerControlService:
    def __init__(self, *, artifact_store: ArtifactStore | None = None, workspace_dir: Path | None = None) -> None:
        self.artifact_store = artifact_store or ArtifactStore()
        self.workspace_dir = workspace_dir or get_settings().codex_workspace_dir

    def authenticate(self, session: Session, *, worker_id: str, token: str) -> tuple[Worker, Attempt]:
        worker = session.get(Worker, worker_id)
        if worker is None or worker.status not in {"RUNNING", "CONCLUDING"}:
            raise PermissionError("worker is not active")
        attempt = session.exec(
            select(Attempt).where(Attempt.worker_id == worker_id, Attempt.status.in_(["RUNNING", "FINALIZING"])).order_by(Attempt.started_at.desc())
        ).first()
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""
        if attempt is None or not attempt.codex_control_token_hash or not hmac.compare_digest(digest, attempt.codex_control_token_hash):
            raise PermissionError("invalid worker control token")
        return worker, attempt

    def query(self, session: Session, *, worker: Worker, attempt: Attempt) -> dict[str, Any]:
        state = ProjectCoordinationService().ensure(session, project_id=worker.project_id)
        session.refresh(state)
        intent = session.get(Intent, worker.intent_id)
        memory = select_context_memory(session, project_id=worker.project_id, intent=intent, attempt=attempt, fact_limit=50, checkpoint_limit=5)
        facts, checkpoints, live = memory.facts, memory.checkpoints, memory.live_checkpoints
        return {
            "version": state.graph_version,
            "attempt_version": attempt.blackboard_version,
            "environment_id": current_environment_id(session, worker.project_id),
            "stale": False,
            "session_handoff": memory.handoff(),
            "facts": [
                {
                    "id": fact.id,
                    "statement": fact.statement,
                    "category": fact.category,
                    "confidence": fact.confidence,
                    "evidence_refs": fact.evidence_refs,
                    "evidence_items": fact.evidence_items,
                    "source_attempt_id": fact.source_attempt_id,
                }
                for fact in facts
            ],
            "checkpoints": [
                {
                    "id": checkpoint.id,
                    "attempt_id": checkpoint.attempt_id,
                    "memory_role": "lineage" if checkpoint.id in memory.pinned_checkpoint_ids else "peer_history",
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
        request_id: str | None = None,
    ) -> dict[str, Any]:
        prior = self._prior_write_result(session, attempt=attempt, event_type="blackboard.fact_appended", request_id=request_id)
        if prior is not None:
            return {
                "fact_id": prior.get("fact_id"),
                "created": bool(prior.get("created")),
                "evidence_refs": list(prior.get("evidence_refs") or []),
                "version": int(prior.get("graph_version") or prior.get("blackboard_version") or attempt.blackboard_version),
                "deduplicated": True,
            }
        before_progress = evidence_progress_counts(session, worker.project_id)
        refs = self._normalize_artifact_refs(
            session,
            worker=worker,
            attempt=attempt,
            refs=evidence_refs,
            required=True,
        )
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
        after_progress = evidence_progress_counts(session, worker.project_id)
        if any(after > before for before, after in zip(before_progress, after_progress)):
            session.add(WorkerEvent(
                project_id=worker.project_id, worker_id=worker.id, intent_id=worker.intent_id, attempt_id=attempt.id,
                event_type="evidence.progress", payload_json={"fact_id": result.item.id, "evidence_refs": refs},
            ))
        version = self._advance(
            session,
            worker=worker,
            attempt=attempt,
            event_type="blackboard.fact_appended",
            payload={"fact_id": result.item.id, "created": result.created, "evidence_refs": refs, "request_id": request_id},
        )
        return {
            "fact_id": result.item.id,
            "created": result.created,
            "evidence_refs": refs,
            "version": version,
        }

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
        request_id: str | None = None,
    ) -> dict[str, Any]:
        prior = self._prior_write_result(session, attempt=attempt, event_type="checkpoint.saved", request_id=request_id)
        if prior is not None:
            return {
                "status": "saved",
                "artifact_refs": list(prior.get("artifact_refs") or []),
                "version": int(prior.get("graph_version") or prior.get("blackboard_version") or attempt.blackboard_version),
                "deduplicated": True,
            }
        refs = self._normalize_artifact_refs(
            session,
            worker=worker,
            attempt=attempt,
            refs=artifact_refs,
            required=False,
        )
        payload = {
            "summary": summary.strip()[:2000],
            "completed_steps": self._strings(completed_steps),
            "failed_routes": self._strings(failed_routes),
            "next_step": next_step.strip()[:1000],
            "artifact_refs": refs,
            "request_id": request_id,
        }
        version = self._advance(session, worker=worker, attempt=attempt, event_type="checkpoint.saved", payload=payload)
        return {"status": "saved", "artifact_refs": refs, "version": version}

    def read_artifact(
        self,
        session: Session,
        *,
        worker: Worker,
        artifact_id: str,
        max_bytes: int = 64_000,
    ) -> dict[str, Any]:
        artifact = session.get(Artifact, artifact_id)
        if artifact is None or artifact.project_id != worker.project_id:
            raise ValueError("artifact does not belong to this project")
        limit = max(1, min(int(max_bytes), 64_000))
        try:
            content = self.artifact_store.read_text(artifact, max_bytes=limit)
        except OSError as exc:
            raise ValueError("artifact content is unavailable") from exc
        return {
            "id": artifact.id,
            "type": artifact.type,
            "sha256": artifact.sha256,
            "mime_type": artifact.mime_type,
            "size": artifact.size,
            "summary": artifact.summary,
            "origin_kind": artifact.origin_kind,
            "evidence_context": artifact.evidence_context,
            "current_evidence": FlagValidator().is_current_evidence(session, artifact),
            "content": content,
            "truncated": artifact.size > limit,
        }

    def _normalize_artifact_refs(
        self,
        session: Session,
        *,
        worker: Worker,
        attempt: Attempt,
        refs: list[str],
        required: bool,
    ) -> list[str]:
        values = list(dict.fromkeys(ref.strip() for ref in refs if isinstance(ref, str) and ref.strip()))
        if required and not values:
            raise ValueError("fact evidence_refs must not be empty")

        resolved: list[Artifact | Path] = []
        for ref in values:
            artifact = session.get(Artifact, ref)
            if artifact is not None:
                if artifact.project_id != worker.project_id:
                    raise ValueError("artifact reference does not belong to this project")
                resolved.append(artifact)
                continue
            resolved.append(self._resolve_workspace_file(worker=worker, ref=ref))

        artifact_ids: list[str] = []
        for item in resolved:
            if isinstance(item, Artifact):
                artifact_ids.append(item.id)
                continue
            artifact = self.artifact_store.write_file(
                session,
                project_id=worker.project_id,
                source=item,
                summary=f"Worker evidence: {item.name}",
                source_attempt_id=attempt.id,
                artifact_type="worker-evidence",
                origin_kind="worker_observation",
                deduplicate=True,
            )
            artifact_ids.append(artifact.id)
        return list(dict.fromkeys(artifact_ids))

    def _resolve_workspace_file(self, *, worker: Worker, ref: str) -> Path:
        workspace = (self.workspace_dir / worker.project_id / worker.id).resolve()
        if ref == "/workspace":
            relative = Path()
        elif ref.startswith("/workspace/"):
            relative = Path(ref.removeprefix("/workspace/"))
        else:
            supplied = Path(ref)
            relative = supplied if not supplied.is_absolute() else None
        candidate = (workspace / relative).resolve() if relative is not None else Path(ref).resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("artifact path escapes the worker workspace") from exc
        if not candidate.is_file():
            raise ValueError("artifact path must name an existing file in the worker workspace")
        return candidate

    @staticmethod
    def _strings(values: list[str]) -> list[str]:
        return [str(value).strip()[:1000] for value in values if str(value).strip()][:20]

    @staticmethod
    def _prior_write_result(
        session: Session,
        *,
        attempt: Attempt,
        event_type: str,
        request_id: str | None,
    ) -> dict[str, Any] | None:
        if not request_id:
            return None
        events = session.exec(
            select(WorkerEvent)
            .where(WorkerEvent.attempt_id == attempt.id, WorkerEvent.event_type == event_type)
            .order_by(WorkerEvent.created_at.desc())
            .limit(100)
        ).all()
        return next(
            (
                event.payload_json
                for event in events
                if isinstance(event.payload_json, dict) and event.payload_json.get("request_id") == request_id
            ),
            None,
        )

    @staticmethod
    def _advance(session: Session, *, worker: Worker, attempt: Attempt, event_type: str, payload: dict[str, Any]) -> int:
        state = ProjectCoordinationService().ensure(session, project_id=worker.project_id)
        session.refresh(state)
        version = state.graph_version
        if event_type == "checkpoint.saved":
            version = ProjectCoordinationService().record_graph_change(session, project_id=worker.project_id, commit=False)
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
                payload_json={**payload, "blackboard_version": attempt.blackboard_version, "graph_version": version},
            )
        )
        session.commit()
        session.refresh(attempt)
        return version
