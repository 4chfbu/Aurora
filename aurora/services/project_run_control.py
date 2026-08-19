from __future__ import annotations

import threading
from dataclasses import dataclass

from aurora.models import new_id


@dataclass
class ProjectRunClaim:
    project_id: str
    run_id: str
    owner: str
    stop_requested: bool = False


class ProjectRunControl:
    """Provide one in-process execution owner per project."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claims: dict[str, ProjectRunClaim] = {}

    def acquire(self, *, project_id: str, owner: str) -> ProjectRunClaim | None:
        with self._lock:
            if project_id in self._claims:
                return None
            claim = ProjectRunClaim(project_id=project_id, run_id=new_id("run"), owner=owner)
            self._claims[project_id] = claim
            return claim

    def owns(self, *, project_id: str, run_id: str) -> bool:
        with self._lock:
            claim = self._claims.get(project_id)
            return bool(claim and claim.run_id == run_id)

    def release(self, *, project_id: str, run_id: str) -> None:
        with self._lock:
            claim = self._claims.get(project_id)
            if claim and claim.run_id == run_id:
                self._claims.pop(project_id, None)

    def request_stop(self, project_id: str) -> ProjectRunClaim | None:
        with self._lock:
            claim = self._claims.get(project_id)
            if claim is not None:
                claim.stop_requested = True
            return claim

    def should_stop(self, *, project_id: str, run_id: str) -> bool:
        with self._lock:
            claim = self._claims.get(project_id)
            return claim is None or claim.run_id != run_id or claim.stop_requested

    def status(self, project_id: str) -> ProjectRunClaim | None:
        with self._lock:
            return self._claims.get(project_id)


project_run_control = ProjectRunControl()
