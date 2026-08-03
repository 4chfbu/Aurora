from __future__ import annotations

import threading

from sqlmodel import Session, select

from aurora.db import engine
from aurora.models import Attempt, AuthorizationScope, Hint, Intent, Project, ProjectRuntimePolicy, WorkerEvent, now_utc
from aurora.config import get_settings
from aurora.services.blackboard_repository import BlackboardRepository
from aurora.services.capability_gateway import CapabilityGateway
from aurora.services.context_builder import ContextBuilder
from aurora.services.flag_validator import FlagValidator
from aurora.services.result_processor import ResultProcessor
from aurora.services.round_summary import RoundSummaryService
from aurora.services.scheduler import Scheduler
from aurora.services.subagent_collector import SubagentCollector
from aurora.services.worker_runtime import get_worker_runtime


def _execution_budget(intent: object) -> dict:
    settings = get_settings()
    raw = getattr(intent, "budget", {}) or {}
    return {
        **raw,
        "model_role": raw.get("model_role", "solver"),
        "soft_timeout_seconds": int(raw.get("soft_timeout_seconds", settings.default_soft_timeout_seconds)),
        "hard_timeout_seconds": int(raw.get("hard_timeout_seconds", settings.default_hard_timeout_seconds)),
        "max_tool_calls": int(raw.get("max_tool_calls", settings.default_max_tool_calls)),
        "max_repeat_failures": int(raw.get("max_repeat_failures", settings.default_max_repeat_failures)),
    }


def _lease_seconds_for_intent(intent: object) -> int:
    """Keep the scheduler lease valid for the whole Harness wall-clock budget.

    A lease is a liveness safeguard, not a second (and shorter) execution
    timeout.  The small grace period covers result parsing and persistence
    after the subprocess exits.
    """
    hard_timeout = _execution_budget(intent)["hard_timeout_seconds"]
    return max(600, hard_timeout + 120)


