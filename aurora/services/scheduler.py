from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session, select

from aurora.models import Attempt, Intent, Project, Worker, WorkerEvent, now_utc, new_id
from aurora.services.round_summary import RoundReflectionService
from aurora.services.container_control import stop_worker_containers


class Scheduler:
    def claim_next(self, session: Session, *, project_id: str, lease_seconds: int = 300) -> tuple[Intent, Worker] | None:
        project = session.get(Project, project_id)
        if project is None or project.status in {"COMPLETED", "CANCELLED", "FAILED", "FLAG_READY", "AWAITING_MANUAL_VALIDATION"}:
            return None
        intent = session.exec(
            select(Intent)
            .where(Intent.project_id == project_id, Intent.status == "PENDING")
            .order_by(Intent.priority.desc(), Intent.created_at)
        ).first()
        if intent is None:
            return None

        worker_id = new_id("worker")
        intent.status = "RUNNING"
        intent.lease_owner = worker_id
        intent.lease_expires_at = now_utc() + timedelta(seconds=lease_seconds)
        intent.updated_at = now_utc()
        worker = Worker(
            id=worker_id,
            project_id=project_id,
            intent_id=intent.id,
            status="RUNNING",
            capability_set=intent.capability_tags or ["sandbox.exec"],
            lease={"expires_at": intent.lease_expires_at.isoformat()},
        )
        session.add(intent)
        session.add(worker)
        session.commit()
        session.refresh(intent)
        session.refresh(worker)
        return intent, worker

    def complete(self, session: Session, *, intent: Intent, worker: Worker, status: str = "COMPLETED") -> None:
        # A lease may have expired while a runtime was still unwinding.  That
        # worker no longer owns the intent and must not overwrite the timeout
        # state (or a retry claimed by a newer worker).
        if intent.status != "RUNNING" or intent.lease_owner != worker.id or worker.status != "RUNNING":
            return
        # Publish a single concluding barrier before writing the terminal
        # state. Reapers only claim RUNNING rows, so a late result cannot win
        # after this transaction has acquired the barrier.
        intent.status = "CONCLUDING"
        worker.status = "CONCLUDING"
        intent.updated_at = now_utc()
        worker.updated_at = now_utc()
        session.add(intent)
        session.add(worker)
        session.commit()
        session.refresh(intent)
        session.refresh(worker)
        if intent.status != "CONCLUDING" or worker.status != "CONCLUDING":
            return
        intent.status = status
        intent.lease_owner = None
        intent.lease_expires_at = None
        intent.updated_at = now_utc()
        worker.status = status
        worker.updated_at = now_utc()
        worker.heartbeat = now_utc()
        session.add(intent)
        session.add(worker)
        session.add(
            WorkerEvent(
                project_id=worker.project_id,
                worker_id=worker.id,
                intent_id=worker.intent_id,
                event_type="worker.completed",
                payload_json={"status": status},
            )
        )
        session.commit()

    def owns_active_lease(self, session: Session, *, intent: Intent, worker: Worker) -> bool:
        """Return whether this worker may still publish its final result."""
        session.refresh(intent)
        session.refresh(worker)
        return intent.status == "RUNNING" and intent.lease_owner == worker.id and worker.status == "RUNNING"

    def heartbeat(self, session: Session, *, worker_id: str, lease_seconds: int = 300) -> Worker | None:
        worker = session.get(Worker, worker_id)
        if worker is None or worker.status != "RUNNING":
            return None
        intent = session.get(Intent, worker.intent_id)
        if intent is None or intent.status != "RUNNING" or intent.lease_owner != worker.id:
            return None

        expires_at = now_utc() + timedelta(seconds=lease_seconds)
        worker.heartbeat = now_utc()
        worker.lease = {"expires_at": expires_at.isoformat()}
        worker.updated_at = now_utc()
        intent.lease_expires_at = expires_at
        intent.updated_at = now_utc()
        session.add(worker)
        session.add(intent)
        session.add(
            WorkerEvent(
                project_id=worker.project_id,
                worker_id=worker.id,
                intent_id=worker.intent_id,
                event_type="worker.heartbeat",
                payload_json={"lease_expires_at": expires_at.isoformat()},
            )
        )
        session.commit()
        session.refresh(worker)
        return worker

    def reap_expired(self, session: Session, *, project_id: str) -> int:
        expired = session.exec(
            select(Intent).where(
                Intent.project_id == project_id,
                Intent.status == "RUNNING",
                Intent.lease_expires_at < now_utc(),
            )
        ).all()
        timed_out_primary_attempts: list[tuple[Attempt, dict]] = []
        for intent in expired:
            previous_status = intent.status
            intent.status = "PENDING" if intent.retry_count < intent.max_retries else "FAILED"
            intent.retry_count += 1
            worker_id = intent.lease_owner
            intent.lease_owner = None
            intent.lease_expires_at = None
            intent.updated_at = now_utc()
            session.add(intent)
            session.add(
                WorkerEvent(
                    project_id=project_id,
                    worker_id=worker_id,
                    intent_id=intent.id,
                    event_type="intent.lease_expired",
                    payload_json={"previous_status": previous_status, "new_status": intent.status, "retry_count": intent.retry_count},
                )
            )
            if worker_id:
                worker = session.get(Worker, worker_id)
                if worker:
                    cleanup = stop_worker_containers(worker_id)
                    worker.status = "TIMEOUT"
                    worker.lease = {}
                    worker.heartbeat = now_utc()
                    worker.updated_at = now_utc()
                    session.add(worker)
                    session.add(
                        WorkerEvent(
                            project_id=project_id,
                            worker_id=worker_id,
                            intent_id=intent.id,
                            event_type="worker.timed_out",
                            payload_json={"reason": "lease_expired", "status": "TIMEOUT", "container_cleanup": cleanup},
                        )
                    )
                running_attempts = session.exec(
                    select(Attempt).where(Attempt.worker_id == worker_id, Attempt.status == "RUNNING")
                ).all()
                for attempt in running_attempts:
                    attempt.status = "TIMEOUT"
                    attempt.failure_reason = "worker lease expired"
                    attempt.finished_at = now_utc()
                    session.add(attempt)
                    session.add(
                        WorkerEvent(
                            project_id=project_id,
                            worker_id=worker_id,
                            intent_id=intent.id,
                            attempt_id=attempt.id,
                            event_type="attempt.timed_out",
                            payload_json={"reason": "worker lease expired", "status": "TIMEOUT"},
                        )
                    )
                    if worker is not None and worker.execution_kind == "primary":
                        timed_out_primary_attempts.append((attempt, worker.budgets or {}))
        session.commit()
        for attempt, budget in timed_out_primary_attempts:
            RoundReflectionService().create(
                session,
                attempt=attempt,
                output={
                    "status": "failed",
                    "summary": attempt.failure_reason or "Solver round timed out.",
                    "failed_attempts": [{"reason": attempt.failure_reason or "worker lease expired"}],
                    "hypotheses": [],
                    "suggested_intents": [],
                    "decision_summary": {"next_tool_plan": []},
                },
                budget=budget,
            )
        return len(expired)

    def reap_all_expired(self, session: Session) -> int:
        """Reap every active project so stale workers cannot survive unattended."""
        project_ids = session.exec(
            select(Project.id).where(Project.status.not_in(["COMPLETED", "CANCELLED", "FAILED"]))
        ).all()
        return sum(self.reap_expired(session, project_id=project_id) for project_id in project_ids)
