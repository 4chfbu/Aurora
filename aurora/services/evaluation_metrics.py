from __future__ import annotations

from aurora.models import Attempt, AttemptCheckpoint, ChallengeGroupItem, FlagCandidate, LLMTrace, WorkerEvent, now_utc
from aurora.services.deadlines import as_utc


def handoff_metrics(attempts: list[Attempt], checkpoints: list[AttemptCheckpoint], events: list[WorkerEvent]) -> dict:
    followups = {(checkpoint.attempt_id, intent_id): checkpoint.created_at for checkpoint in checkpoints for intent_id in checkpoint.generated_intent_ids}
    consumed = {
        (attempt.parent_attempt_id, attempt.intent_id) for attempt in attempts
        if (attempt.parent_attempt_id, attempt.intent_id) in followups
        and as_utc(attempt.started_at) >= as_utc(followups[(attempt.parent_attempt_id, attempt.intent_id)])
    }
    scheduled = {
        (event.attempt_id, event.payload_json.get("thread_id"))
        for event in events if event.event_type == "codex.resume_scheduled" and event.payload_json.get("thread_id")
    }
    started = {
        (event.attempt_id, event.payload_json.get("thread_id"))
        for event in events if event.event_type == "codex.session_started" and event.payload_json.get("thread_id")
    }
    routes = [route for checkpoint in checkpoints for route in checkpoint.failed_routes]
    usable = sum(isinstance(route, str) and bool(route.strip()) for route in routes)
    return {
        "continuations_generated": len(followups), "continuations_consumed": len(consumed),
        "continuation_consumption_rate": len(consumed) / len(followups) if followups else None,
        "resume_started": len(started & scheduled),
        "failed_routes": len(routes), "usable_failed_routes": usable,
        "usable_failed_route_rate": usable / len(routes) if routes else None,
    }


def runtime_metrics(traces: list[LLMTrace]) -> dict:
    measured = []
    for trace in traces:
        metadata = trace.provider_usage_json or {}
        usage = metadata.get("usage") if metadata.get("runtime") == "codex" else metadata
        if isinstance(usage, dict) and any(key in usage for key in ("input_tokens", "prompt_tokens")):
            measured.append(usage)
    return {
        "runtime_traces": len(traces), "runtime_usage_measured": len(measured),
        "runtime_usage_coverage": len(measured) / len(traces) if traces else None,
        "runtime_usage_complete": sum(trace.provider_usage_json.get("usage_complete") is True for trace in traces),
        "runtime_input_tokens": sum(usage.get("input_tokens", usage.get("prompt_tokens", 0)) for usage in measured),
        "runtime_output_tokens": sum(usage.get("output_tokens", usage.get("completion_tokens", 0)) for usage in measured),
        "runtime_cached_input_tokens": sum(usage.get("cached_input_tokens", 0) for usage in measured),
        "runtime_setup_ms": sum((trace.provider_usage_json.get("timing_ms") or {}).get("setup", 0) for trace in traces),
        "runtime_finalization_ms": sum((trace.provider_usage_json.get("timing_ms") or {}).get("finalization", 0) for trace in traces),
    }


def candidate_latencies(candidates: list[FlagCandidate], events: list[WorkerEvent]) -> list[float]:
    created = {candidate.id: candidate.created_at for candidate in candidates}
    latencies = {}
    for event in events:
        candidate_id = event.payload_json.get("candidate_id")
        decided = event.event_type == "flag.platform_decided" or (
            event.event_type == "project.completed" and event.payload_json.get("reason") == "competition platform accepted flag"
        )
        if decided and candidate_id in created:
            latency = max(0.0, (as_utc(event.created_at) - as_utc(created[candidate_id])).total_seconds())
            latencies[candidate_id] = min(latencies.get(candidate_id, latency), latency)
    return list(latencies.values())


def scheduling_metrics(attempts: list[Attempt], item: ChallengeGroupItem | None) -> dict:
    meta = (item.competition_meta or {}) if item else {}
    waited = float(meta.get("resource_wait_seconds", 0))
    paused_at = (meta.get("resource_retry") or {}).get("paused_at")
    if paused_at:
        ended = item.finished_at or now_utc()
        waited += max(0.0, (as_utc(ended) - as_utc(paused_at)).total_seconds())
    return {
        "zero_attempt": not attempts,
        "worker_elapsed_seconds": sum(max(0.0, (as_utc(attempt.finished_at) - as_utc(attempt.started_at)).total_seconds())
                                      for attempt in attempts if attempt.finished_at),
        "resource_wait_seconds": waited,
    }
