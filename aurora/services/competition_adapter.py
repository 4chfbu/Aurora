from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from sqlmodel import Session, select

from aurora.config import Settings, get_settings
from aurora.models import AuthorizationScope, ChallengeGroup, ChallengeGroupItem, Project, WorkerEvent, new_id, now_utc
from aurora.services.slab_match import SlabMatchClient, SlabMatchError, SlabMatchNeedsSession
from aurora.services.tsecbench import TSecBenchClient, TSecBenchError, TSecBenchNeedsSession, TSecBenchSubmission


@dataclass(frozen=True)
class EnvironmentHealth:
    available: bool
    reason: str | None = None
    disposition: str | None = None
    detail: str | None = None


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


def competition_platform(item: ChallengeGroupItem | None) -> str:
    return str((item.competition_meta or {}).get("platform") or "").strip().lower() if item is not None else ""


def is_managed_competition_platform(platform: str | None) -> bool:
    return str(platform or "").strip().lower() in {"tsecbench", "slab_match"}


def _addresses(value: object, *, default_scheme: str = "http") -> list[str]:
    results: list[str] = []
    if isinstance(value, dict):
        for key in ("container_addr", "containerAddr", "target_url", "address", "addr", "url"):
            results.extend(_addresses(value.get(key), default_scheme=default_scheme))
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                results.extend(_addresses(nested, default_scheme=default_scheme))
    elif isinstance(value, list):
        for nested in value:
            results.extend(_addresses(nested, default_scheme=default_scheme))
    elif isinstance(value, str) and value.strip():
        address = value.strip()
        if "://" not in address:
            address = f"{default_scheme}://{address}"
        parsed = urlparse(address)
        if parsed.hostname:
            results.append(address)
    return list(dict.fromkeys(results))


def _running(status: object) -> bool:
    return str(status or "").strip().lower() in {"available", "running", "started", "starting", "active", "up", "online", "ready"}


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


def _authorized_hosts(values: list[str]) -> list[str]:
    return [host for host in dict.fromkeys((urlparse(value).hostname or "").lower().rstrip(".") for value in values) if host]


def _merge_project_authorization(session: Session, *, project_id: str, addresses: list[str]) -> list[str]:
    scope = session.exec(select(AuthorizationScope).where(AuthorizationScope.project_id == project_id)).first()
    if scope is None:
        scope = AuthorizationScope(project_id=project_id)
    hosts = _authorized_hosts(addresses)
    scope.allowed_hosts = list(dict.fromkeys([*scope.allowed_hosts, *hosts]))
    session.add(scope)
    return hosts


