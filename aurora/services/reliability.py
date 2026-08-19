from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from aurora.models import Attempt, AttemptCheckpoint, FlagCandidate, Intent, LLMTrace, ToolTrace, Worker, WorkerEvent, now_utc
from aurora.services.blackboard_repository import route_fingerprint


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
        candidates = session.exec(select(FlagCandidate).where(FlagCandidate.project_id == project_id)).all()

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
        actions = [trace for trace in tool_traces if trace.tool_name == "codex.shell"]
        progress_events = [
            event for event in events
            if event.event_type in {"checkpoint.saved", "blackboard.fact_appended", "artifact.read", "hypothesis.eliminated"}
        ]
        request_counts: dict[tuple[str, str], int] = {}
        for trace in tool_traces:
            key = (trace.tool_name, route_fingerprint(trace.request_json))
            request_counts[key] = request_counts.get(key, 0) + 1
        redundant_requests = sum(max(0, count - 1) for count in request_counts.values())
        derived_candidates = [
            candidate
            for candidate in candidates
            if candidate.provenance_kind.upper() in {"DERIVED_REPLAY", "VERIFIED_REPLAY"}
        ]
        verified_derived = [candidate for candidate in derived_candidates if candidate.verification_artifact_ref]
        rejected_candidates = [candidate for candidate in candidates if candidate.status == "REJECTED"]
        verify_calls = [trace for trace in tool_traces if trace.tool_name == "flag.verify"]
        dropped_non_privileged = sum(event.event_type == "tool.skipped.non_privileged" for event in events)
        mcp_server_events = [event for event in events if event.event_type == "mcp.server.observed"]
        mcp_servers_observed = sorted({
            server
            for event in mcp_server_events
            for server in (event.payload_json or {}).get("servers", [])
        })
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
            "progress": {
                "evidence_events": len(progress_events),
                "codex_actions": len(actions),
                "evidence_per_action": len(progress_events) / len(actions) if actions else None,
            },
            "state": {"active_workers": len(active_workers), "stale_worker_ids": stale_workers},
            "model": {"metadata_fallbacks": model_fallbacks},
            "routes": {
                "codex_actions": len(actions),
                "failed_codex_actions": sum(trace.tool_name == "codex.shell" and trace.exit_code not in (None, 0) for trace in tool_traces),
                "redundant_requests": redundant_requests,
                "redundant_request_rate": redundant_requests / len(tool_traces) if tool_traces else 0.0,
            },
            "verification": {
                "flag_verify_calls": len(verify_calls),
                "derived_candidates": len(derived_candidates),
                "verified_derived_candidates": len(verified_derived),
                "derived_verification_coverage": len(verified_derived) / len(derived_candidates) if derived_candidates else 1.0,
                "rejected_candidates": len(rejected_candidates),
                "rejected_candidate_rate": len(rejected_candidates) / len(candidates) if candidates else 0.0,
            },
            "tool_contract": {
                "dropped_non_privileged_requests": dropped_non_privileged,
                "mcp_servers_observed": mcp_servers_observed,
                "capability_request_escalations": sum(event.event_type == "operator.escalated" for event in events),
            },
            "gates": {
                "all_terminal_attempts_checkpointed": checkpointed_terminal == len(terminal),
                "no_stale_workers": not stale_workers,
                "no_model_metadata_fallback": model_fallbacks == 0,
                "all_derived_candidates_verified": len(verified_derived) == len(derived_candidates),
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
