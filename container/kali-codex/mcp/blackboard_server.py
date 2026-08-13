from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from mcp.server.fastmcp import FastMCP

from common import audited


mcp = FastMCP("aurora_blackboard")
BASE_URL = os.getenv("AURORA_WORKER_CONTROL_BASE_URL", "").rstrip("/")
WORKER_ID = os.getenv("AURORA_WORKER_ID", "")
TOKEN = os.getenv("AURORA_WORKER_CONTROL_TOKEN", "")


def _request(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not BASE_URL or not WORKER_ID or not TOKEN:
        raise RuntimeError("Aurora worker control channel is not configured")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{BASE_URL}/internal/workers/{WORKER_ID}{path}",
        data=body,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Aurora control request failed: HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Aurora control request failed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Aurora control response was not an object")
    return parsed


@mcp.tool()
def query() -> dict[str, Any]:
    """Read the latest scoped facts and checkpoints for this project."""
    return audited("aurora_blackboard", "query", {}, lambda: _request("/blackboard"))


@mcp.tool()
def append_fact(statement: str, evidence_refs: list[str], category: str = "analysis", confidence: float = 0.7) -> dict[str, Any]:
    """Persist an evidence-backed fact immediately to the project Blackboard."""
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
    """Save current progress before a long operation or final response."""
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

