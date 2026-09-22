import json
from datetime import timedelta

from sqlmodel import Session, select

from aurora.db import engine
from aurora.models import Attempt, AttemptCheckpoint, ChallengeGroupItem, FlagCandidate, LLMTrace, Worker, WorkerEvent, now_utc
from aurora.services.evaluation_metrics import candidate_latencies, handoff_metrics, runtime_metrics, scheduling_metrics
from aurora.services.worker_runtime import CodexHarnessRuntime
from aurora.services.runtime_usage import session_token_usage, session_usage_delta
from aurora.models import EvaluationItemResult, EvaluationRun, EvaluationSuite
from aurora.services.evaluation import EvaluationService


def test_handoff_metrics_require_actual_consumption_and_native_thread_start():
    created = now_utc()
    checkpoint = AttemptCheckpoint(project_id="project", intent_id="original", attempt_id="parent", summary="handoff", generated_intent_ids=["child", "unused"], failed_routes=["", "connection refused"], created_at=created)
    attempt = Attempt(project_id="project", intent_id="child", parent_attempt_id="parent", worker_id="worker", started_at=created + timedelta(seconds=1))
    events = [WorkerEvent(project_id="project", attempt_id=attempt.id, event_type="codex.resume_scheduled", payload_json={"thread_id": "thread"})]
    metrics = handoff_metrics([attempt], [checkpoint], events)
    assert metrics["continuations_consumed"] == 1 and metrics["continuation_consumption_rate"] == 0.5
    assert metrics["resume_started"] == 0
    assert metrics["usable_failed_route_rate"] == 0.5
    events.append(WorkerEvent(project_id="project", attempt_id=attempt.id, event_type="codex.session_started", payload_json={"thread_id": "thread"}))
    assert handoff_metrics([attempt], [checkpoint], events)["resume_started"] == 1
    attempt.parent_attempt_id = "other"
    assert handoff_metrics([attempt], [checkpoint], events)["continuations_consumed"] == 0


def test_runtime_preserves_observed_token_usage_without_estimating_missing_data():
    with Session(engine) as session:
        worker = Worker(project_id="project", intent_id="intent")
        attempt = Attempt(project_id=worker.project_id, intent_id=worker.intent_id, worker_id=worker.id)
        session.add_all([worker, attempt])
        session.commit()
        for usage in ({"input_tokens": 142070, "cached_input_tokens": 134144, "output_tokens": 1723}, {"input_tokens": 200, "output_tokens": 10, "invalid_tokens": -1}):
            CodexHarnessRuntime._record_codex_event(session, worker=worker, attempt=attempt, stream="stdout", line=json.dumps({"type": "turn.completed", "usage": usage}))
        assert attempt.token_usage == {"input_tokens": 142270, "cached_input_tokens": 134144, "output_tokens": 1733}
        assert len(session.exec(select(WorkerEvent).where(WorkerEvent.event_type == "codex.usage")).all()) == 2
        traces = [LLMTrace(project_id="project", worker_id=worker.id, intent_id="intent", context_snapshot_id="snapshot", prompt_hash="hash", provider_usage_json=usage) for usage in ({"runtime": "codex", "usage": attempt.token_usage}, {"runtime": "codex"})]
        metrics = runtime_metrics(traces)
        assert metrics["runtime_usage_coverage"] == 0.5
        assert metrics["runtime_input_tokens"] == 142270


def test_candidate_latency_includes_partial_and_rejected_platform_decisions():
    created = now_utc()
    candidates = [FlagCandidate(project_id="project", value="flag{test}", value_hash="hash", created_at=created)]
    events = [WorkerEvent(project_id="project", event_type="flag.platform_decided", payload_json={"candidate_id": candidates[0].id, "accepted": False}, created_at=created + timedelta(seconds=9))]
    assert candidate_latencies(candidates, events) == [9.0]


def test_interrupted_session_usage_excludes_resumed_history_and_repeated_totals(tmp_path):
    sessions = tmp_path / "runtime" / "codex-home" / "sessions"
    sessions.mkdir(parents=True)
    path = sessions / "rollout-thread.jsonl"

    def event(input_tokens, output_tokens):
        return json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }}}) + "\n"

    path.write_text(event(100, 10))
    before = session_token_usage(tmp_path, "thread")
    with path.open("a") as stream:
        stream.write(event(150, 15) + event(150, 15) + event(180, 18) + '{"interrupted":')
    assert session_usage_delta(before, session_token_usage(tmp_path, "thread")) == {"input_tokens": 80, "output_tokens": 8}
    (sessions / "rollout-otherthread.jsonl").write_text(event(10000, 1000))
    assert session_token_usage(tmp_path, "thread")["input_tokens"] == 180
    assert session_token_usage(tmp_path, "unrelated") == {}
    assert session_usage_delta(before, {"input_tokens": 50, "output_tokens": 5}) == {}


def test_scheduling_metrics_separate_waiting_and_parallel_worker_time():
    started = now_utc()
    item = ChallengeGroupItem(group_id="group", project_id="project", position=0,
                              finished_at=started + timedelta(seconds=50),
                              competition_meta={"resource_wait_seconds": 20, "resource_retry": {"paused_at": started.isoformat()}})
    attempts = [Attempt(project_id="project", intent_id="intent", worker_id=str(index),
                        started_at=started, finished_at=started + timedelta(seconds=30)) for index in range(2)]
    assert scheduling_metrics(attempts, item) == {"zero_attempt": False, "worker_elapsed_seconds": 60.0, "resource_wait_seconds": 70.0}
    assert scheduling_metrics([], item)["zero_attempt"] is True


def test_comparison_discloses_and_uses_matching_repeat_metric_versions():
    with Session(engine) as session:
        suite = EvaluationSuite(name="metric versions", content_hash="frozen")
        baseline = EvaluationRun(suite_id=suite.id, label="before", variant="baseline", status="COMPLETED")
        candidate = EvaluationRun(suite_id=suite.id, label="after", variant="candidate", status="COMPLETED")
        before = EvaluationItemResult(run_id=baseline.id, challenge_key="same", metrics_json={"metrics_version": 2, "repeated_requests": 100, "repeated_experiments": 2})
        after = EvaluationItemResult(run_id=candidate.id, challenge_key="same", metrics_json={"metrics_version": 2, "repeated_requests": 0, "repeated_experiments": 3})
        session.add_all([suite, baseline, candidate, before, after])
        session.commit()
        comparison = EvaluationService().compare(session, baseline_run_id=baseline.id, candidate_run_id=candidate.id)
        assert comparison.repeated_request_metric == "repeated_experiments"
        assert comparison.repeated_request_reduction == -0.5
        before.metrics_json = {"repeated_requests": 100}
        session.add(before)
        session.commit()
        comparison = EvaluationService().compare(session, baseline_run_id=baseline.id, candidate_run_id=candidate.id)
        assert comparison.repeated_request_metric == "repeated_requests"
