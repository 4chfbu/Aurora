from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from aurora.config import Settings, get_settings
from aurora.services.network_proxy import network_proxy_registry


AGENT_API_PREFIX = "/slab-match/api/v1/agent"
_runtime_lock = threading.RLock()


def _url_origin(value: Any) -> tuple[str, str | None, int | None]:
    parsed = value if hasattr(value, "scheme") else urlparse(str(value))
    return parsed.scheme.lower(), (parsed.hostname or "").lower().rstrip("."), parsed.port or ({"http": 80, "https": 443}.get(parsed.scheme.lower()))


def _validate_public_attachment_url(url: str) -> None:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Slab Match attachment URL must be an absolute HTTP(S) URL")
    hostname = parsed.hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if (
        hostname in {"localhost", "metadata.google.internal"}
        or hostname.endswith(".localhost")
        or (
            address is not None
            and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved)
        )
    ):
        raise ValueError("Slab Match attachment redirects to a local or metadata endpoint")


class _SlabMatchRedirectHandler(HTTPRedirectHandler):
    """Keep control-plane credentials on-origin and revalidate downloads."""

    def __init__(self, credential_origin: str, *, allow_cross_origin: bool, public_targets_only: bool = False) -> None:
        super().__init__()
        self.credential_origin = _url_origin(credential_origin)
        self.allow_cross_origin = allow_cross_origin
        self.public_targets_only = public_targets_only

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request | None:
        target_origin = _url_origin(newurl)
        if target_origin != self.credential_origin and not self.allow_cross_origin:
            raise SlabMatchError("Slab Match API refused a cross-origin redirect")
        if self.public_targets_only:
            _validate_public_attachment_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and target_origin != self.credential_origin:
            redirected.remove_header("X-Agent-AccessKey")
        return redirected