def _clear_project_authorization(session: Session, *, project_id: str, addresses: list[str]) -> None:
    scope = session.exec(select(AuthorizationScope).where(AuthorizationScope.project_id == project_id)).first()
    if scope is None:
        return
    old_hosts = set(_authorized_hosts(addresses))
    if old_hosts:
        scope.allowed_hosts = [host for host in scope.allowed_hosts if host not in old_hosts]
        session.add(scope)


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

    @classmethod
    def _occupies_capacity(cls, meta: dict[str, object]) -> bool:
        status = str(meta.get("container_status") or "").strip().lower()
        return bool(cls._addresses(meta.get("container_addr"))) and (
            cls._running(status) or status in {"release_failed", "releasing", "unknown"}
        )

    @staticmethod
    def _capacity_exhausted(exc: TSecBenchError) -> bool:
        """Normalize platform quota responses into a scheduler-owned reason."""
        detail = " ".join(
            str(value or "")
            for value in (exc.code, exc.status, exc, exc.detail)
        ).strip().lower()
        return (
            exc.status == 429
            or str(exc.code or "").strip().lower() == "resource_unavailable"
            or any(
                marker in detail
                for marker in (
                    "capacity exhausted",
                    "capacity_exhausted",
                    "max active challenge instances reached",
                    "maximum active challenge instances",
                    "too many active challenge instances",
                    "no available challenge instance",
                )
            )
        )

    @staticmethod
    def _task_finished(exc: TSecBenchError) -> bool:
        detail = " ".join(
            str(value or "")
            for value in (exc.code, exc.status, exc, exc.detail)
        ).strip().lower()
        return any(
            marker in detail
            for marker in (
                "already finished",
                "task finished",
                "task has finished",
                "task ended",
                "task has ended",
                "task expired",
                "task has expired",
                "task manually stopped",
            )
        )

    def _classify_start_error(self, exc: TSecBenchError) -> EnvironmentHealth:
        detail = str(exc).strip() or str(exc.code or "TSecBench environment start failed")
        if self._task_finished(exc):
            return EnvironmentHealth(False, "tsecbench_task_finished", "task_terminal", detail)
        if exc.code == "challenge_not_found":
            return EnvironmentHealth(False, "tsecbench_challenge_not_found", "item_terminal", detail)
        if self._capacity_exhausted(exc):
            return EnvironmentHealth(False, "tsecbench_capacity_exhausted", "wait_resource", detail)
        if exc.code == "invalid_state":
            try:
                self.client.list_challenges()
            except TSecBenchNeedsSession as confirm_exc:
                return EnvironmentHealth(False, "tsecbench_session_required", "wait_input", str(confirm_exc))
            except TSecBenchError as confirm_exc:
                if confirm_exc.code == "invalid_state" or self._task_finished(confirm_exc):
                    return EnvironmentHealth(False, "tsecbench_task_finished", "task_terminal", str(confirm_exc))
                return EnvironmentHealth(False, "tsecbench_control_plane_unavailable", "wait_resource", str(confirm_exc))
            return EnvironmentHealth(False, "tsecbench_capacity_exhausted", "wait_resource", detail)
        return EnvironmentHealth(False, "tsecbench_control_plane_unavailable", "wait_resource", detail)

    @staticmethod
    def _already_released(exc: Exception) -> bool:
        """Return whether a close failure confirms that no live task remains."""
        code = str(getattr(exc, "code", "") or "").strip().lower()
        message = str(exc).strip().lower()
        return code in {"task_not_found", "challenge_not_found", "invalid_state"} or any(
            marker in message
            for marker in (
                "already finished",
                "already closed",
                "task does not exist",
                "challenge does not exist",
                "task not found",
                "challenge not found",
            )
        )

    def _reconcile_release_failures(self, session: Session, *, exclude_project_id: str) -> None:
        stale_items = session.exec(select(ChallengeGroupItem)).all()
        for stale in stale_items:
            meta = stale.competition_meta or {}
            if (
                stale.project_id == exclude_project_id
                or str(meta.get("platform") or "").strip().lower() != "tsecbench"
                or str(meta.get("container_status") or "").strip().lower() != "release_failed"
            ):
                continue
            try:
                self.close_environment(project_id=stale.project_id, session=session)
                session.commit()
            except Exception:
                # Ambiguous failures remain capacity-owning and can be retried by
                # a later admission attempt without advertising a false vacancy.
                session.commit()

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
                self._reconcile_release_failures(session, exclude_project_id=project_id)
                session.expire_all()
                active_projects = {
                    current.project_id
                    for current in session.exec(select(ChallengeGroupItem)).all()
                    if self._occupies_capacity(current.competition_meta or {})
                    and str((current.competition_meta or {}).get("platform") or "").lower() == "tsecbench"
                }
                if project_id not in active_projects and len(active_projects) >= max(1, int(self.settings.tsecbench_max_concurrent or 1)):
                    return EnvironmentHealth(False, "tsecbench_capacity_exhausted", "wait_resource")
                try:
                    started = self.client.start(code)
                except TSecBenchNeedsSession as exc:
                    return EnvironmentHealth(False, "tsecbench_session_required", "wait_input", str(exc))
                except TSecBenchError as exc:
                    return self._classify_start_error(exc)
            started_scheme = self._default_scheme(project, started)
            started_addresses = self._addresses(started, default_scheme=started_scheme)
            addresses = started_addresses or addresses
            address = addresses[0] if addresses else None
            meta["container_status"] = "available"
            meta["environment_id"] = new_id("environment")
        if address:
            meta.setdefault("environment_id", new_id("environment"))
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
        if session is None:
            from aurora.db import engine
            with Session(engine) as owned_session:
                try:
                    self.close_environment(project_id=project_id, session=owned_session)
                finally:
                    owned_session.commit()
            return

        item = self._item(session, project_id)
        meta = dict(item.competition_meta or {})
        code = self._codes.get(project_id) or self._code(item)
        # Cleanup may be requested both at the end of the Solver turn and
        # again when the phase becomes terminal. Keep it idempotent so a
        # successful release cannot turn into a spurious platform error.
        try:
            if self._occupies_capacity(meta) or bool(self._addresses(meta.get("container_addr"))):
                self.client.close(code)
        except Exception as exc:
            if self._already_released(exc):
                pass
            else:
            # A timeout is ambiguous: the platform may still own the scarce
            # slot. Retain the address and authorization until a retry confirms
            # release instead of advertising a false local vacancy.
                meta["container_status"] = "release_failed"
                item.competition_meta = meta
                session.add(item)
                project = session.get(Project, project_id)
                if project is not None:
                    project.target_verification_reason = "TSecBench target release is pending confirmation"
                    project.updated_at = now_utc()
                    session.add(project)
                raise

        self._codes.pop(project_id, None)
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


