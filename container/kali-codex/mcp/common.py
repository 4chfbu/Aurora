from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, TypeVar


WORKSPACE = Path("/workspace").resolve()
T = TypeVar("T")


def workspace_path(value: str, *, must_exist: bool = True) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = WORKSPACE / candidate
    resolved = candidate.resolve()
    if resolved != WORKSPACE and WORKSPACE not in resolved.parents:
        raise ValueError("path must be inside /workspace; copy the file into /workspace and retry")
    if must_exist and not resolved.exists():
        raise ValueError(f"path does not exist: {value}")
    return resolved


def audited(server: str, tool: str, request: dict[str, Any], operation: Callable[[], T]) -> T:
    started = time.monotonic()
    event: dict[str, Any] = {
        "server": server,
        "tool": tool,
        "request": _sanitize(request),
        "started_at": time.time(),
    }
    try:
        result = operation()
        event.update({"success": True, "summary": _summary(result)})
        return result
    except Exception as exc:
        event.update({"success": False, "summary": f"{type(exc).__name__}: {exc}"[:500]})
        raise
    finally:
        event["duration_ms"] = round((time.monotonic() - started) * 1000)
        _append_event(event)


def _append_event(event: dict[str, Any]) -> None:
    log_path = Path(os.getenv("AURORA_MCP_EVENT_LOG", "/workspace/runtime/mcp-events.jsonl"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("<redacted>" if key.lower() in {"env", "password", "token", "secret"} else _sanitize(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value[:50]]
    if isinstance(value, str):
        return value[:1000]
    return value


def _summary(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("summary", "status", "session_id"):
            if result.get(key):
                return str(result[key])[:500]
    return str(result)[:500]
