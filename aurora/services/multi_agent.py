from __future__ import annotations

from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Condition
from time import monotonic
from typing import Any

from sqlmodel import Session, select

from aurora.db import engine
from aurora.models import ChallengeGroup, ChallengeGroupItem, Intent, Project, ProjectRuntimePolicy, WorkerEvent
from aurora.services.container_control import stop_project_containers
from aurora.services.demo import _run_one_demo_step_claimed, run_one_demo_step
from aurora.services.project_run_control import project_run_control
from aurora.services.agent_runtime import agent_runtime_settings


class _GlobalExplorerCapacity:
    def __init__(self) -> None:
        self._condition = Condition()
        self._active = 0
        self._waiters: deque[object] = deque()

    def reserve(self, requested: int, limit: int, *, timeout_seconds: float = 0.5) -> int:
        waiter = object()
        deadline = monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            self._waiters.append(waiter)
            while self._waiters[0] is not waiter or self._active >= limit:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    self._waiters.remove(waiter)
                    self._condition.notify_all()
                    return 0
                self._condition.wait(remaining)
            granted = max(0, min(requested, limit - self._active))
            self._active += granted
            self._waiters.popleft()
            self._condition.notify_all()
            return granted

    def release(self, count: int) -> None:
        with self._condition:
            self._active = max(0, self._active - count)
            self._condition.notify_all()


_global_explorer_capacity = _GlobalExplorerCapacity()


def _execute_one(project_id: str) -> dict[str, Any]:
    # SQLModel sessions are not thread-safe. Every peer explorer owns a fresh
    # session and communicates with siblings only through committed board data.
    with Session(engine) as worker_session:
        return _run_one_demo_step_claimed(worker_session, project_id=project_id)


def _fair_project_share(session: Session, *, project_id: str, global_limit: int) -> int:
    item = session.exec(
        select(ChallengeGroupItem)
        .where(ChallengeGroupItem.project_id == project_id)
        .order_by(ChallengeGroupItem.updated_at.desc())
    ).first()
    if item is None:
        return global_limit
    group = session.get(ChallengeGroup, item.group_id)
    if group is None:
        return global_limit
    unresolved = session.exec(
        select(ChallengeGroupItem).where(
            ChallengeGroupItem.group_id == group.id,
            ChallengeGroupItem.phase == item.phase,
            ChallengeGroupItem.fused_status.notin_(["COMPLETED", "FAILED"]),
        ).order_by(ChallengeGroupItem.position, ChallengeGroupItem.id)
    ).all()
    # The group runner may dispatch by priority rather than by position.  Once
    # items are marked RUNNING, those items own the group's worker slots; using
    # the first pending positions here can assign every actually dispatched
    # project a share of zero and leave all autorunners waiting forever.
    running = [candidate for candidate in unresolved if candidate.fused_status == "RUNNING"]
    contenders = (running or unresolved)[:max(1, int(group.max_concurrent or 1))]
    contender_ids = [candidate.id for candidate in contenders]
    if item.id not in contender_ids:
        return 0
    quotient, remainder = divmod(global_limit, len(contenders))
    rank = contender_ids.index(item.id)
    return quotient + (1 if rank < remainder else 0)


def run_project_exploration_once(session: Session, *, project_id: str) -> dict[str, Any]:
    claim = project_run_control.acquire(project_id=project_id, owner="multi_agent.step")
    if claim is None:
        return {"status": "busy", "message": "project_run_active"}
    try:
        return run_project_exploration_step(session, project_id=project_id, run_id=claim.run_id)
    finally:
        project_run_control.release(project_id=project_id, run_id=claim.run_id)


def run_project_exploration_step(
    session: Session,
    *,
    project_id: str,
    run_id: str,
    on_dispatch: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if not project_run_control.owns(project_id=project_id, run_id=run_id):
        return {"status": "busy", "message": "project_run_active"}
    runtime = agent_runtime_settings(session)
    policy = session.exec(
        select(ProjectRuntimePolicy).where(ProjectRuntimePolicy.project_id == project_id)
    ).first()
    enabled = bool(
        runtime.multi_agent_exploration_enabled
        and policy is not None
        and policy.multi_agent_exploration_enabled
    )
    if not enabled:
        if on_dispatch is not None:
            on_dispatch()
        return run_one_demo_step(session, project_id=project_id, run_id=run_id)

    pending = session.exec(
        select(Intent).where(Intent.project_id == project_id, Intent.status == "PENDING")
    ).all()
    if not pending:
        return {"status": "idle", "project_id": project_id, "worker_count": 0, "results": []}
    requested = min(len(pending), max(1, policy.max_parallel_explorers))
    requested = min(
        requested,
        _fair_project_share(
            session,
            project_id=project_id,
            global_limit=runtime.max_global_workers,
        ),
    )
    if requested <= 0:
        return {"status": "capacity_wait", "project_id": project_id, "worker_count": 0, "results": []}
    granted = _global_explorer_capacity.reserve(requested, runtime.max_global_workers)
    if granted <= 0:
        return {"status": "capacity_wait", "project_id": project_id, "worker_count": 0, "results": []}

    if on_dispatch is not None:
        on_dispatch()
    session.add(
        WorkerEvent(
            project_id=project_id,
            event_type="multi_agent.batch_started",
            payload_json={"requested": requested, "worker_count": granted, "pending_intents": len(pending)},
        )
    )
    session.commit()
    results: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=granted, thread_name_prefix=f"explore-{project_id}") as pool:
            futures = [pool.submit(_execute_one, project_id) for _ in range(granted)]
            pending_futures = set(futures)
            while pending_futures:
                completed, pending_futures = wait(
                    pending_futures,
                    timeout=0.25,
                    return_when=FIRST_COMPLETED,
                )
                for future in completed:
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        results.append({"status": "runtime_error", "message": str(exc)[:1000]})
                session.expire_all()
                project = session.get(Project, project_id)
                if pending_futures and project is not None and project.status in {
                    "FLAG_READY",
                    "COMPLETED",
                    "FAILED",
                    "CANCELLED",
                }:
                    cancelled = sum(1 for sibling in pending_futures if sibling.cancel())
                    stopped = stop_project_containers(project_id)
                    session.add(
                        WorkerEvent(
                            project_id=project_id,
                            event_type="multi_agent.batch_short_circuited",
                            payload_json={
                                "project_status": project.status,
                                "cancelled_futures": cancelled,
                                "stopped_containers": stopped.get("stopped", []),
                                "stop_errors": stopped.get("errors", []),
                            },
                        )
                    )
                    session.commit()
                    break
    finally:
        _global_explorer_capacity.release(granted)

    session.expire_all()
    statuses = [str(result.get("status") or "") for result in results]
    if statuses and all(status == "idle" for status in statuses):
        status = "idle"
    elif statuses and all(status in {"runtime_error", "runtime_preflight_failed"} for status in statuses):
        status = "runtime_error"
    else:
        status = "multi_agent_batch"
    session.add(
        WorkerEvent(
            project_id=project_id,
            event_type="multi_agent.batch_completed",
            payload_json={"worker_count": granted, "statuses": statuses},
        )
    )
    session.commit()
    return {
        "status": status,
        "project_id": project_id,
        "worker_count": granted,
        "results": results,
        "message": next((result.get("message") for result in results if result.get("message")), None),
    }