class SlabMatchError(RuntimeError):
    """A diagnostic error returned by the Slab Match control plane."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None, auth_required: bool = False, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.auth_required = auth_required
        self.detail = detail


class SlabMatchNeedsSession(SlabMatchError):
    def __init__(self, message: str = "Slab Match requires an X-Agent-AccessKey") -> None:
        super().__init__(message, status=401, code="unauthorized", auth_required=True)


@dataclass(frozen=True)
class SlabMatchChallenge:
    exercise_id: int
    title: str
    description: str
    challenge_type: str
    difficulty: str | None
    points: Any
    attachments: list[dict[str, str]]
    endpoints: list[dict[str, Any]]
    has_solved: bool
    is_need_init: bool
    is_need_check: bool
    raw: dict[str, Any]
    match_info: dict[str, str] | None = None


def normalize_slab_match_base_url(base_url: str) -> str:
    parsed = urlparse(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Slab Match Base URL must be an absolute HTTP(S) URL without credentials or fragments")
    path = parsed.path.rstrip("/")
    prefix_at = path.find(AGENT_API_PREFIX)
    if prefix_at >= 0:
        path = path[: prefix_at + len(AGENT_API_PREFIX)]
    else:
        path = f"{path}{AGENT_API_PREFIX}" if path else AGENT_API_PREFIX
    return f"{parsed.scheme}://{parsed.netloc}{path}"


# Compatibility for callers that used the initial private helper.
_normalize_base_url = normalize_slab_match_base_url


def public_slab_match_config(settings: Settings | None = None) -> dict[str, Any]:
    current = settings or get_settings()
    with _runtime_lock:
        environment_keys = {value for key in ("AURORA_SLAB_MATCH_ACCESS_KEY", "SLAB_MATCH_ACCESS_KEY") if (value := os.getenv(key))}
        return {
            "base_url": current.slab_match_base_url,
            "access_key_configured": bool(current.slab_match_access_key),
            "access_key_source": "environment" if current.slab_match_access_key in environment_keys else ("runtime" if current.slab_match_access_key else "none"),
            "timeout_seconds": current.slab_match_timeout_seconds,
            "max_concurrent": current.slab_match_max_concurrent,
            "notice_poll_seconds": current.slab_match_notice_poll_seconds,
            "vpn_required": False,
        }


def configure_slab_match(*, base_url: str, access_key: str | None, clear_access_key: bool, timeout_seconds: int, max_concurrent: int) -> dict[str, Any]:
    if not 1 <= timeout_seconds <= 300:
        raise ValueError("Slab Match timeout must be between 1 and 300 seconds")
    if not 1 <= max_concurrent <= 10:
        raise ValueError("Slab Match dynamic environment limit must be between 1 and 10")
    if access_key is not None and len(access_key.strip()) > 4096:
        raise ValueError("Slab Match access key is too long")
    settings = get_settings()
    with _runtime_lock:
        settings.slab_match_base_url = normalize_slab_match_base_url(base_url)
        settings.slab_match_timeout_seconds = timeout_seconds
        settings.slab_match_max_concurrent = max_concurrent
        if clear_access_key:
            settings.slab_match_access_key = None
        elif access_key is not None and access_key.strip():
            settings.slab_match_access_key = access_key.strip()
    return public_slab_match_config(settings)


class SlabMatchClient:
    """Small, dependency-free client for the Agent API described in api_doc.md."""

    _request_lock = threading.Lock()
    _next_request_at = 0.0

    def __init__(
        self,
        settings: Settings | None = None,
        request_json: Callable[..., Any] | None = None,
        *,
        base_url: str | None = None,
        request_interval_seconds: float | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.base_url = normalize_slab_match_base_url(base_url or self.settings.slab_match_base_url)
        self._request_json = request_json or self._http_json
        self._request_interval_seconds = max(
            0.0,
            float(request_interval_seconds if request_interval_seconds is not None else (0.0 if request_json is not None else 1.0)),
        )

    def with_base_url(self, base_url: str) -> "SlabMatchClient":
        normalized = normalize_slab_match_base_url(base_url)
        if normalized == self.base_url:
            return self
        return SlabMatchClient(
            self.settings,
            self._request_json,
            base_url=normalized,
            request_interval_seconds=self._request_interval_seconds,
        )

    def _serialized_request(self, request: Callable[[], Any]) -> Any:
        # The Agent API is a shared control plane. Serialize across all client
        # instances so import, planner, and submission calls cannot burst it.
        with self._request_lock:
            delay = self._next_request_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            try:
                return request()
            finally:
                type(self)._next_request_at = time.monotonic() + self._request_interval_seconds

    def _url(self, path: str, query: dict[str, Any] | None = None) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            base = path
        else:
            base = urljoin(self.base_url.rstrip("/") + "/", path.lstrip("/"))
        if query:
            return f"{base}?{urlencode({key: value for key, value in query.items() if value is not None})}"
        return base

    @staticmethod
    def _exercise_id(value: int | str) -> int:
        try:
            exercise_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Slab Match exerciseId must be an integer") from exc
        if exercise_id <= 0:
            raise ValueError("Slab Match exerciseId must be positive")
        return exercise_id

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None, query: dict[str, Any] | None = None) -> Any:
        if not self.settings.slab_match_access_key:
            raise SlabMatchNeedsSession()
        url = self._url(path, query=query)

        def request() -> Any:
            try:
                return self._request_json(url, method=method, payload=payload, access_key=self.settings.slab_match_access_key)
            except TypeError:
                try:
                    return self._request_json(url, method, payload, self.settings.slab_match_access_key)
                except TypeError:
                    return self._request_json(url, payload or {})

        return self._serialized_request(request)

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        if isinstance(payload, dict):
            code = str(payload.get("code") or "")
            if code and code != "00000":
                message = str(payload.get("message") or code)
                if code.lower() in {"401", "403", "unauthorized", "forbidden"} or re.search(r"access\s*key|accesskey|未授权|无权限|鉴权|认证|访问密钥", message, re.IGNORECASE):
                    raise SlabMatchNeedsSession(message)
                raise SlabMatchError(message, code=code, detail=payload.get("data"))
            if "data" in payload:
                return payload.get("data")
        return payload

    @classmethod
    def _as_files(cls, value: Any) -> list[dict[str, str]]:
        if isinstance(value, str):
            candidate = value.strip()
            if not candidate:
                return []
            if candidate[:1] in {"{", "["}:
                try:
                    return cls._as_files(json.loads(candidate))
                except json.JSONDecodeError:
                    return []
            return [{"name": candidate.rsplit("/", 1)[-1], "url": candidate, "ext": ""}]
        if isinstance(value, list):
            files = [file for item in value for file in cls._as_files(item)]
            return list({file["url"]: file for file in files}.values())
        if not isinstance(value, dict):
            return []

        url = str(
            value.get("url")
            or value.get("downloadUrl")
            or value.get("download_url")
            or value.get("fileUrl")
            or value.get("file_url")
            or value.get("filePath")
            or value.get("file_path")
            or value.get("download")
            or value.get("href")
            or value.get("uri")
            or value.get("path")
            or value.get("ossUrl")
            or value.get("oss_url")
            or value.get("objectUrl")
            or value.get("object_url")
            or value.get("fileLink")
            or value.get("file_link")
            or value.get("downloadPath")
            or value.get("download_path")
            or ""
        ).strip()
        if url:
            name = str(value.get("name") or value.get("filename") or value.get("fileName") or url.rsplit("/", 1)[-1]).strip()
            return [{"name": name, "url": url, "ext": str(value.get("ext") or value.get("extension") or "").strip()}]

        # Attachment payloads have changed shape across Slab Match versions.
        # This method is only called on attachment/file fields, so recursively
        # inspect unknown wrappers instead of silently dropping a valid file
        # nested under a provider-specific key.
        nested = [
            file
            for key, child in value.items()
            if isinstance(child, (dict, list))
            or (key in {"files", "fileList", "file_list", "attachments", "attachment", "file", "list", "data"} and isinstance(child, str))
            for file in cls._as_files(child)
        ]
        return list({file["url"]: file for file in nested}.values())

    @classmethod
    def parse_challenge(cls, raw: dict[str, Any], *, category: str | None = None, match_info: dict[str, str] | None = None) -> SlabMatchChallenge:
        exercise_id = cls._exercise_id(raw.get("id"))
        title = str(raw.get("name") or raw.get("title") or f"exercise-{exercise_id}").strip()
        if not title:
            raise SlabMatchError("Slab Match exercise is missing title")
        description = str(raw.get("description") or "")
        challenge_type = str(category or raw.get("category") or raw.get("type") or "unknown").strip() or "unknown"
        attachment_sources = [
            raw.get("attachment"),
            raw.get("attachments"),
            raw.get("file"),
            raw.get("files"),
            raw.get("attachmentFiles"),
            raw.get("attachment_files"),
            raw.get("attachmentUrl"),
            raw.get("attachment_url"),
            raw.get("attachmentFile"),
            raw.get("attachment_file"),
            raw.get("fileList"),
            raw.get("file_list"),
        ]
        attachment_sources.extend(
            value
            for key, value in raw.items()
            if re.search(r"attach|file", str(key), re.IGNORECASE) and value is not None
        )
        attachments = cls._as_files(attachment_sources)
        endpoints = [item for item in (raw.get("endpoints") if isinstance(raw.get("endpoints"), list) else []) if isinstance(item, dict)]
        return SlabMatchChallenge(
            exercise_id=exercise_id,
            title=title,
            description=description,
            challenge_type=challenge_type,
            difficulty=str(raw.get("difficulty")).strip() if raw.get("difficulty") is not None else None,
            points=raw.get("score"),
            attachments=attachments,
            endpoints=endpoints,
            has_solved=bool(raw.get("hasSolved")),
            is_need_init=bool(raw.get("isNeedInit")),
            is_need_check=bool(raw.get("isNeedCheck")),
            raw=raw,
            match_info=match_info,
        )

    def match_info(self) -> dict[str, str]:
        data = self._unwrap(self._call("GET", "/match/notice/match-info"))
        if not isinstance(data, dict):
            return {}
        result: dict[str, str] = {}
        for key in ("note", "rule"):
            if data.get(key):
                result[key] = str(data[key]).strip()
        return result

    def overview(self) -> dict[str, Any]:
        data = self._unwrap(self._call("GET", "/answer-panel/overview"))
        return data if isinstance(data, dict) else {}

    def exercise_list(self) -> list[dict[str, Any]]:
        data = self._unwrap(self._call("GET", "/ctf/exercise-list"))
        if not isinstance(data, list):
            raise SlabMatchError("Slab Match exercise list response is not a JSON array")
        return [item for item in data if isinstance(item, dict)]

    def notice_list(self) -> list[dict[str, Any]]:
        data = self._unwrap(self._call("GET", "/match/notice/now-list"))
        if not isinstance(data, list):
            raise SlabMatchError("Slab Match notice list response is not a JSON array")
        return [item for item in data if isinstance(item, dict) and item.get("id") is not None]

    def notice_detail(self, notice_id: int | str) -> dict[str, Any]:
        identifier = self._exercise_id(notice_id)
        data = self._unwrap(self._call("GET", "/match/notice/detail", query={"id": identifier}))
        if not isinstance(data, dict):
            raise SlabMatchError("Slab Match notice detail response is not a JSON object")
        return data

    def get_exercise(self, exercise_id: int | str) -> SlabMatchChallenge:
        data = self._unwrap(self._call("GET", "/ctf/exercise", query={"exerciseId": self._exercise_id(exercise_id)}))
        if not isinstance(data, dict):
            raise SlabMatchError("Slab Match exercise detail response is not a JSON object")
        return self.parse_challenge(data)

    def list_challenges(self) -> list[SlabMatchChallenge]:
        match_info = self.match_info()
        challenges: list[SlabMatchChallenge] = []
        for category in self.exercise_list():
            category_name = str(category.get("name") or category.get("title") or "unknown").strip() or "unknown"
            for item in category.get("corpus", []) if isinstance(category.get("corpus"), list) else []:
                if not isinstance(item, dict) or item.get("id") is None or item.get("isOpen") is False:
                    continue
                detail = self.get_exercise(item["id"])
                raw = {**detail.raw, **{key: value for key, value in item.items() if key not in detail.raw}}
                challenges.append(self.parse_challenge(raw, category=category_name, match_info=match_info))
        return challenges

    def build_environment(self, exercise_id: int | str) -> dict[str, Any]:
        value = self._unwrap(self._call("POST", "/ctf/build-exercise-env", {"exerciseId": self._exercise_id(exercise_id)}))
        return value if isinstance(value, dict) else {"data": value}

    def recover_environment(self, exercise_id: int | str) -> dict[str, Any]:
        value = self._unwrap(self._call("POST", "/ctf/recover-exercise-env", {"exerciseId": self._exercise_id(exercise_id)}))
        return value if isinstance(value, dict) else {"data": value}

    def wait_until_ready(self, exercise_id: int | str) -> SlabMatchChallenge:
        identifier = self._exercise_id(exercise_id)
        deadline = time.monotonic() + max(1, int(self.settings.slab_match_timeout_seconds))
        last = self.get_exercise(identifier)
        while time.monotonic() < deadline:
            if not last.is_need_check and last.endpoints:
                return last
            time.sleep(1)
            last = self.get_exercise(identifier)
        return last

    def submit_result(self, exercise_id: int | str, flag: str) -> bool | None:
        if not 1 <= len(flag) <= 256:
            raise ValueError("Slab Match flag length must be between 1 and 256 characters")
        try:
            value = self._unwrap(self._call("POST", "/answer-panel/answer", {"exerciseId": self._exercise_id(exercise_id), "flag": flag}))
        except SlabMatchError as exc:
            # The API documents incorrect answers as a non-success code rather
            # than data.isCorrect=false. Keep transport/control-plane failures
            # distinct so the runner only rejects an explicitly wrong flag.
            diagnostic = f"{exc.code or ''} {exc}".lower()
            if re.search(r"incorrect|wrong|invalid[ _-]*(?:flag|answer)|答案.{0,8}(?:错误|不正确)|flag.{0,8}(?:错误|不正确)", diagnostic, re.IGNORECASE):
                return False
            raise
        if isinstance(value, dict) and value.get("isCorrect") is not None:
            return bool(value.get("isCorrect"))
        if isinstance(value, bool):
            return value
        return None

    def submit(self, exercise_id: int | str, flag: str) -> bool | None:
        return self.submit_result(exercise_id, flag)

    def download_attachment(self, url: str, max_bytes: int) -> tuple[bytes, str | None]:
        _validate_public_attachment_url(url)
        parsed = urlparse(url.strip())
        base = urlparse(self.base_url)

        authenticated_origin = _url_origin(parsed) == _url_origin(base)
        access_key = self.settings.slab_match_access_key if authenticated_origin else None

        headers = {"Accept": "application/octet-stream,*/*", "User-Agent": "Aurora-SlabMatch/1.0"}
        if access_key:
            headers["X-Agent-AccessKey"] = access_key
        request = Request(url, headers=headers)
        try:
            def fetch() -> tuple[bytes, str | None]:
                with build_opener(
                    network_proxy_registry.get().urllib_proxy_handler(),
                    _SlabMatchRedirectHandler(
                        self.base_url,
                        allow_cross_origin=True,
                        public_targets_only=True,
                    ),
                ).open(
                    request,
                    timeout=max(1, self.settings.slab_match_timeout_seconds),
                ) as response:
                    content_length = response.headers.get("Content-Length")
                    if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                        raise ValueError("attachment exceeds size limit")
                    data = response.read(max_bytes + 1)
                    if len(data) > max_bytes:
                        raise ValueError("attachment exceeds size limit")
                    return data, response.headers.get_content_type()

            return self._serialized_request(fetch) if authenticated_origin else fetch()
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise SlabMatchNeedsSession("Slab Match attachment authentication failed") from exc
            raise SlabMatchError(f"Slab Match attachment request failed with HTTP {exc.code}", status=exc.code) from exc
        except (URLError, TimeoutError) as exc:
            raise SlabMatchError(f"Slab Match attachment request failed: {exc}") from exc

    def _http_json(self, url: str, *, method: str = "GET", payload: dict[str, Any] | None = None, access_key: str | None = None) -> Any:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Aurora-SlabMatch/1.0",
            "X-Agent-AccessKey": access_key or "",
        }
        request = Request(url, headers=headers, method=method, data=json.dumps(payload).encode("utf-8") if payload is not None else None)
        try:
            with build_opener(
                network_proxy_registry.get().urllib_proxy_handler(),
                _SlabMatchRedirectHandler(url, allow_cross_origin=False),
            ).open(request, timeout=max(1, self.settings.slab_match_timeout_seconds)) as response:
                raw = response.read(2 * 1024 * 1024)
                try:
                    return json.loads(raw.decode(response.headers.get_content_charset() or "utf-8"))
                except json.JSONDecodeError as exc:
                    raise SlabMatchError("Slab Match API returned a non-JSON response") from exc
        except HTTPError as exc:
            raw = exc.read(2 * 1024 * 1024)
            try:
                error_payload = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                error_payload = {}
            message = str(error_payload.get("message") or f"Slab Match API request failed with HTTP {exc.code}")
            if exc.code in {401, 403}:
                raise SlabMatchNeedsSession(message) from exc
            raise SlabMatchError(message, status=exc.code, code=str(error_payload.get("code") or "") or None, detail=error_payload.get("data")) from exc
        except (URLError, TimeoutError) as exc:
            raise SlabMatchError(f"Slab Match API request failed: {exc}") from exc


def test_slab_match_connection(settings: Settings | None = None) -> dict[str, Any]:
    current = settings or get_settings()
    client = SlabMatchClient(current)
    categories = client.exercise_list()
    challenge_count = sum(
        1
        for category in categories
        for item in (category.get("corpus", []) if isinstance(category.get("corpus"), list) else [])
        if isinstance(item, dict) and item.get("id") is not None and item.get("isOpen") is not False
    )
    return {
        "api": {"status": "reachable", "challenge_count": challenge_count},
        "endpoint": {"status": "unverified", "address": None, "message": "题目详情和靶机状态仅在 planner 需要时读取。"},
        "progress": {},
    }
