from __future__ import annotations

import hashlib
import threading

from sqlmodel import Session, select

from aurora.db import engine
from aurora.models import Attempt, AuthorizationScope, Hint, Intent, Project, ProjectCoordinationState, ProjectRuntimePolicy, ToolTrace, WorkerEvent, now_utc
from aurora.config import get_settings
from aurora.services.blackboard_repository import BlackboardRepository, route_fingerprint
from aurora.services.browser_sessions import browser_session_registry
from aurora.services.capability_gateway import CapabilityGateway
from aurora.services.context_builder import ContextBuilder
from aurora.services.flag_validator import FlagValidator
from aurora.services.result_processor import ResultProcessor
from aurora.services.round_summary import RoundReflectionService
from aurora.services.scheduler import Scheduler
from aurora.services.subagent_collector import SubagentCollector
from aurora.services.worker_runtime import get_worker_runtime
from aurora.services.tool_profiles import worker_preflight
from aurora.services.project_run_control import project_run_control


def _execution_budget(intent: object) -> dict:
    settings = get_settings()
    raw = getattr(intent, "budget", {}) or {}
    phase = int(raw.get("phase", 1) or 1)
    # The Codex harness drives its own action loop and only writes to the
    # blackboard at end-of-turn, so a "no progress" heuristic that counts
    # mid-turn blackboard writes kills legitimate local analysis (crypto
    # factoring, pwn RE) after a few shell commands. Disable it by default;
    # the group runner (ChallengeGroupRunner._apply_phase_attempt_budget)
    # already does the same. The per-attempt shell-action budget
    # (max_agent_actions) is also disabled by default; soft/hard timeout
    # is the real wall-clock bound. max_route_repeats still kills repeated
    # failed routes. Set max_agent_actions on an intent to opt back in.
    phase_defaults = {
        1: {"soft_timeout_seconds": 3600, "hard_timeout_seconds": 5400, "max_agent_actions": 0, "max_no_progress_actions": 0, "max_route_repeats": 2, "model_role": "triage"},
        2: {"soft_timeout_seconds": 3600, "hard_timeout_seconds": 5400, "max_agent_actions": 0, "max_no_progress_actions": 0, "max_route_repeats": 2, "model_role": "solver"},
        3: {"soft_timeout_seconds": 3600, "hard_timeout_seconds": 5400, "max_agent_actions": 0, "max_no_progress_actions": 0, "max_route_repeats": 2, "model_role": "solver"},
        4: {"soft_timeout_seconds": 3600, "hard_timeout_seconds": 5400, "max_agent_actions": 0, "max_no_progress_actions": 0, "max_route_repeats": 2, "model_role": "reviewer"},
    }.get(phase, {})
    budget = {
        **raw,
        "model_role": raw.get("model_role", phase_defaults.get("model_role", "solver")) if raw.get("model_role", phase_defaults.get("model_role", "solver")) in {"triage", "planner", "solver", "reviewer"} else "solver",
        "phase": phase,
        "soft_timeout_seconds": int(raw.get("soft_timeout_seconds", phase_defaults.get("soft_timeout_seconds", settings.default_soft_timeout_seconds))),
        "hard_timeout_seconds": int(raw.get("hard_timeout_seconds", phase_defaults.get("hard_timeout_seconds", settings.default_hard_timeout_seconds))),
        "max_tool_calls": int(raw.get("max_tool_calls", settings.default_max_tool_calls)),
        "max_repeat_failures": int(raw.get("max_repeat_failures", settings.default_max_repeat_failures)),
        "max_agent_actions": int(raw.get("max_agent_actions", phase_defaults.get("max_agent_actions", settings.default_max_agent_actions))),
        "max_route_repeats": int(raw.get("max_route_repeats", phase_defaults.get("max_route_repeats", settings.default_max_route_repeats))),
        "max_no_progress_actions": int(raw.get("max_no_progress_actions", phase_defaults.get("max_no_progress_actions", settings.default_max_no_progress_actions))),
        "finalize_grace_seconds": int(raw.get("finalize_grace_seconds", settings.default_finalize_grace_seconds)),
    }
    if budget["hard_timeout_seconds"] <= budget["soft_timeout_seconds"]:
        budget["hard_timeout_seconds"] = budget["soft_timeout_seconds"] + max(60, budget["finalize_grace_seconds"])
    return budget