class _WorkerLeaseHeartbeat:
    """Renew a worker lease while its synchronous Harness call is blocking."""

    def __init__(self, *, worker_id: str, lease_seconds: int) -> None:
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"lease-heartbeat-{worker_id}")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        # Renew immediately so the lease includes setup time, then renew at a
        # bounded cadence.  This uses an independent database session because
        # the caller's Session is occupied by the synchronous runtime.
        interval_seconds = min(60, max(15, self.lease_seconds // 4))
        while not self._stop.is_set():
            try:
                with Session(engine) as heartbeat_session:
                    worker = Scheduler().heartbeat(
                        heartbeat_session,
                        worker_id=self.worker_id,
                        lease_seconds=self.lease_seconds,
                    )
                if worker is None:
                    return
            except Exception:
                # Do not crash the solver due to a transient status-write
                # failure; the existing lease still provides a grace window.
                pass
            if self._stop.wait(interval_seconds):
                return


def create_project_with_bootstrap(
    session: Session,
    *,
    name: str,
    goal: str,
    challenge_type: str | None = None,
    allowed_hosts: list[str] | None = None,
    hint: str | None = None,
    subagents_enabled: bool = False,
) -> Project:
    project = Project(name=name, goal=goal, challenge_type=challenge_type)
    session.add(project)
    session.commit()
    session.refresh(project)

    scope = AuthorizationScope(project_id=project.id, allowed_hosts=allowed_hosts or [])
    session.add(scope)
    settings = get_settings()
    session.add(
        ProjectRuntimePolicy(
            project_id=project.id,
            subagents_enabled=bool(subagents_enabled and settings.subagents_enabled),
            max_subagents_per_worker=settings.subagents_max_per_worker,
            max_subagents_concurrent=settings.subagents_max_concurrent,
        )
    )
    session.commit()
    project.authorization_scope_id = scope.id
    session.add(project)
    if hint:
        session.add(Hint(project_id=project.id, content=hint))
    BlackboardRepository().upsert_intent(
        session,
        project_id=project.id,
        objective="Bootstrap the project by validating scope and collecting the first actionable facts.",
        capability_tags=["sandbox.exec", "blackboard.query"],
        priority=1.0,
        risk_level="low",
        budget={"model_role": "planner", "max_tool_calls": 3},
    )
    session.refresh(project)
    return project


def run_one_demo_step(session: Session, *, project_id: str) -> dict:
    project = session.get(Project, project_id)
    if project is not None and project.status == "COMPLETED":
        return {"status": "project_completed", "message": "project is already completed"}

    scheduler = Scheduler()
    next_intent = session.exec(
        select(Intent)
        .where(Intent.project_id == project_id, Intent.status == "PENDING")
        .order_by(Intent.priority.desc(), Intent.created_at)
    ).first()
    if next_intent is None:
        return {"status": "idle", "message": "no pending intents"}
    claimed = scheduler.claim_next(
        session,
        project_id=project_id,
        lease_seconds=_lease_seconds_for_intent(next_intent),
    )
    if claimed is None:
        return {"status": "idle", "message": "no pending intents"}
    intent, worker = claimed
    worker.budgets = _execution_budget(intent)
    worker.agent_profile_id = f"{worker.budgets['model_role']}.general"
    session.add(worker)
    session.add(
        WorkerEvent(
            project_id=project_id,
            worker_id=worker.id,
            intent_id=intent.id,
            event_type="worker.started",
            payload_json={"image": get_settings().default_worker_image, "runtime": get_settings().worker_runtime},
        )
    )
    session.commit()

    attempt = Attempt(project_id=project_id, intent_id=intent.id, worker_id=worker.id)
    session.add(attempt)
    session.commit()
    session.refresh(attempt)

    snapshot = ContextBuilder().build(session, project_id=project_id, intent_id=intent.id, worker_id=worker.id)
    session.add(
        WorkerEvent(
            project_id=project_id,
            worker_id=worker.id,
            intent_id=intent.id,
            attempt_id=attempt.id,
            event_type="context.built",
            payload_json={
                "context_snapshot_id": snapshot.id,
                "estimated_tokens": snapshot.estimated_tokens,
                "total_chars": snapshot.total_chars,
            },
        )
    )
    session.commit()

    lease_heartbeat = _WorkerLeaseHeartbeat(
        worker_id=worker.id,
        lease_seconds=_lease_seconds_for_intent(intent),
    )
    lease_heartbeat.start()
    try:
        runtime_output = get_worker_runtime().execute(session, worker=worker, snapshot=snapshot)
    except Exception as exc:
        lease_heartbeat.stop()
        attempt.status = "FAILED"
        attempt.failure_reason = str(exc)
        attempt.finished_at = now_utc()
        session.add(attempt)
        session.add(
            WorkerEvent(
                project_id=project_id,
                worker_id=worker.id,
                intent_id=intent.id,
                attempt_id=attempt.id,
                event_type="runtime.error",
                payload_json={"error": str(exc), "runtime": "codex"},
            )
        )
        session.commit()
        scheduler.complete(session, intent=intent, worker=worker, status="FAILED")
        return {
            "status": "runtime_error",
            "message": str(exc),
            "project_id": project_id,
            "intent_id": intent.id,
            "worker_id": worker.id,
            "attempt_id": attempt.id,
            "context_snapshot_id": snapshot.id,
        }
    lease_heartbeat.stop()
    session.add(
        WorkerEvent(
            project_id=project_id,
            worker_id=worker.id,
            intent_id=intent.id,
            attempt_id=attempt.id,
            event_type="llm.completed",
            payload_json={
                "llm_trace_id": runtime_output.llm_trace.id,
                "model": runtime_output.llm_trace.model,
                "next_tool_plan": runtime_output.llm_trace.decision_summary.get("next_tool_plan", []),
            },
        )
    )
    session.commit()

    if runtime_output.llm_trace.provider_usage_json.get("exit_code") == 124:
        session.add(
            WorkerEvent(
                project_id=project_id,
                worker_id=worker.id,
                intent_id=intent.id,
                attempt_id=attempt.id,
                event_type="attempt.budget_exhausted",
                payload_json={
                    "budget_type": "wall_clock",
                    "budget": worker.budgets.get("hard_timeout_seconds"),
                    "reason": "worker command timed out",
                },
            )
        )
        session.commit()

    gateway = CapabilityGateway()
    tool_calls = []
    structured = runtime_output.structured_output
    workspace = get_settings().codex_workspace_dir / project_id / worker.id
    reports = SubagentCollector().collect(
        session,
        parent_worker=worker,
        parent_attempt=attempt,
        parent_snapshot=snapshot,
        workspace=workspace,
    )
    if reports:
        structured["subagent_reports"] = reports
        structured.setdefault("artifact_refs", []).extend(
            artifact_id for report in reports for artifact_id in report["artifact_refs"]
        )
        runtime_output.llm_trace.structured_output = structured
        session.add(runtime_output.llm_trace)
        session.commit()
    max_tool_calls = worker.budgets["max_tool_calls"]
    max_repeat_failures = worker.budgets["max_repeat_failures"]
    failed_by_tool: dict[str, int] = {}
    skipped_tools = 0
    raw_tool_requests = structured.get("tool_requests", [])
    valid_tool_requests = [item for item in raw_tool_requests if isinstance(item, dict)] if isinstance(raw_tool_requests, list) else []
    for tool_request in valid_tool_requests[:max_tool_calls]:
        tool_name = str(tool_request.get("tool_name") or "")
        if failed_by_tool.get(tool_name, 0) >= max_repeat_failures:
            skipped_tools += 1
            continue
        activity_label = _activity_label(tool_request)
        session.add(
            WorkerEvent(
                project_id=project_id,
                worker_id=worker.id,
                intent_id=intent.id,
                attempt_id=attempt.id,
                event_type="tool.started",
                payload_json={"tool_name": tool_request["tool_name"], "activity_label": activity_label, "status": "RUNNING"},
            )
        )
        session.commit()
        result = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker.id,
            intent_id=intent.id,
            attempt_id=attempt.id,
            tool_name=tool_request["tool_name"],
            request=tool_request.get("request", {}),
        )
        tool_calls.append(
            {
                "tool_name": tool_request["tool_name"], "activity_label": activity_label,
                "success": result.success,
                "summary": result.summary,
                "artifact_refs": result.artifact_refs,
                "trace_id": result.trace_id,
            }
        )
        if not result.success:
            failed_by_tool[tool_name] = failed_by_tool.get(tool_name, 0) + 1
        session.add(
            WorkerEvent(
                project_id=project_id,
                worker_id=worker.id,
                intent_id=intent.id,
                attempt_id=attempt.id,
                event_type="tool.executed",
                payload_json={
                    "tool_name": tool_request["tool_name"],
                    "success": result.success,
                    "summary": result.summary,
                    "artifact_refs": result.artifact_refs,
                    "trace_id": result.trace_id,
                    "metrics": result.metrics,
                },
            )
        )
        session.commit()
        structured.setdefault("artifact_refs", []).extend(result.artifact_refs)

    requested_tools = len(valid_tool_requests)
    if requested_tools > max_tool_calls or skipped_tools:
        session.add(
            WorkerEvent(
                project_id=project_id, worker_id=worker.id, intent_id=intent.id, attempt_id=attempt.id,
                event_type="attempt.checkpointed",
                payload_json={"reason": "tool_budget_or_repeat_failure", "executed": len(tool_calls), "requested": requested_tools, "skipped": skipped_tools, "budget": worker.budgets},
            )
        )
        session.commit()

    attempt.tool_calls = tool_calls
    if not scheduler.owns_active_lease(session, intent=intent, worker=worker):
        # A background reaper already made the authoritative timeout decision.
        # Do not let a late runtime result create facts or overwrite its state.
        return {
            "status": "timeout",
            "message": "worker lease expired before result publication",
            "project_id": project_id,
            "intent_id": intent.id,
            "worker_id": worker.id,
            "attempt_id": attempt.id,
            "context_snapshot_id": snapshot.id,
            "llm_trace_id": runtime_output.llm_trace.id,
            "tool_calls": tool_calls,
        }
    candidate_flags = FlagValidator().extract_candidate_flags(session, artifact_refs=structured.get("artifact_refs", []), project_id=project_id)
    if candidate_flags:
        structured.setdefault("candidate_flags", []).extend(candidate_flags)
    ResultProcessor().apply(session, attempt=attempt, output=structured, llm_trace=runtime_output.llm_trace)
    checkpoint = RoundSummaryService().create(session, attempt=attempt, output=structured, budget=worker.budgets)
    estimated_tokens = runtime_output.llm_trace.estimated_input_tokens + runtime_output.llm_trace.estimated_output_tokens
    token_budget = worker.budgets.get("token_budget")
    if token_budget and estimated_tokens > int(token_budget):
        session.add(WorkerEvent(project_id=project_id, worker_id=worker.id, intent_id=intent.id, attempt_id=attempt.id, event_type="attempt.budget_exhausted", payload_json={"budget_type": "token", "budget": token_budget, "observed": estimated_tokens}))
        session.commit()
    final_status = "FAILED" if structured.get("status") == "failed" else "COMPLETED"
    scheduler.complete(session, intent=intent, worker=worker, status=final_status)
    return {
        "status": final_status.lower(),
        "project_id": project_id,
        "intent_id": intent.id,
        "worker_id": worker.id,
        "attempt_id": attempt.id,
        "context_snapshot_id": snapshot.id,
        "llm_trace_id": runtime_output.llm_trace.id,
        "checkpoint_id": checkpoint.id,
        "tool_calls": tool_calls,
    }


def _activity_label(tool_request: dict) -> str:
    supplied = str(tool_request.get("activity_label") or "").strip()
    if supplied and len(supplied) <= 120 and not any(secret in supplied.lower() for secret in ("cookie", "token", "password", "authorization", "api_key")):
        return supplied
    tool_name = str(tool_request.get("tool_name") or "")
    labels = {
        "http.request": "正在尝试 HTTP 请求", "sandbox.exec": "正在运行受控命令", "browser.interact": "正在检查并启动靶机",
        "python.analyze": "正在尝试 Python 分析", "php.unserialize": "正在尝试反序列化", "blackboard.query": "正在查询解题上下文",
    }
    return labels.get(tool_name, f"正在执行 {tool_name or '工具操作'}")
