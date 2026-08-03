from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlmodel import Session

from aurora.db import engine
from aurora.models import Project, WorkerEvent, now_utc
from aurora.services.autorun_registry import autorun_registry
from aurora.services.autorunner import AutoRunLimits
from aurora.services.container_control import stop_project_containers
from aurora.services.project_rethink import rethink_project


@dataclass
class ProjectRethinkState:
    project_id: str
    status: str = "stopping"
    started_at: datetime = field(default_factory=now_utc)
    finished_at: datetime | None = None
    bootstrap_intent_id: str | None = None
    containers: dict[str, Any] | None = None
    error: str | None = None


class ProjectRethinkRegistry:
    """Run stop/reset/restart outside the request that initiated a rethink."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, ProjectRethinkState] = {}

    def start(self, *, project_id: str) -> ProjectRethinkState:
        with self._lock:
            existing = self._tasks.get(project_id)
            if existing and existing.status in {"stopping", "resetting"}:
                return existing
            state = ProjectRethinkState(project_id=project_id)
            self._tasks[project_id] = state

        with Session(engine) as session:
            project = session.get(Project, project_id)
            if project is None:
                raise ValueError("project not found")
            project.status = "WORKING"
            project.updated_at = now_utc()
            session.add(project)
            session.add(WorkerEvent(project_id=project_id, event_type="project.rethink_requested", payload_json={"phase": "stopping"}))
            session.commit()

        thread = threading.Thread(target=self._run, args=(project_id, state), daemon=True, name=f"rethink-{project_id}")
        thread.start()
        return state

    def status(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._tasks.get(project_id)
            return asdict(state) if state else None

    def _run(self, project_id: str, state: ProjectRethinkState) -> None:
        try:
            self._stop_active_autorun(project_id, state)

            with Session(engine) as session:
                self._update(state, status="resetting")
                bootstrap = rethink_project(session, project_id=project_id)

            autorun_registry.start(project_id=project_id, limits=AutoRunLimits())
            self._update(state, status="completed", bootstrap_intent_id=bootstrap.id, finished_at=now_utc())
        except Exception as exc:
            self._update(state, status="failed", error=str(exc), finished_at=now_utc())
            with Session(engine) as session:
                session.add(
                    WorkerEvent(
                        project_id=project_id,
                        event_type="project.rethink_failed",
                        payload_json={"error": str(exc)},
                    )
                )
                session.commit()

    def _stop_active_autorun(self, project_id: str, state: ProjectRethinkState) -> dict[str, Any]:
        autorun_registry.stop(project_id)
        containers: dict[str, Any] = {"stopped": [], "errors": []}
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            stopped = stop_project_containers(project_id)
            containers["stopped"] = list(dict.fromkeys([*containers["stopped"], *stopped.get("stopped", [])]))
            containers["errors"].extend(stopped.get("errors", []))
            self._update(state, containers=containers)
            autorun = autorun_registry.status(project_id)
            if autorun is None or autorun.get("status") not in {"running", "stopping"}:
                return containers
            time.sleep(0.25)
        raise RuntimeError("active autorun did not stop within 45 seconds")

    def _update(self, state: ProjectRethinkState, **values: Any) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(state, key, value)


project_rethink_registry = ProjectRethinkRegistry()
