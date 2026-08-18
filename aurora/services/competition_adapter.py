from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from sqlmodel import Session, select

from aurora.config import Settings, get_settings
from aurora.models import AuthorizationScope, ChallengeGroupItem, Project, WorkerEvent, now_utc
from aurora.services.tsecbench import TSecBenchClient, TSecBenchError, TSecBenchNeedsSession, TSecBenchSubmission


@dataclass(frozen=True)
class EnvironmentHealth:
    available: bool
    reason: str | None = None


@dataclass(frozen=True)
class CompetitionSubmissionResult:
    correct: bool
    completed: bool
    detail: dict[str, object]


class CompetitionAdapter(Protocol):
    """Control-plane boundary for a concrete CTF platform integration."""

    def ensure_environment(self, session: Session, *, project_id: str) -> EnvironmentHealth: ...
    def close_environment(self, *, project_id: str, session: Session | None = None) -> None: ...
    def fetch_hint(self, session: Session, *, project_id: str) -> str | None: ...
    def submit_flag(self, session: Session, *, project_id: str, value: str) -> bool | None | CompetitionSubmissionResult: ...


class LocalCompetitionAdapter:
    """Safe default until a platform-specific adapter is configured.

    It intentionally performs no network requests and never fabricates hints
    or submissions.  Real competition API implementations plug in here.
    """

    def ensure_environment(self, session: Session, *, project_id: str) -> EnvironmentHealth:
        return EnvironmentHealth(True)

    def close_environment(self, *, project_id: str, session: Session | None = None) -> None:
        from aurora.services.container_control import stop_project_containers

        stop_project_containers(project_id)

    def fetch_hint(self, session: Session, *, project_id: str) -> str | None:
        return None

    def submit_flag(self, session: Session, *, project_id: str, value: str) -> bool | None:
        return None


