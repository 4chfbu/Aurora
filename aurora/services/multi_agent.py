from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Condition
from time import monotonic
from typing import Any

from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.db import engine
from aurora.models import Intent, ProjectRuntimePolicy, WorkerEvent
from aurora.services.demo import _run_one_demo_step_claimed, run_one_demo_step
from aurora.services.project_run_control import project_run_control


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


def run_project_exploration_once(session: Session, *, project_id: str) -> dict[str, Any]:
    claim = project_run_control.acquire(project_id=project_id, owner="multi_agent.step")
    if claim is None:
        return {"status": "busy", "message": "project_run_active"}
    try:
        return run_project_exploration_step(session, project_id=project_id, run_id=claim.run_id)
    finally:
        project_run_control.release(project_id=project_id, run_id=claim.run_id)


def run_project_exploration_step(session: Session, *, project_id: str, run_id: str) -> dict[str, Any]:
    if not project_run_control.owns(project_id=project_id, run_id=run_id):
        return {"status": "busy", "message": "project_run_active"}
    settings = get_settings()
    policy = session.exec(
        select(ProjectRuntimePolicy).where(ProjectRuntimePolicy.project_id == project_id)
    ).first()
    enabled = bool(
        settings.multi_agent_exploration_enabled
        and policy is not None
        and policy.multi_agent_exploration_enabled
    )
    if not enabled:
        return run_one_demo_step(session, project_id=project_id, run_id=run_id)

    pending = session.exec(
        select(Intent).where(Intent.project_id == project_id, Intent.status == "PENDING")
    ).all()
    if not pending:
        return {"status": "idle", "project_id": project_id, "worker_count": 0, "results": []}
    requested = min(len(pending), max(1, policy.max_parallel_explorers))
    granted = _global_explorer_capacity.reserve(requested, settings.multi_agent_max_global_workers)
    if granted <= 0:
        return {"status": "capacity_wait", "project_id": project_id, "worker_count": 0, "results": []}

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
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({"status": "runtime_error", "message": str(exc)[:1000]})
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