def _lease_seconds_for_intent(intent: object) -> int:
    """Keep the scheduler lease valid for the whole Harness wall-clock budget.

    A lease is a liveness safeguard, not a second (and shorter) execution
    timeout.  The small grace period covers result parsing and persistence
    after the subprocess exits.
    """
    hard_timeout = _execution_budget(intent)["hard_timeout_seconds"]
    return max(600, hard_timeout + 120)


def _select_parent_attempt(session: Session, *, project_id: str, intent: Intent) -> Attempt | None:
    """Select the newest resumable project state, preferring the explicit parent."""
    terminal_statuses = ["SUCCESS", "COMPLETED", "PARTIAL", "FAILED", "TIMEOUT"]
    resumable = (
        Attempt.project_id == project_id,
        Attempt.codex_thread_id.is_not(None),
        Attempt.resume_manifest_artifact_id.is_not(None),
        Attempt.status.in_(terminal_statuses),
    )
    retry = session.exec(
        select(Attempt)
        .where(*resumable, Attempt.intent_id == intent.id)
        .order_by(Attempt.started_at.desc())
    ).first()
    if retry is not None:
        return retry
    policy = session.exec(
        select(ProjectRuntimePolicy).where(ProjectRuntimePolicy.project_id == project_id)
    ).first()
    if policy is not None and policy.multi_agent_exploration_enabled:
        # Peer branches share facts and artifacts, never another branch's
        # conversational state or worker-local filesystem assumptions.
        return None
    if intent.parent_intent_id:
        parent = session.exec(
            select(Attempt)
            .where(*resumable, Attempt.intent_id == intent.parent_intent_id)
            .order_by(Attempt.started_at.desc())
        ).first()
        if parent is not None:
            return parent
    return session.exec(
        select(Attempt)
        .where(*resumable, Attempt.intent_id != intent.id)
        .order_by(Attempt.started_at.desc())
    ).first()


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
    multi_agent_exploration_enabled: bool = False,
    max_parallel_explorers: int | None = None,
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
            multi_agent_exploration_enabled=bool(
                multi_agent_exploration_enabled and settings.multi_agent_exploration_enabled
            ),
            max_parallel_explorers=min(
                max_parallel_explorers or settings.multi_agent_max_project_workers,
                settings.multi_agent_max_global_workers,
            ),
            max_reason_intents=settings.multi_agent_max_reason_intents,
            max_pending_intents=settings.multi_agent_max_pending_intents,
        )
    )
    session.add(ProjectCoordinationState(project_id=project.id))
    session.commit()
    project.authorization_scope_id = scope.id
    session.add(project)
    if hint:
        session.add(Hint(project_id=project.id, content=hint))
        coordination = session.exec(
            select(ProjectCoordinationState).where(ProjectCoordinationState.project_id == project.id)
        ).one()
        coordination.graph_version += 1
        session.add(coordination)
    BlackboardRepository().upsert_intent(
        session,
        project_id=project.id,
        objective="Bootstrap the project by validating scope and collecting the first actionable facts.",
        capability_tags=["sandbox.exec", "blackboard.query"],
        priority=1.0,
        risk_level="low",
        budget={"model_role": "triage", "phase": 1, "max_tool_calls": 3},
    )
    session.refresh(project)
    return project


def run_one_demo_step(session: Session, *, project_id: str, run_id: str | None = None) -> dict:
    claim = None
    if run_id is None:
        claim = project_run_control.acquire(project_id=project_id, owner="worker.step")
        if claim is None:
            return {"status": "busy", "message": "project_run_active"}
        run_id = claim.run_id
    elif not project_run_control.owns(project_id=project_id, run_id=run_id):
        return {"status": "busy", "message": "project_run_active"}
    try:
        return _run_one_demo_step_claimed(session, project_id=project_id)
    finally:
        if claim is not None:
            project_run_control.release(project_id=project_id, run_id=claim.run_id)


