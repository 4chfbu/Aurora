from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlmodel import Session

from aurora.db import engine
from aurora.models import now_utc
from aurora.services.autorunner import AutoRunLimits, AutoRunResult, AutoRunnerService


@dataclass
class AutoRunTaskState:
    project_id: str
    status: str = "running"
    stop_requested: bool = False
    started_at: datetime = field(default_factory=now_utc)
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class AutoRunRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, AutoRunTaskState] = {}

    def start(self, *, project_id: str, limits: AutoRunLimits) -> AutoRunTaskState:
        with self._lock:
            existing = self._tasks.get(project_id)
            if existing and existing.status == "running":
                return existing
            state = AutoRunTaskState(project_id=project_id)
            self._tasks[project_id] = state

        thread = threading.Thread(target=self._run, args=(project_id, limits, state), daemon=True)
        thread.start()
        return state

    def stop(self, project_id: str) -> AutoRunTaskState | None:
        with self._lock:
            state = self._tasks.get(project_id)
            if state:
                state.stop_requested = True
                if state.status == "running":
                    state.status = "stopping"
            return state

    def status(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._tasks.get(project_id)
            return asdict(state) if state else None

    def wait_for_stop(self, project_id: str, *, timeout_seconds: float = 10) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            with self._lock:
                state = self._tasks.get(project_id)
                if state is None or state.status not in {"running", "stopping"}:
                    return True
            time.sleep(0.1)
        return False

    def _run(self, project_id: str, limits: AutoRunLimits, state: AutoRunTaskState) -> None:
        try:
            with Session(engine) as session:
                result = AutoRunnerService().run_until_stop(
                    session,
                    project_id=project_id,
                    limits=limits,
                    should_stop=lambda: state.stop_requested,
                )
            with self._lock:
                state.status = result.status
                state.finished_at = now_utc()
                state.result = asdict(result)
        except Exception as exc:
            with self._lock:
                state.status = "failed"
                state.finished_at = now_utc()
                state.error = str(exc)


autorun_registry = AutoRunRegistry()
