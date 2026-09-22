"""Bound model-facing memory without corrupting identifiers or tool arguments."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any


def context_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


TEXT_FIELDS = {
    "project_goal", "objective", "statement", "description", "summary", "reason",
    "hint", "hint_content", "conclusions", "hypotheses", "failed_routes", "next_steps",
    "next_step", "completed_steps", "previous_attempts", "notices", "reproduction",
}


def fit_context(
    sections: dict[str, Any], *, max_bytes: int,
    pinned_fact_ids: set[str], pinned_checkpoint_ids: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Keep all required references; archive access is supplied by the caller.

    First bound oversized prose, then drop optional history. Executable tool arguments,
    IDs, paths, rejection values and budgets must remain exact. If that minimal
    contract cannot fit, fail explicitly instead of silently exceeding the cap.
    """
    result = deepcopy(sections)
    report: dict[str, Any] = {"truncated": False, "original_chars": len(json.dumps(sections, ensure_ascii=False)),
                              "original_bytes": context_bytes(sections), "limit_bytes": max_bytes,
                              "omitted_items": {}, "shortened_fields": []}
    shortened: set[str] = set()

    def shorten(value: Any, limit: int, *, path: str = "", prose: bool = False) -> Any:
        if isinstance(value, dict):
            return {
                key: child if key in {"budget", "tool_request", "authorization_scope", "context_memory", "id", "path", "url", "sha256", "artifact_refs", "fact_refs", "evidence_refs", "value", "rejected_values"}
                or key.endswith(("_id", "_ids"))
                else shorten(child, limit, path=f"{path}.{key}".strip("."), prose=prose or key in TEXT_FIELDS)
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [shorten(item, limit, path=f"{path}[{index}]", prose=prose) for index, item in enumerate(value)]
        if prose and isinstance(value, str) and len(value) > limit:
            shortened.add(path)
            return value[:limit] + " … [full text in context_memory]"
        return value

    if context_bytes(result) > max_bytes:
        result = shorten(result, 8000)
    if context_bytes(result) > max_bytes:
        for key, protected in (("artifact_summaries", set()), ("recent_checkpoints", pinned_checkpoint_ids), ("facts", pinned_fact_ids)):
            values = result.get(key, [])
            retained = [item for item in values if isinstance(item, dict) and item.get("id") in protected]
            if len(retained) < len(values):
                result[key] = retained
                report["omitted_items"][key] = len(values) - len(retained)
            if context_bytes(result) <= max_bytes:
                break
    for limit in (2000, 512, 128):
        if context_bytes(result) <= max_bytes:
            break
        result = shorten(result, limit)
    kept_bytes = context_bytes(result)
    if kept_bytes > max_bytes:
        raise ValueError(f"Required context needs {kept_bytes} bytes after compaction; context limit is {max_bytes}. Increase max_context_snapshot_bytes or reduce required dependencies.")
    report.update(truncated=result != sections, kept_bytes=kept_bytes, shortened_fields=sorted(shortened))
    return result, report
