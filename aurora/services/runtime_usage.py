from __future__ import annotations

import json
import re
from pathlib import Path


def session_token_usage(workspace: Path, thread_id: str | None) -> dict[str, int]:
    if not thread_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", thread_id):
        return {}
    sessions = workspace / "runtime" / "codex-home" / "sessions"
    latest = {}
    for path in sorted(sessions.rglob(f"*{thread_id}.jsonl")):
        if path.name != f"{thread_id}.jsonl" and not path.name.endswith(f"-{thread_id}.jsonl"):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(sessions.resolve()):
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict) or event.get("type") != "event_msg":
                        continue
                    payload = event.get("payload")
                    if not isinstance(payload, dict) or payload.get("type") != "token_count":
                        continue
                    info = payload.get("info")
                    usage = info.get("total_token_usage") if isinstance(info, dict) else None
                    if not isinstance(usage, dict) or not isinstance(usage.get("input_tokens"), int):
                        continue
                    measured = {key: value for key, value in usage.items()
                                if key.endswith("tokens") and isinstance(value, int) and not isinstance(value, bool) and value >= 0}
                    if measured.get("input_tokens", -1) >= latest.get("input_tokens", -1):
                        latest = measured
        except OSError:
            continue
    return latest


def session_usage_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    if "input_tokens" not in after or any(after.get(key, 0) < value for key, value in before.items()):
        return {}
    delta = {key: value - before.get(key, 0) for key, value in after.items()}
    return delta if any(delta.values()) else {}
