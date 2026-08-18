from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from aurora.models import Attempt, AttemptCheckpoint, Intent, LLMTrace, ToolTrace, Worker, WorkerEvent, now_utc


TERMINAL_ATTEMPT_STATUSES = {"SUCCESS", "PARTIAL", "FAILED", "TIMEOUT"}


class ReliabilityService:
    def project_report(self, session: Session, *, project_id: str) -> dict:
        attempts = session.exec(select(Attempt).where(Attempt.project_id == project_id)).all()
        checkpoints = session.exec(select(AttemptCheckpoint).where(AttemptCheckpoint.project_id == project_id)).all()
        workers = session.exec(select(Worker).where(Worker.project_id == project_id)).all()
        intents = session.exec(select(Intent).where(Intent.project_id == project_id)).all()
        events = session.exec(select(WorkerEvent).where(WorkerEvent.project_id == project_id)).all()
        traces = session.exec(select(LLMTrace).where(LLMTrace.project_id == project_id)).all()
        tool_traces = session.exec(select(ToolTrace).where(ToolTrace.project_id == project_id)).all()

        checkpoint_attempt_ids = {checkpoint.attempt_id for checkpoint in checkpoints}
        checkpoint_attempt_ids.update(
            event.attempt_id
            for event in events
            if event.attempt_id and event.event_type in {"checkpoint.saved", "checkpoint.failed"}
        )
        terminal = [attempt for attempt in attempts if attempt.status in TERMINAL_ATTEMPT_STATUSES]
        resume_scheduled = sum(event.event_type == "codex.resume_scheduled" for event in events)
        resume_rejected = sum(event.event_type == "codex.resume_rejected" for event in events)
        active_workers = [worker for worker in workers if worker.status in {"RUNNING", "STARTING", "CONCLUDING"}]
        intent_by_id = {intent.id: intent for intent in intents}
        stale_workers: list[str] = []
        for worker in active_workers:
            intent = intent_by_id.get(worker.intent_id)
            owns_state = bool(
                intent
                and intent.status in {"RUNNING", "CONCLUDING"}
                and intent.lease_owner == worker.id
                and intent.lease_generation == worker.lease_generation
                and (intent.status == "CONCLUDING" or self._at_or_after(intent.lease_expires_at, now_utc()))
            )
            if not owns_state:
                stale_workers.append(worker.id)

        model_fallbacks = sum(
            "model metadata not found" in str(trace.provider_usage_json).lower()
            or trace.provider_usage_json.get("model_metadata_source") == "fallback"
            or trace.provider_usage_json.get("model_metadata_warning") is True
            for trace in traces
        )
        checkpointed_terminal = sum(attempt.id in checkpoint_attempt_ids for attempt in terminal)
        resume_total = resume_scheduled + resume_rejected
        return {
            "project_id": project_id,
            "attempts": {
                "total": len(attempts),
                "terminal": len(terminal),
                "finalizing": sum(attempt.status == "FINALIZING" for attempt in attempts),
                "checkpointed_terminal": checkpointed_terminal,
                "checkpoint_coverage": checkpointed_terminal / len(terminal) if terminal else 1.0,
            },
            "runtime_controls": {
                "finalization_started": sum(event.event_type == "attempt.finalization_started" for event in events),
                "soft_deadlines": sum(event.event_type == "attempt.soft_deadline" for event in events),
                "budget_enforced": sum(event.event_type == "attempt.budget_enforced" for event in events),
                "late_outputs_discarded": sum(event.event_type == "attempt.late_output_discarded" for event in events),
            },
            "resume": {
                "scheduled": resume_scheduled,
                "rejected": resume_rejected,
                "verified_rate": resume_scheduled / resume_total if resume_total else None,
            },
            "state": {"active_workers": len(active_workers), "stale_worker_ids": stale_workers},
            "model": {"metadata_fallbacks": model_fallbacks},
            "routes": {
                "codex_actions": sum(trace.tool_name == "codex.shell" for trace in tool_traces),
                "failed_codex_actions": sum(trace.tool_name == "codex.shell" and trace.exit_code not in (None, 0) for trace in tool_traces),
            },
            "gates": {
                "all_terminal_attempts_checkpointed": checkpointed_terminal == len(terminal),
                "no_stale_workers": not stale_workers,
                "no_model_metadata_fallback": model_fallbacks == 0,
            },
        }

    @staticmethod
    def _at_or_after(value: datetime | None, reference: datetime) -> bool:
        if value is None:
            return False
        if value.tzinfo is None and reference.tzinfo is not None:
            reference = reference.replace(tzinfo=None)
        elif value.tzinfo is not None and reference.tzinfo is None:
            value = value.replace(tzinfo=None)
        return value >= reference