def _run_one_demo_step_claimed(session: Session, *, project_id: str) -> dict:
    project = session.get(Project, project_id)
    if project is not None and project.status == "COMPLETED":
        return {"status": "project_completed", "message": "project is already completed"}
    if project is None:
        return {"status": "project_missing", "message": "project not found"}

    preflight = worker_preflight(get_settings(), project.challenge_type)
    session.add(WorkerEvent(project_id=project_id, event_type="worker.preflight", payload_json=preflight))
    session.commit()
    if not preflight["ready"]:
        return {
            "status": "runtime_preflight_failed",
            "message": f"Worker image preflight failed for {preflight['image']} ({preflight['profile']}): {preflight['error']}. Build with {preflight['build_command']}",
            "preflight": preflight,
        }

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
    runtime_policy = session.exec(
        select(ProjectRuntimePolicy).where(ProjectRuntimePolicy.project_id == project_id)
    ).first()
    if (
        runtime_policy is not None
        and runtime_policy.multi_agent_exploration_enabled
        and not intent.objective.startswith("Bootstrap the project")
    ):
        worker.execution_kind = "explorer"
    session.add(worker)
    session.add(
        WorkerEvent(
            project_id=project_id,
            worker_id=worker.id,
            intent_id=intent.id,
            event_type="worker.started",
            payload_json={"image": preflight["image"], "profile": preflight["profile"], "runtime": get_settings().worker_runtime, "execution_kind": worker.execution_kind},
        )
    )
    session.commit()

    parent_attempt = _select_parent_attempt(session, project_id=project_id, intent=intent)
    attempt = Attempt(
        project_id=project_id,
        intent_id=intent.id,
        worker_id=worker.id,
        parent_attempt_id=parent_attempt.id if parent_attempt else None,
        lease_generation=worker.lease_generation,
    )
    session.add(attempt)
    session.commit()
    session.refresh(attempt)
    if parent_attempt is not None and parent_attempt.intent_id != intent.parent_intent_id:
        session.add(
            WorkerEvent(
                project_id=project_id,
                worker_id=worker.id,
                intent_id=intent.id,
                attempt_id=attempt.id,
                event_type="attempt.parent_fallback_selected",
                payload_json={
                    "parent_attempt_id": parent_attempt.id,
                    "parent_intent_id": parent_attempt.intent_id,
                    "requested_parent_intent_id": intent.parent_intent_id,
                },
            )
        )
        session.commit()

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
        checkpoint = RoundReflectionService().create(
            session,
            attempt=attempt,
            output={
                "status": "failed",
                "summary": f"Solver runtime failed: {exc}",
                "failed_attempts": [{"reason": str(exc)}],
                "hypotheses": [],
                "suggested_intents": [],
                "decision_summary": {"next_tool_plan": []},
            },
            budget=worker.budgets,
        )
        scheduler.complete(session, intent=intent, worker=worker, status="FAILED")
        return {
            "status": "runtime_error",
            "message": str(exc),
            "project_id": project_id,
            "intent_id": intent.id,
            "worker_id": worker.id,
            "attempt_id": attempt.id,
            "context_snapshot_id": snapshot.id,
            "checkpoint_id": checkpoint.id,
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
    max_agent_actions = int(worker.budgets.get("max_agent_actions", 0) or 0)
    internal_action_count = len(session.exec(select(ToolTrace).where(ToolTrace.attempt_id == attempt.id, ToolTrace.tool_name == "codex.shell")).all())
    skipped_tools = 0
    raw_tool_requests = structured.get("tool_requests", [])
    valid_tool_requests = [item for item in raw_tool_requests if isinstance(item, dict)] if isinstance(raw_tool_requests, list) else []
    if max_agent_actions and internal_action_count >= max_agent_actions:
        session.add(WorkerEvent(
            project_id=project_id,
            worker_id=worker.id,
            intent_id=intent.id,
            attempt_id=attempt.id,
            event_type="attempt.finalization_started",
            payload_json={
                "reason": "max_agent_actions",
                "observed": internal_action_count,
                "budget": max_agent_actions,
                "gateway_requests_preserved": len(valid_tool_requests),
            },
        ))
        session.commit()
    # Verification is a post-solver gate, not another exploratory action.
    # Run it first so a valid derivation cannot be dropped merely because the
    # model also returned enough ordinary requests to fill the tool budget.
    valid_tool_requests.sort(key=lambda item: {"flag.verify": 0, "flag.submit": 1}.get(item.get("tool_name"), 2))
    for tool_request in valid_tool_requests[:max_tool_calls]:
        tool_name = str(tool_request.get("tool_name") or "")
        request = _route_request(project_id, tool_name, tool_request.get("request", {}))
        repeat_failures = _repeat_failure_count(
            session,
            project_id=project_id,
            tool_name=tool_name,
            request=request,
        )
        route_repeat_budget = int(worker.budgets.get("max_route_repeats", max_repeat_failures) or max_repeat_failures)
        repeat_limit = min(max_repeat_failures, route_repeat_budget) if max_repeat_failures and route_repeat_budget else max(max_repeat_failures, route_repeat_budget)
        if repeat_failures >= repeat_limit:
            skipped_tools += 1
            summary = f"skipped repeated failed route after {repeat_failures} failure(s)"
            tool_calls.append({
                "tool_name": tool_name,
                "activity_label": _activity_label(tool_request),
                "success": False,
                "skipped": True,
                "summary": summary,
                "artifact_refs": [],
                "trace_id": None,
            })
            structured.setdefault("failed_attempts", []).append({
                "reason": "repeat_failure_suppressed",
                "tool_name": tool_name,
                "summary": summary,
            })
            session.add(WorkerEvent(
                project_id=project_id,
                worker_id=worker.id,
                intent_id=intent.id,
                attempt_id=attempt.id,
                event_type="tool.skipped.repeat_failure",
                payload_json={
                    "tool_name": tool_name,
                    "request": request,
                    "failure_count": repeat_failures,
                    "max_repeat_failures": max_repeat_failures,
                },
            ))
            session.commit()
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
            request=request,
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
        if tool_request["tool_name"] == "flag.verify" and not result.success:
            structured.setdefault("failed_attempts", []).append({
                "reason": "flag_verification_failed",
                "tool_name": "flag.verify",
                "summary": result.summary,
                "artifact_refs": result.artifact_refs,
            })
            structured.setdefault("suggested_intents", []).append({
                "objective": f"Repair the rejected flag verification workflow: {result.summary[:500]}",
                "expected_observation": "Two isolated replays exit successfully and print the same single flag derived from declared input artifacts.",
                "capabilities": ["flag.verify", "sandbox.exec", "blackboard.query"],
                "priority": 1.6,
                "risk_level": "low",
                "budget": {"model_role": "reviewer", "phase": 4},
            })

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

    # Native Codex shell calls are persisted asynchronously while the Harness
    # streams. The model only sees workspace paths, not the server-side
    # Artifact IDs created for those calls, so merge them before evidence and
    # flag processing. FlagValidator still applies the trusted-origin gate.
    shell_artifact_refs = _attempt_shell_artifact_refs(session, attempt.id)
    if shell_artifact_refs:
        model_artifact_refs = [
            ref
            for ref in (structured.get("artifact_refs", []) if isinstance(structured.get("artifact_refs"), list) else [])
            if isinstance(ref, str) and ref
        ]
        structured["artifact_refs"] = list(dict.fromkeys([
            *model_artifact_refs,
            *shell_artifact_refs,
        ]))
        runtime_output.llm_trace.structured_output = structured
        session.add(runtime_output.llm_trace)
        session.commit()

    attempt.tool_calls = tool_calls
    if not scheduler.owns_active_lease(session, intent=intent, worker=worker):
        # A background reaper already made the authoritative timeout decision.
        # Do not let a late runtime result create facts or overwrite its state.
        session.add(
            WorkerEvent(
                project_id=project_id,
                worker_id=worker.id,
                intent_id=intent.id,
                attempt_id=attempt.id,
                event_type="attempt.late_output_discarded",
                payload_json={"llm_trace_id": runtime_output.llm_trace.id, "tool_call_count": len(tool_calls), "reason": "lease_no_longer_owned"},
            )
        )
        session.commit()
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
    checkpoint = RoundReflectionService().create(
        session,
        attempt=attempt,
        output=structured,
        budget=worker.budgets,
        # The Solver already returns its evidence, next step, and continuation
        # Intent. A second model pass here used to rewrite that plan between
        # turns and made the outer loop part of the solving process.
        skip_planner=True,
        # Cairn-style peer Explorers report facts only. The fenced Reason pass
        # owns graph expansion, preventing every branch from recursively
        # multiplying its own suggestions.
        author_intents=worker.execution_kind != "explorer",
    )
    estimated_tokens = runtime_output.llm_trace.estimated_input_tokens + runtime_output.llm_trace.estimated_output_tokens
    token_budget = worker.budgets.get("token_budget")
    if token_budget and estimated_tokens > int(token_budget):
        session.add(WorkerEvent(project_id=project_id, worker_id=worker.id, intent_id=intent.id, attempt_id=attempt.id, event_type="attempt.budget_exhausted", payload_json={"budget_type": "token", "budget": token_budget, "observed": estimated_tokens}))
        session.commit()
    final_status = "FAILED" if structured.get("status") == "failed" else "COMPLETED"
    scheduler.complete(session, intent=intent, worker=worker, status=final_status)
    failed_attempts = structured.get("failed_attempts", [])
    if not isinstance(failed_attempts, list):
        failed_attempts = []
    failure_reasons = {
        str(item.get("reason") or "")
        for item in failed_attempts
        if isinstance(item, dict)
    }
    runtime_unavailable = "provider_unavailable" in failure_reasons
    if runtime_unavailable:
        message = "CC Switch proxy is unavailable; restore its health before starting another Worker."
        session.add(
            WorkerEvent(
                project_id=project_id,
                worker_id=worker.id,
                intent_id=intent.id,
                attempt_id=attempt.id,
                event_type="runtime.preflight_failed",
                payload_json={"reason": "provider_unavailable", "message": message},
            )
        )
        session.commit()
    return {
        "status": "runtime_preflight_failed" if runtime_unavailable else final_status.lower(),
        "message": message if runtime_unavailable else None,
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


def _attempt_shell_artifact_refs(session: Session, attempt_id: str) -> list[str]:
    """Return server-side Artifact IDs emitted by native shell actions."""
    refs: list[str] = []
    traces = session.exec(
        select(ToolTrace).where(
            ToolTrace.attempt_id == attempt_id,
            ToolTrace.tool_name == "codex.shell",
        ).order_by(ToolTrace.created_at)
    ).all()
    for trace in traces:
        for artifact_ref in trace.artifact_refs or []:
            if isinstance(artifact_ref, str) and artifact_ref and artifact_ref not in refs:
                refs.append(artifact_ref)
    return refs


def _route_request(project_id: str, tool_name: str, request: object) -> dict:
    normalized = dict(request) if isinstance(request, dict) else {}
    if tool_name != "browser.interact":
        return normalized
    browser_session = browser_session_registry.get_project_session(project_id)
    if browser_session is not None:
        session_material = f"{browser_session.source_url}\0{browser_session.cookie}".encode("utf-8")
        normalized["_aurora_browser_session"] = hashlib.sha256(session_material).hexdigest()[:16]
    return normalized


def _repeat_failure_count(
    session: Session,
    *,
    project_id: str,
    tool_name: str,
    request: dict,
) -> int:
    traces = session.exec(
        select(ToolTrace).where(
            ToolTrace.project_id == project_id,
            ToolTrace.tool_name == tool_name,
        )
    ).all()
    fingerprint = route_fingerprint(request)
    return sum(
        1
        for trace in traces
        if route_fingerprint(trace.request_json) == fingerprint
        and (trace.policy_decision != "allow" or (trace.exit_code is not None and trace.exit_code != 0))
    )
