from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlmodel import Session


@dataclass(frozen=True)
class EnvironmentHealth:
    available: bool
    reason: str | None = None


class CompetitionAdapter(Protocol):
    """Control-plane boundary for a concrete CTF platform integration."""

    def ensure_environment(self, session: Session, *, project_id: str) -> EnvironmentHealth: ...
    def close_environment(self, *, project_id: str) -> None: ...
    def fetch_hint(self, session: Session, *, project_id: str) -> str | None: ...
    def submit_flag(self, session: Session, *, project_id: str, value: str) -> bool | None: ...


class LocalCompetitionAdapter:
    """Safe default until a platform-specific adapter is configured.

    It intentionally performs no network requests and never fabricates hints
    or submissions.  Real competition API implementations plug in here.
    """

    def ensure_environment(self, session: Session, *, project_id: str) -> EnvironmentHealth:
        return EnvironmentHealth(True)

    def close_environment(self, *, project_id: str) -> None:
        from aurora.services.container_control import stop_project_containers

        stop_project_containers(project_id)

    def fetch_hint(self, session: Session, *, project_id: str) -> str | None:
        return None

    def submit_flag(self, session: Session, *, project_id: str, value: str) -> bool | None:
        return None
