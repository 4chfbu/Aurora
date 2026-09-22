import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from common import audited


mcp = FastMCP("aurora_blackboard")
BASE_URL = os.getenv("AURORA_WORKER_CONTROL_BASE_URL", "").rstrip("/")
WORKER_ID = os.getenv("AURORA_WORKER_ID", "")
TOKEN = os.getenv("AURORA_WORKER_CONTROL_TOKEN", "")
REQUEST_TIMEOUT_SECONDS = max(1.0, float(os.getenv("AURORA_WORKER_CONTROL_TIMEOUT_SECONDS", "8")))
MAX_ATTEMPTS = max(1, int(os.getenv("AURORA_WORKER_CONTROL_MAX_ATTEMPTS", "3")))
RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _request(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not BASE_URL or not WORKER_ID or not TOKEN:
        raise RuntimeError("Aurora worker control channel is not configured")
    request_payload = dict(payload) if payload is not None else None
    if request_payload is not None:
        request_payload.setdefault("request_id", uuid.uuid4().hex)
    body = json.dumps(request_payload).encode("utf-8") if request_payload is not None else None
    parsed: Any = None
    last_error: Exception | None = None
    for attempt_number in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(
            f"{BASE_URL}/internal/workers/{WORKER_ID}{path}",
            data=body,
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
            method="POST" if request_payload is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                parsed = json.loads(response.read().decode("utf-8"))
            last_error = None
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP_STATUS or attempt_number >= MAX_ATTEMPTS:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Aurora control request failed: HTTP {exc.code}: {detail[:500]}") from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            last_error = exc
            if attempt_number >= MAX_ATTEMPTS:
                break
        time.sleep(min(0.2 * (2 ** (attempt_number - 1)), 1.0))
    if last_error is not None:
        raise RuntimeError(f"Aurora control request failed after {MAX_ATTEMPTS} attempts: {last_error}") from last_error
    if not isinstance(parsed, dict):
        raise RuntimeError("Aurora control response was not an object")
    return parsed


@mcp.tool()
def query() -> dict[str, Any]:
    """Read the latest scoped facts and checkpoints for this project."""
    return audited("aurora_blackboard", "query", {}, _query_with_fallback)


def _query_with_fallback() -> dict[str, Any]:
    try:
        snapshot = _request("/blackboard")
    except (RuntimeError, OSError) as exc:
        try:
            snapshot = json.loads(Path("runtime/blackboard.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RuntimeError("Live blackboard unavailable and no local snapshot exists; peer state is unknown") from exc
        if not isinstance(snapshot, dict):
            raise RuntimeError("Local blackboard snapshot is invalid") from exc
        return {**snapshot, "stale": True, "sync_error": str(exc)[:300], "guidance": "This is cached evidence. Peer state may have advanced; do not conclude that no peer breakthrough exists."}
    try:
        snapshot_path = Path("runtime/blackboard.json")
        snapshot_path.parent.mkdir(exist_ok=True)
        temporary = snapshot_path.with_suffix(f".mcp-{os.getpid()}.tmp")
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        temporary.replace(snapshot_path)
    except OSError:
        pass
    return snapshot


@mcp.tool()
def read_artifact(artifact_id: str, max_bytes: int = 64_000) -> dict[str, Any]:
    """Read a bounded text preview of a same-project Artifact referenced by the Blackboard."""
    payload = {"artifact_id": artifact_id, "max_bytes": max_bytes}
    encoded_id = urllib.parse.quote(artifact_id, safe="")
    return audited(
        "aurora_blackboard",
        "read_artifact",
        payload,
        lambda: _request(f"/artifacts/{encoded_id}?max_bytes={max_bytes}"),
    )


@mcp.tool()
def append_fact(statement: str, evidence_refs: list[str], category: str = "analysis", confidence: float = 0.7) -> dict[str, Any]:
    """Persist a fact using same-project Artifact IDs or files inside this Worker's /workspace."""
    payload = {"statement": statement, "evidence_refs": evidence_refs, "category": category, "confidence": confidence}
    return audited("aurora_blackboard", "append_fact", payload, lambda: _request("/facts", payload))


@mcp.tool()
def save_checkpoint(
    summary: str,
    completed_steps: list[str] | None = None,
    failed_routes: list[str] | None = None,
    next_step: str = "",
    artifact_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Save progress; artifact_refs may be Artifact IDs or files inside this Worker's /workspace."""
    payload = {
        "summary": summary,
        "completed_steps": completed_steps or [],
        "failed_routes": failed_routes or [],
        "next_step": next_step,
        "artifact_refs": artifact_refs or [],
    }
    return audited("aurora_blackboard", "save_checkpoint", payload, lambda: _request("/checkpoint", payload))


if __name__ == "__main__":
    mcp.run(transport="stdio")
