from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlmodel import Session

from aurora.models import WorkerEvent
from aurora.services.autorunner import AutoRunLimits, AutoRunnerService


@dataclass(frozen=True)
class HarvesterResult:
    status: str
    reason: str


class HarvesterRunner(Protocol):
    """Execution boundary: schedulers dispatch work but never solve it."""

    def run(self, session: Session, *, project_id: str, task: dict[str, Any], limits: AutoRunLimits, should_stop: callable | None) -> HarvesterResult: ...


class AutoRunnerHarvester:
    """Compatibility harvester backed by the existing project runtime."""

    def run(self, session: Session, *, project_id: str, task: dict[str, Any], limits: AutoRunLimits, should_stop: callable | None) -> HarvesterResult:
        # This is the scheduler-to-harvester boundary.  ``task`` is raw
        # challenge context only; ContextBuilder exposes it to the solver.
        session.add(WorkerEvent(project_id=project_id, event_type="harvester.task_dispatched", payload_json=task))
        session.commit()
        result = AutoRunnerService().run_until_stop(session, project_id=project_id, limits=limits, should_stop=should_stop)
        return HarvesterResult(status=result.status.upper(), reason=result.stop_reason)