class TSecBenchCompetitionAdapter:
    """Competition control-plane adapter for an imported TSecBench item."""

    _allocation_lock = threading.Lock()

    def __init__(self, settings: Settings | None = None, client: TSecBenchClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = client or TSecBenchClient(self.settings)
        self._codes: dict[str, str] = {}

    @staticmethod
    def _item(session: Session, project_id: str) -> ChallengeGroupItem:
        item = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.project_id == project_id).order_by(ChallengeGroupItem.created_at.desc())).first()
        if item is None:
            raise TSecBenchError("TSecBench group item not found")
        return item

    @staticmethod
    def _code(item: ChallengeGroupItem) -> str:
        code = str((item.competition_meta or {}).get("unique_code") or "").strip()
        if not code:
            raise TSecBenchError("TSecBench item is missing unique_code")
        return code

    @staticmethod
    def _addresses(value: object, *, default_scheme: str = "http") -> list[str]:
        results: list[str] = []
        if isinstance(value, dict):
            for key in ("container_addr", "containerAddr", "target_url", "address", "addr", "url"):
                results.extend(TSecBenchCompetitionAdapter._addresses(value.get(key), default_scheme=default_scheme))
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    results.extend(TSecBenchCompetitionAdapter._addresses(nested, default_scheme=default_scheme))
        elif isinstance(value, list):
            for nested in value:
                results.extend(TSecBenchCompetitionAdapter._addresses(nested, default_scheme=default_scheme))
        elif isinstance(value, str) and value.strip():
            address = value.strip()
            if "://" not in address:
                address = f"{default_scheme}://{address}"
            parsed = urlparse(address)
            if parsed.hostname:
                results.append(address)
        return list(dict.fromkeys(results))

    @staticmethod
    def _address(value: object, *, default_scheme: str = "http") -> str | None:
        addresses = TSecBenchCompetitionAdapter._addresses(value, default_scheme=default_scheme)
        return addresses[0] if addresses else None

    @staticmethod
    def _running(status: object) -> bool:
        return str(status or "").strip().lower() in {"available", "running", "started", "starting", "active", "up", "online"}

    def ensure_environment(self, session: Session, *, project_id: str) -> EnvironmentHealth:
        if not self.settings.tsecbench_configured:
            return EnvironmentHealth(False, "tsecbench is not configured; set AURORA_TSECBENCH_TOKEN")
        item = self._item(session, project_id)
        code = self._code(item)
        self._codes[project_id] = code
        meta = dict(item.competition_meta or {})
        project = session.get(Project, project_id)
        # TSecBench often omits the challenge category and returns a bare
        # host:port.  Treat well-known web ports as HTTP targets; otherwise an
        # unknown web challenge becomes tcp://host:80 and later http.request
        # rejects it before making any request.
        raw_addresses = meta.get("container_addr")
        default_scheme = self._default_scheme(project, raw_addresses)
        addresses = self._addresses(raw_addresses, default_scheme=default_scheme)
        # Repair addresses persisted by older Aurora versions, which assigned
        # tcp:// solely because TSecBench reported challenge_type=unknown.
        addresses = [self._repair_legacy_web_scheme(value, project) for value in addresses]
        address = addresses[0] if addresses else None
        if not address or not self._running(meta.get("container_status")):
            # Serialize the capacity check and allocation so concurrent groups
            # cannot both observe the last free slot. The platform remains the
            # final authority, but avoiding the fourth request locally makes
            # quota pressure a normal waiting state instead of an API error.
            with self._allocation_lock:
                active_projects = {
                    current.project_id
                    for current in session.exec(select(ChallengeGroupItem)).all()
                    if self._running((current.competition_meta or {}).get("container_status"))
                    and bool(self._addresses((current.competition_meta or {}).get("container_addr")))
                    and str((current.competition_meta or {}).get("platform") or "").lower() == "tsecbench"
                }
                if project_id not in active_projects and len(active_projects) >= max(1, int(self.settings.tsecbench_max_concurrent or 1)):
                    return EnvironmentHealth(False, "tsecbench_capacity_exhausted")
                try:
                    started = self.client.start(code)
                except TSecBenchNeedsSession as exc:
                    return EnvironmentHealth(False, str(exc))
                except TSecBenchError as exc:
                    return EnvironmentHealth(False, str(exc))
            started_scheme = self._default_scheme(project, started)
            started_addresses = self._addresses(started, default_scheme=started_scheme)
            addresses = started_addresses or addresses
            address = addresses[0] if addresses else None
            meta["container_status"] = "available"
        if address:
            meta["container_addr"] = addresses
            item.competition_meta = meta
            session.add(item)
            if project is not None:
                project.target_url = address
                project.target_verification_status = "VERIFIED"
                project.target_verification_reason = "TSecBench container address supplied by the authorized platform"
                project.target_verified_at = now_utc()
                project.updated_at = now_utc()
                session.add(project)
                scope = session.exec(select(AuthorizationScope).where(AuthorizationScope.project_id == project_id)).first()
                if scope is None:
                    scope = AuthorizationScope(project_id=project_id)
                hosts = list(dict.fromkeys((urlparse(value).hostname or "").lower().rstrip(".") for value in addresses))
                hosts = [host for host in hosts if host]
                scope.allowed_hosts = list(dict.fromkeys([*scope.allowed_hosts, *hosts]))
                session.add(scope)
                session.add(WorkerEvent(project_id=project_id, event_type="tsecbench.environment_ready", payload_json={"unique_code": code, "container_addr": addresses, "authorized_hosts": hosts}))
            session.commit()
            return EnvironmentHealth(True)
        return EnvironmentHealth(False, "TSecBench did not return a container address")

    @staticmethod
    def _default_scheme(project: Project | None, value: object) -> str:
        if project is not None and (project.challenge_type or "").strip().lower() in {"web", "webapp", "web_app"}:
            return "http"

        def bare_ports(current: object) -> list[int]:
            if isinstance(current, dict):
                return [port for nested in current.values() for port in bare_ports(nested)]
            if isinstance(current, list):
                return [port for nested in current for port in bare_ports(nested)]
            if not isinstance(current, str) or "://" in current:
                return []
            try:
                parsed = urlparse(f"tcp://{current.strip()}")
                return [parsed.port] if parsed.port is not None else []
            except ValueError:
                return []

        return "http" if any(port in {80, 443, 8000, 8080, 8081, 8443, 8888} for port in bare_ports(value)) else "tcp"

    @staticmethod
    def _repair_legacy_web_scheme(value: str, project: Project | None) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme == "tcp"
            and parsed.port in {80, 443, 8000, 8080, 8081, 8443, 8888}
            and (project is None or (project.challenge_type or "").strip().lower() in {"", "unknown", "web", "webapp", "web_app"})
        ):
            scheme = "https" if parsed.port in {443, 8443} else "http"
            return parsed._replace(scheme=scheme).geturl()
        return value

    def close_environment(self, *, project_id: str, session: Session | None = None) -> None:
        # The runner has no Session in this protocol method. It resolves the
        # unique code from the project metadata through a short-lived session.
        code = self._codes.get(project_id)
        if not code:
            from aurora.db import engine
            with Session(engine) as session:
                item = self._item(session, project_id)
                code = self._code(item)
        self.client.close(code)
        self._codes.pop(project_id, None)
        if session is not None:
            item = self._item(session, project_id)
            meta = dict(item.competition_meta or {})
            meta["container_status"] = "stopped"
            meta["container_addr"] = []
            item.competition_meta = meta
            session.add(item)
            project = session.get(Project, project_id)
            if project is not None:
                old_hosts = {
                    (urlparse(value).hostname or "").lower().rstrip(".")
                    for value in self._addresses(project.target_url)
                }
                project.target_url = None
                project.target_verification_status = "UNVERIFIED"
                project.target_verification_reason = "TSecBench target was released"
                project.target_verified_at = None
                project.updated_at = now_utc()
                session.add(project)
                scope = session.exec(select(AuthorizationScope).where(AuthorizationScope.project_id == project_id)).first()
                if scope is not None and old_hosts:
                    scope.allowed_hosts = [host for host in scope.allowed_hosts if host not in old_hosts]
                    session.add(scope)

    def fetch_hint(self, session: Session, *, project_id: str) -> str | None:
        item = self._item(session, project_id)
        self._codes[project_id] = self._code(item)
        return self.client.hint(self._code(item))

    def submit_flag(self, session: Session, *, project_id: str, value: str) -> bool | None | CompetitionSubmissionResult:
        item = self._item(session, project_id)
        self._codes[project_id] = self._code(item)
        submit_result = getattr(self.client, "submit_result", None)
        if not callable(submit_result):
            return self.client.submit(self._code(item), value)
        try:
            result: TSecBenchSubmission | None = submit_result(self._code(item), value)
        except TSecBenchError as exc:
            if exc.code != "duplicate":
                raise
            challenge = next((entry for entry in self.client.list_challenges() if entry.unique_code == self._code(item)), None)
            if challenge is None:
                raise
            duplicate_meta = dict(item.competition_meta or {})
            duplicate_meta["correct_flag_count"] = challenge.correct_flag_count
            duplicate_meta["flag_count"] = challenge.flag_count
            duplicate_meta["is_completed"] = challenge.is_completed
            item.competition_meta = duplicate_meta
            session.add(item)
            return CompetitionSubmissionResult(
                correct=True,
                completed=challenge.is_completed,
                detail={
                    "duplicate": True,
                    "correct_flag_count": challenge.correct_flag_count,
                    "total_flag_count": int(challenge.flag_count or 0),
                },
            )
        if result is None:
            return None
        meta = dict(item.competition_meta or {})
        meta["correct_flag_count"] = result.correct_flag_count
        meta["flag_count"] = result.total_flag_count or meta.get("flag_count")
        meta["is_completed"] = result.completed
        meta["cumulative_score"] = result.cumulative_score
        item.competition_meta = meta
        session.add(item)
        return CompetitionSubmissionResult(
            correct=result.correct,
            completed=result.completed,
            detail={
                "awarded": result.awarded,
                "cumulative_score": result.cumulative_score,
                "correct_flag_count": result.correct_flag_count,
                "total_flag_count": result.total_flag_count,
                "matched_flag_index": result.matched_flag_index,
            },
        )


# Short alias retained for integrations that refer to platform adapters by
# their provider name rather than the CompetitionAdapter protocol name.
TSecBenchAdapter = TSecBenchCompetitionAdapter