class SlabMatchCompetitionAdapter:
    """Competition control-plane adapter for an imported Slab Match item."""

    _allocation_lock = threading.Lock()

    def __init__(self, settings: Settings | None = None, client: SlabMatchClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = client or SlabMatchClient(self.settings)
        self._exercise_ids: dict[str, int] = {}

    def _client_for(self, item: ChallengeGroupItem):
        base_url = str((item.competition_meta or {}).get("base_url") or "").strip()
        with_base_url = getattr(self.client, "with_base_url", None)
        return with_base_url(base_url) if base_url and callable(with_base_url) else self.client

    def _environment_limit(self, session: Session, item: ChallengeGroupItem) -> int:
        group = session.get(ChallengeGroup, item.group_id)
        raw = (group.limits or {}).get("max_dynamic_environments") if group is not None else None
        try:
            return max(1, min(10, int(raw if raw is not None else self.settings.slab_match_max_concurrent)))
        except (TypeError, ValueError):
            return max(1, int(self.settings.slab_match_max_concurrent or 1))

    @staticmethod
    def _uses_environment(meta: dict[str, object]) -> bool:
        return bool(meta.get("requires_environment", True))

    def _active_environment_projects(self, session: Session) -> set[str]:
        projects: set[str] = set()
        for current in session.exec(select(ChallengeGroupItem)).all():
            meta = current.competition_meta or {}
            if competition_platform(current) != "slab_match" or not self._uses_environment(meta):
                continue
            if str(meta.get("container_status") or "").lower() in {"building", "available"}:
                projects.add(current.project_id)
        return projects

    @staticmethod
    def _item(session: Session, project_id: str) -> ChallengeGroupItem:
        item = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.project_id == project_id).order_by(ChallengeGroupItem.created_at.desc())).first()
        if item is None:
            raise SlabMatchError("Slab Match group item not found")
        return item

    @staticmethod
    def _exercise_id(item: ChallengeGroupItem) -> int:
        try:
            value = int((item.competition_meta or {}).get("exercise_id"))
        except (TypeError, ValueError) as exc:
            raise SlabMatchError("Slab Match item is missing exercise_id") from exc
        if value <= 0:
            raise SlabMatchError("Slab Match item has an invalid exercise_id")
        return value

    @staticmethod
    def _endpoint_addresses(project: Project | None, endpoints: list[dict[str, object]]) -> list[str]:
        addresses: list[str] = []
        web_target = project is not None and (project.challenge_type or "").strip().lower() in {"web", "webapp", "web_app"}

        def host_and_port(value: str) -> tuple[str, str]:
            candidate = value.strip()
            parsed = urlparse(candidate if "://" in candidate else f"//{candidate}")
            try:
                parsed_port = str(parsed.port or "")
            except ValueError:
                parsed_port = ""
            return (parsed.hostname or candidate).strip("[]"), parsed_port

        def service(value: str) -> tuple[str, str]:
            candidate = value.strip()
            if "/" in candidate:
                service_name, port = candidate.rsplit("/", 1)
                return service_name.lower(), port
            return "", candidate

        def target(host_value: str, connect_value: str, service_value: str, mapping_type: str = "") -> str | None:
            host, embedded_port = host_and_port(host_value)
            service_name, service_port = service(service_value)
            _, connect_port = service(connect_value)
            connect_port = embedded_port or connect_port or service_port
            if not host or not connect_port.isdigit():
                return None
            declared_type = mapping_type.lower() or service_name
            if declared_type in {"http", "https"}:
                scheme = declared_type
            elif web_target:
                scheme = "https" if service_port in {"443", "8443"} else "http"
            else:
                scheme = "tcp"
            host_literal = f"[{host}]" if ":" in host else host
            return f"{scheme}://{host_literal}:{connect_port}"

        for endpoint in endpoints:
            expose_ips = [str(value).strip() for value in endpoint.get("exposeIps", []) if str(value).strip()]
            ports = [str(value).strip() for value in endpoint.get("ports", []) if str(value).strip()]
            proxy_ips = [str(value).strip() for value in endpoint.get("proxyIps", []) if str(value).strip()]
            mappings = [mapping for mapping in endpoint.get("portMappings", []) if isinstance(mapping, dict)]
            direct = [address for host in expose_ips for port in ports if (address := target(host, port, port))]
            proxy = []
            for host in proxy_ips:
                for mapping in mappings:
                    service_port = str(mapping.get("port") or "").strip()
                    connect_port = str(mapping.get("proxy") or "").strip()
                    if connect_port:
                        address = target(host, connect_port, service_port, str(mapping.get("type") or ""))
                        if address:
                            proxy.append(address)
            ordered = [*proxy, *direct] if endpoint.get("isProxy") else [*direct, *proxy]
            addresses.extend(ordered)
        return list(dict.fromkeys(addresses))

    @staticmethod
    def _environment_note(challenge_type: str, endpoints: list[dict[str, object]]) -> str | None:
        lines: list[str] = []
        users: list[str] = []
        for endpoint in endpoints:
            for user in endpoint.get("users", []) if isinstance(endpoint.get("users"), list) else []:
                if not isinstance(user, dict):
                    continue
                username = str(user.get("username") or "").strip()
                password = str(user.get("password") or "").strip()
                if username or password:
                    users.append(f"{username}:{password}" if password else username)
        if users:
            lines.append(f"Environment credentials: {', '.join(dict.fromkeys(users))}")
        if challenge_type.lower() not in {"web", "webapp", "web_app"}:
            exposed = []
            for endpoint in endpoints:
                for host in endpoint.get("exposeIps", []) if isinstance(endpoint.get("exposeIps"), list) else []:
                    for port in endpoint.get("ports", []) if isinstance(endpoint.get("ports"), list) else []:
                        host_text = str(host).strip()
                        port_text = str(port).strip().rsplit("/", 1)[-1]
                        parsed = urlparse(host_text if "://" in host_text else f"//{host_text}")
                        try:
                            embedded_port = parsed.port
                        except ValueError:
                            embedded_port = None
                        hostname = parsed.hostname or host_text
                        exposed.append(f"{hostname}:{embedded_port or port_text}")
            if exposed:
                lines.append(f"Reachable endpoints: {', '.join(dict.fromkeys(str(value) for value in exposed if str(value).strip()))}")
        return "\n".join(lines) if lines else None

    @staticmethod
    def _apply_project_note(project: Project, note: str | None) -> None:
        if not note:
            return
        marker = "Environment access:"
        if marker in (project.goal or ""):
            return
        project.goal = f"{project.goal.rstrip()}\n\n{marker}\n{note}".strip()

    def _release_environment_state(
        self,
        session: Session,
        *,
        item: ChallengeGroupItem,
        project: Project | None,
        client: SlabMatchClient,
        exercise_id: int,
        reason: str,
    ) -> str | None:
        meta = dict(item.competition_meta or {})
        old_addresses = list(meta.get("container_addr") or [])
        cleanup_error = None
        if self._uses_environment(meta) and str(meta.get("container_status") or "").lower() not in {"stopped", "not_required"}:
            try:
                client.recover_environment(exercise_id)
            except Exception as exc:
                cleanup_error = str(exc)[:500]
        meta["container_status"] = "stopped" if self._uses_environment(meta) else "not_required"
        meta["container_addr"] = []
        meta["endpoints"] = []
        meta["is_need_check"] = False
        item.competition_meta = meta
        session.add(item)
        if project is not None:
            project.target_url = None
            project.target_verification_status = "UNVERIFIED"
            project.target_verification_reason = f"Slab Match target was released: {reason}"
            project.target_verified_at = None
            project.updated_at = now_utc()
            session.add(project)
            _clear_project_authorization(session, project_id=project.id, addresses=old_addresses)
            session.add(WorkerEvent(
                project_id=project.id,
                event_type="slab_match.environment_released" if cleanup_error is None else "slab_match.environment_cleanup_failed",
                payload_json={"exercise_id": exercise_id, "reason": reason, "error": cleanup_error},
            ))
        session.commit()
        return cleanup_error

    def ensure_environment(self, session: Session, *, project_id: str) -> EnvironmentHealth:
        if not self.settings.slab_match_configured:
            return EnvironmentHealth(False, "slab_match is not configured; set AURORA_SLAB_MATCH_ACCESS_KEY")
        item = self._item(session, project_id)
        client = self._client_for(item)
        exercise_id = self._exercise_id(item)
        self._exercise_ids[project_id] = exercise_id
        meta = dict(item.competition_meta or {})
        project = session.get(Project, project_id)
        with self._allocation_lock:
            try:
                detail = client.get_exercise(exercise_id)
            except SlabMatchNeedsSession as exc:
                return EnvironmentHealth(False, str(exc))
            except SlabMatchError as exc:
                return EnvironmentHealth(False, str(exc))
            requires_environment = bool(detail.is_need_init or detail.is_need_check or detail.endpoints)
            meta["requires_environment"] = requires_environment
            meta["attachment_only"] = bool(meta.get("attachments")) and not requires_environment
            if not requires_environment:
                meta["is_need_init"] = False
                meta["is_need_check"] = False
                meta["container_status"] = "not_required"
                meta["container_addr"] = []
                item.competition_meta = meta
                session.add(item)
                session.commit()
                return EnvironmentHealth(True)
            if detail.is_need_init:
                active_projects = self._active_environment_projects(session)
                if project_id not in active_projects and len(active_projects) >= self._environment_limit(session, item):
                    return EnvironmentHealth(False, "slab_match_capacity_exhausted")
                meta["container_status"] = "building"
                item.competition_meta = meta
                session.add(item)
                session.commit()
                try:
                    client.build_environment(exercise_id)
                    meta["environment_id"] = new_id("environment")
                except SlabMatchNeedsSession as exc:
                    meta["container_status"] = "stopped"
                    item.competition_meta = meta
                    session.add(item)
                    session.commit()
                    return EnvironmentHealth(False, str(exc))
                except SlabMatchError as exc:
                    meta["container_status"] = "stopped"
                    item.competition_meta = meta
                    session.add(item)
                    session.commit()
                    return EnvironmentHealth(False, str(exc))
            if detail.is_need_init or detail.is_need_check or not detail.endpoints:
                try:
                    detail = client.wait_until_ready(exercise_id)
                except SlabMatchNeedsSession as exc:
                    cleanup = self._release_environment_state(session, item=item, project=project, client=client, exercise_id=exercise_id, reason="environment_wait_auth_failed")
                    return EnvironmentHealth(False, f"{exc}; cleanup failed: {cleanup}" if cleanup else str(exc))
                except SlabMatchError as exc:
                    cleanup = self._release_environment_state(session, item=item, project=project, client=client, exercise_id=exercise_id, reason="environment_wait_failed")
                    return EnvironmentHealth(False, f"{exc}; cleanup failed: {cleanup}" if cleanup else str(exc))
        endpoints = detail.endpoints
        addresses = self._endpoint_addresses(project, endpoints)
        meta["is_need_init"] = detail.is_need_init
        meta["is_need_check"] = detail.is_need_check
        meta["has_solved"] = detail.has_solved
        if not any(isinstance(value, dict) and value.get("artifact_id") for value in meta.get("attachments", []) if isinstance(value, dict)):
            meta["attachments"] = detail.attachments
        meta["endpoints"] = endpoints
        meta["container_status"] = "available" if addresses and not detail.is_need_check else ("building" if detail.is_need_check else "stopped")
        if detail.is_need_check:
            cleanup = self._release_environment_state(session, item=item, project=project, client=client, exercise_id=exercise_id, reason="environment_ready_timeout")
            reason = "Slab Match exercise environment is still preparing after the configured timeout"
            return EnvironmentHealth(False, f"{reason}; cleanup failed: {cleanup}" if cleanup else reason)
        if not addresses:
            cleanup = self._release_environment_state(session, item=item, project=project, client=client, exercise_id=exercise_id, reason="environment_missing_endpoint")
            reason = "Slab Match did not return a ready exercise endpoint"
            return EnvironmentHealth(False, f"{reason}; cleanup failed: {cleanup}" if cleanup else reason)
        meta["container_addr"] = addresses
        meta["container_status"] = "available"
        meta.setdefault("environment_id", new_id("environment"))
        meta["environment_notes"] = self._environment_note(project.challenge_type if project is not None else "", endpoints)
        item.competition_meta = meta
        session.add(item)
        if project is not None:
            project.target_url = addresses[0]
            project.target_verification_status = "VERIFIED"
            project.target_verification_reason = "Slab Match exercise endpoint supplied by the authorized platform"
            project.target_verified_at = now_utc()
            project.updated_at = now_utc()
            self._apply_project_note(project, meta.get("environment_notes"))
            session.add(project)
            hosts = _merge_project_authorization(session, project_id=project_id, addresses=addresses)
            session.add(WorkerEvent(project_id=project_id, event_type="slab_match.environment_ready", payload_json={"exercise_id": exercise_id, "container_addr": addresses, "authorized_hosts": hosts}))
        session.commit()
        return EnvironmentHealth(True)

    def close_environment(self, *, project_id: str, session: Session | None = None) -> None:
        if session is None:
            from aurora.db import engine
            with Session(engine) as owned_session:
                self.close_environment(project_id=project_id, session=owned_session)
            return
        item = self._item(session, project_id)
        exercise_id = self._exercise_ids.get(project_id) or self._exercise_id(item)
        client = self._client_for(item)
        project = session.get(Project, project_id)
        cleanup_error = self._release_environment_state(
            session,
            item=item,
            project=project,
            client=client,
            exercise_id=exercise_id,
            reason="solver_turn_finished",
        )
        self._exercise_ids.pop(project_id, None)
        if cleanup_error:
            raise SlabMatchError(f"Slab Match environment cleanup failed: {cleanup_error}")

    def fetch_hint(self, session: Session, *, project_id: str) -> str | None:
        item = self._item(session, project_id)
        meta = dict(item.competition_meta or {})
        match_info = meta.get("match_info") if isinstance(meta.get("match_info"), dict) else {}
        rule = str(match_info.get("rule") or "").strip()
        note = str(match_info.get("note") or "").strip()
        parts = [part for part in (note, rule) if part]
        return "\n\n".join(parts) if parts else None

    @staticmethod
    def _submission_value(item: ChallengeGroupItem, value: str) -> tuple[str, str]:
        meta = item.competition_meta or {}
        match_info = meta.get("match_info") if isinstance(meta.get("match_info"), dict) else {}
        rule = str(match_info.get("rule") or "")
        payload_only = bool(
            re.search(r"提交时.{0,16}提交\s*\{\}\s*(?:内|中)", rule, re.IGNORECASE)
            or re.search(r"submit.{0,16}(?:only|just).{0,16}(?:inside|within).{0,8}(?:braces|\{\})", rule, re.IGNORECASE)
        )
        candidate = value.strip()
        if payload_only and candidate.endswith("}") and "{" in candidate:
            prefix, payload = candidate.split("{", 1)
            payload = payload[:-1]
            if prefix and payload and "{" not in payload and "}" not in payload:
                return payload, "brace_payload"
        return candidate, "full_flag"

    def submit_flag(self, session: Session, *, project_id: str, value: str) -> bool | None | CompetitionSubmissionResult:
        item = self._item(session, project_id)
        client = self._client_for(item)
        exercise_id = self._exercise_id(item)
        self._exercise_ids[project_id] = exercise_id
        submission_value, submission_format = self._submission_value(item, value)
        result = client.submit_result(exercise_id, submission_value)
        if result is None:
            return None
        meta = dict(item.competition_meta or {})
        meta["has_solved"] = bool(result)
        item.competition_meta = meta
        session.add(item)
        return CompetitionSubmissionResult(
            correct=bool(result),
            completed=bool(result),
            detail={"exercise_id": exercise_id, "is_correct": bool(result), "submission_format": submission_format},
        )


# Short alias retained for integrations that refer to platform adapters by
# their provider name rather than the CompetitionAdapter protocol name.
TSecBenchAdapter = TSecBenchCompetitionAdapter
SlabMatchAdapter = SlabMatchCompetitionAdapter
