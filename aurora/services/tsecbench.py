from __future__ import annotations

import json
import os
import socket
import threading
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, build_opener

from aurora.config import Settings, get_settings
from aurora.services.network_proxy import network_proxy_registry


class TSecBenchError(RuntimeError):
    """A diagnostic error returned by the TSecBench control plane."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None, auth_required: bool = False, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.auth_required = auth_required
        self.detail = detail


class TSecBenchNeedsSession(TSecBenchError):
    def __init__(self, message: str = "TSecBench requires a BENCHMARK_TOKEN") -> None:
        super().__init__(message, status=404, code="task_not_found", auth_required=True)


@dataclass(frozen=True)
class TSecBenchChallenge:
    unique_code: str
    title: str
    description: str
    challenge_type: str
    difficulty: Any
    level: Any
    points: Any
    flag_count: Any
    container_status: str | None
    container_addr: list[str]
    raw: dict[str, Any]
    correct_flag_count: int = 0
    is_completed: bool = False


@dataclass(frozen=True)
class TSecBenchSubmission:
    correct: bool
    awarded: int
    cumulative_score: int
    correct_flag_count: int
    total_flag_count: int
    matched_flag_index: int | None

    @property
    def completed(self) -> bool:
        return self.correct and self.total_flag_count > 0 and self.correct_flag_count >= self.total_flag_count


_runtime_lock = threading.RLock()


def public_tsecbench_config(settings: Settings | None = None) -> dict[str, Any]:
    current = settings or get_settings()
    with _runtime_lock:
        environment_tokens = {value for key in ("AURORA_TSECBENCH_TOKEN", "BENCHMARK_TOKEN") if (value := os.getenv(key))}
        return {
            "base_url": current.tsecbench_base_url,
            "token_configured": bool(current.tsecbench_token),
            "token_source": "environment" if current.tsecbench_token in environment_tokens else ("runtime" if current.tsecbench_token else "none"),
            "timeout_seconds": current.tsecbench_timeout_seconds,
            "max_concurrent": current.tsecbench_max_concurrent,
            "vpn_required": True,
        }


def configure_tsecbench(*, base_url: str, token: str | None, clear_token: bool, timeout_seconds: int, max_concurrent: int) -> dict[str, Any]:
    parsed = urlparse(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("TSecBench Base URL must be an absolute HTTP(S) URL without credentials or fragments")
    if not 1 <= timeout_seconds <= 300:
        raise ValueError("TSecBench timeout must be between 1 and 300 seconds")
    if not 1 <= max_concurrent <= 3:
        raise ValueError("TSecBench allows at most 3 concurrent challenge instances")
    if token is not None and len(token.strip()) > 4096:
        raise ValueError("TSecBench token is too long")
    settings = get_settings()
    with _runtime_lock:
        settings.tsecbench_base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        settings.tsecbench_timeout_seconds = timeout_seconds
        settings.tsecbench_max_concurrent = max_concurrent
        if clear_token:
            settings.tsecbench_token = None
        elif token is not None and token.strip():
            settings.tsecbench_token = token.strip()
    return public_tsecbench_config(settings)


class TSecBenchClient:
    """Small, dependency-free client for the TSecBench OpenAPI endpoints."""

    def __init__(self, settings: Settings | None = None, request_json: Callable[..., Any] | None = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.tsecbench_base_url.rstrip("/")
        self._request_json = request_json or self._http_json
        self._semaphore = threading.BoundedSemaphore(max(1, int(self.settings.tsecbench_max_concurrent or 1)))

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return urljoin(self.base_url + "/", path.lstrip("/"))

    @staticmethod
    def _unique_code(value: str) -> str:
        code = value.strip()
        if not code:
            raise ValueError("TSecBench unique_code must not be empty")
        return code

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if not self.settings.tsecbench_token:
            raise TSecBenchNeedsSession()
        with self._semaphore:
            try:
                return self._request_json(self._url(path), method=method, payload=payload, token=self.settings.tsecbench_token)
            except TypeError:
                # Test doubles and small integrations commonly expose a simpler
                # (url, method, payload, token) or (url, payload) signature.
                try:
                    return self._request_json(self._url(path), method, payload, self.settings.tsecbench_token)
                except TypeError:
                    return self._request_json(self._url(path), payload or {})

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        if isinstance(payload, dict):
            code = payload.get("code")
            if code in {401, 403, "401", "403"}:
                raise TSecBenchNeedsSession(f"TSecBench authentication failed with HTTP {code}")
            if isinstance(code, str) and code in {"task_not_found", "challenge_not_found", "invalid_state", "duplicate", "resource_unavailable", "internal_error"}:
                message = str(payload.get("message") or code)
                if code == "task_not_found":
                    raise TSecBenchNeedsSession(message)
                raise TSecBenchError(message, code=code, detail=payload.get("detail"))
            if payload.get("success") is False or payload.get("error"):
                message = payload.get("message") or payload.get("msg") or payload.get("error")
                raise TSecBenchError(str(message or "TSecBench API returned an error"))
            for key in ("data", "result"):
                value = payload.get(key)
                if value is not None:
                    return value
        return payload

    @staticmethod
    def _items(payload: Any) -> list[dict[str, Any]]:
        value = TSecBenchClient._unwrap(payload)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for key in ("items", "challenges", "list", "results"):
                if isinstance(value.get(key), list):
                    return [item for item in value[key] if isinstance(item, dict)]
            return [value] if value.get("unique_code") is not None else []
        raise TSecBenchError("TSecBench challenge list response is not a JSON object or array")

    @classmethod
    def parse_challenge(cls, raw: dict[str, Any]) -> TSecBenchChallenge:
        code = str(raw.get("unique_code") or raw.get("uniqueCode") or raw.get("unique-code") or raw.get("challenge_code") or "").strip()
        title = str(raw.get("title") or raw.get("name") or raw.get("challenge_name") or raw.get("challengeTitle") or code).strip()
        if not code or not title:
            raise TSecBenchError("TSecBench challenge is missing unique_code or title")
        description = str(raw.get("description") or raw.get("desc") or raw.get("content") or "")
        category = str(raw.get("category") or raw.get("type") or raw.get("challenge_type") or "unknown")
        container = raw.get("container") if isinstance(raw.get("container"), dict) else {}
        addr = raw.get("container_addr") or raw.get("containerAddr") or raw.get("container_address") or raw.get("target_url") or container.get("addr") or container.get("address") or []
        addresses = [str(value).strip() for value in (addr if isinstance(addr, list) else [addr]) if str(value).strip()]
        status = raw.get("container_status") or raw.get("containerStatus") or container.get("status") or raw.get("status")
        return TSecBenchChallenge(
            unique_code=code,
            title=title,
            description=description,
            challenge_type=category,
            difficulty=raw.get("difficulty"),
            level=raw.get("level") or raw.get("grade"),
            points=raw.get("total_score", raw.get("points", raw.get("point", raw.get("score", raw.get("value"))))),
            flag_count=raw.get("flag_count", raw.get("flagCount", raw.get("flags_count", raw.get("flag_num")))),
            container_status=str(status) if status is not None else None,
            container_addr=addresses,
            raw=raw,
            correct_flag_count=int(raw.get("correct_flag_count") or 0),
            is_completed=bool(raw.get("is_completed")),
        )

    def list_challenges(self) -> list[TSecBenchChallenge]:
        return [self.parse_challenge(item) for item in self._items(self._call("GET", "/openapi/v1/challenges"))]

    def start(self, unique_code: str) -> dict[str, Any]:
        value = self._unwrap(self._call("POST", f"/openapi/v1/challenges/start?unique_code={quote(self._unique_code(unique_code), safe='')}"))
        return value if isinstance(value, dict) else {"data": value}

    def close(self, unique_code: str) -> dict[str, Any]:
        value = self._unwrap(self._call("POST", f"/openapi/v1/challenges/close?unique_code={quote(self._unique_code(unique_code), safe='')}"))
        return value if isinstance(value, dict) else {"data": value}

    def hint(self, unique_code: str) -> str | None:
        value = self._unwrap(self._call("GET", f"/openapi/v1/challenges/hint?unique_code={quote(self._unique_code(unique_code), safe='')}"))
        if isinstance(value, str):
            return value.strip() or None
        if isinstance(value, dict):
            for key in ("hint", "content", "message", "text"):
                if value.get(key):
                    return str(value[key]).strip() or None
        return None

    def submit_result(self, unique_code: str, flag: str) -> TSecBenchSubmission | None:
        code = self._unique_code(unique_code)
        if not 1 <= len(flag) <= 4096:
            raise ValueError("TSecBench flag length must be between 1 and 4096 characters")
        value = self._unwrap(self._call("POST", "/openapi/v1/challenges/submit", {"unique_code": code, "flag": flag}))
        if isinstance(value, bool):
            return TSecBenchSubmission(value, 0, 0, int(value), int(value), None)
        if isinstance(value, dict):
            result = value.get("correct", value.get("accepted", value.get("success")))
            if isinstance(result, bool):
                return TSecBenchSubmission(
                    correct=result,
                    awarded=int(value.get("awarded") or 0),
                    cumulative_score=int(value.get("cumulative_score") or 0),
                    correct_flag_count=int(value.get("correct_flag_count") or 0),
                    total_flag_count=int(value.get("total_flag_count") or 0),
                    matched_flag_index=int(value["matched_flag_index"]) if value.get("matched_flag_index") is not None else None,
                )
        return None

    def submit(self, unique_code: str, flag: str) -> bool | None:
        result = self.submit_result(unique_code, flag)
        return result.correct if result is not None else None

    def _http_json(self, url: str, *, method: str = "GET", payload: dict[str, Any] | None = None, token: str | None = None) -> Any:
        headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Aurora-TSecBench/1.0", "BENCHMARK_TOKEN": token or ""}
        request = Request(url, headers=headers, method=method, data=json.dumps(payload).encode("utf-8") if payload is not None else None)
        try:
            with build_opener(network_proxy_registry.get().urllib_proxy_handler()).open(request, timeout=max(1, self.settings.tsecbench_timeout_seconds)) as response:
                raw = response.read(2 * 1024 * 1024)
                try:
                    return json.loads(raw.decode(response.headers.get_content_charset() or "utf-8"))
                except json.JSONDecodeError as exc:
                    raise TSecBenchError("TSecBench API returned a non-JSON response") from exc
        except HTTPError as exc:
            raw = exc.read(2 * 1024 * 1024)
            try:
                error_payload = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                error_payload = {}
            code = str(error_payload.get("code") or "")
            message = str(error_payload.get("message") or f"TSecBench API request failed with HTTP {exc.code}")
            if exc.code in {401, 403} or code == "task_not_found":
                raise TSecBenchNeedsSession(message) from exc
            raise TSecBenchError(message, status=exc.code, code=code or None, detail=error_payload.get("detail")) from exc
        except (URLError, TimeoutError) as exc:
            raise TSecBenchError(f"TSecBench API request failed: {exc}") from exc


def test_tsecbench_connection(settings: Settings | None = None) -> dict[str, Any]:
    current = settings or get_settings()
    challenges = TSecBenchClient(current).list_challenges()
    active = [challenge for challenge in challenges if challenge.container_status == "available" and challenge.container_addr]
    vpn = {"status": "unverified", "address": None, "message": "No active container address is available for VPN route verification."}
    if active:
        address = active[0].container_addr[0]
        parsed = urlparse(address if "://" in address else f"tcp://{address}")
        host = parsed.hostname
        port = parsed.port
        if host and port:
            try:
                with socket.create_connection((host, port), timeout=min(5, max(1, current.tsecbench_timeout_seconds))):
                    pass
                vpn = {"status": "reachable", "address": address, "message": "The active challenge address is reachable from the API host."}
            except OSError as exc:
                vpn = {"status": "unreachable", "address": address, "message": str(exc)[:300]}
    return {
        "api": {"status": "reachable", "challenge_count": len(challenges)},
        "vpn": vpn,
        "progress": {
            "completed": sum(1 for challenge in challenges if challenge.is_completed),
            "correct_flags": sum(challenge.correct_flag_count for challenge in challenges),
            "total_flags": sum(int(challenge.flag_count or 0) for challenge in challenges),
        },
    }
