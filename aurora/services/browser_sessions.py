from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from urllib.parse import urlparse


@dataclass(frozen=True)
class BrowserSession:
    source_url: str
    cookie: str


class BrowserSessionRegistry:
    """In-memory browser session registry. Cookies never enter persistent storage."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._batch_sessions: dict[str, BrowserSession] = {}
        self._project_sessions: dict[str, BrowserSession] = {}

    def register_batch(self, *, batch_id: str, source_url: str, cookie: str | None) -> None:
        if not cookie or not cookie.strip():
            return
        with self._lock:
            self._batch_sessions[batch_id] = BrowserSession(source_url=source_url, cookie=cookie.strip())

    def bind_batch_projects(self, *, batch_id: str, project_ids: list[str]) -> None:
        with self._lock:
            session = self._batch_sessions.get(batch_id)
            if session is None:
                return
            for project_id in project_ids:
                self._project_sessions[project_id] = session

    def bind_batch_project_sources(self, *, batch_id: str, project_sources: dict[str, str]) -> None:
        """Reuse the in-memory Cookie while binding each project to its detail page."""
        with self._lock:
            session = self._batch_sessions.get(batch_id)
            if session is None:
                return
            for project_id, source_url in project_sources.items():
                if urlparse(source_url).hostname:
                    self._project_sessions[project_id] = BrowserSession(source_url=source_url, cookie=session.cookie)

    def get_batch_session(self, batch_id: str) -> BrowserSession | None:
        with self._lock:
            return self._batch_sessions.get(batch_id)

    def set_project_session(self, *, project_id: str, source_url: str, cookie: str) -> None:
        if not cookie.strip() or not urlparse(source_url).hostname:
            raise ValueError("a source URL and non-empty Cookie are required")
        with self._lock:
            self._project_sessions[project_id] = BrowserSession(source_url=source_url, cookie=cookie.strip())

    def get_project_session(self, project_id: str) -> BrowserSession | None:
        with self._lock:
            return self._project_sessions.get(project_id)

    def clear_project_session(self, project_id: str) -> None:
        with self._lock:
            self._project_sessions.pop(project_id, None)


browser_session_registry = BrowserSessionRegistry()
