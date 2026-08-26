from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterable
from typing import Any

from sqlmodel import Session, select

from aurora.config import Settings, get_settings
from aurora.models import ChallengeGroup, ChallengeGroupItem, now_utc

_runtime_lock = threading.RLock()

DEFAULT_FLAG_PREFIXES: tuple[str, ...] = ("flag",)
_ENV_VAR = "AURORA_FLAG_PREFIXES"
_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$", re.IGNORECASE)


def _normalize_prefixes(values: Iterable[str]) -> list[str]:
    """Deduplicate, lowercase and validate user-supplied flag prefixes."""
    prefixes: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        if "{" in text or "}" in text:
            raise ValueError(f"invalid flag prefix {text!r}: prefixes must not contain braces")
        if not _PREFIX_RE.fullmatch(text):
            raise ValueError(
                f"invalid flag prefix {text!r}: use only letters, digits, underscores or hyphens, and start with a letter or digit"
            )
        lowered = text.lower()
        if lowered not in prefixes:
            prefixes.append(lowered)
    return prefixes


def current_flag_prefixes(settings: Settings | None = None) -> tuple[str, ...]:
    """Return the active global flag prefix whitelist (always lowercase)."""
    current = settings or get_settings()
    with _runtime_lock:
        values = list(current.flag_prefixes or ()) or list(DEFAULT_FLAG_PREFIXES)
    return tuple(values)


def flag_prefixes_for_project(session: Session, project_id: str | None) -> tuple[str, ...]:
    """Resolve the effective prefix whitelist for one project.

    A challenge group may override the global whitelist.  When the project
    belongs to a group that declares ``flag_prefixes``, that override wins;
    otherwise the global whitelist applies.
    """
    if project_id is None:
        return current_flag_prefixes()
    item = session.exec(
        select(ChallengeGroupItem)
        .where(ChallengeGroupItem.project_id == project_id)
        .order_by(ChallengeGroupItem.created_at.desc())
    ).first()
    if item is None:
        return current_flag_prefixes()
    group = session.get(ChallengeGroup, item.group_id)
    if group is None or not group.flag_prefixes:
        return current_flag_prefixes()
    return tuple(str(prefix).strip().lower() for prefix in group.flag_prefixes if str(prefix).strip())


def public_flag_prefix_config(settings: Settings | None = None) -> dict[str, Any]:
    """Describe the active global flag prefix policy for the settings UI."""
    current = settings or get_settings()
    with _runtime_lock:
        env_value = os.getenv(_ENV_VAR)
        prefixes = list(current.flag_prefixes or ()) or list(DEFAULT_FLAG_PREFIXES)
    source = (
        "environment"
        if env_value is not None
        else ("runtime" if tuple(prefixes) != DEFAULT_FLAG_PREFIXES else "default")
    )
    return {
        "prefixes": prefixes,
        "case_insensitive": True,
        "submit_preserves_case": True,
        "default": list(DEFAULT_FLAG_PREFIXES),
        "source": source,
    }


def configure_flag_prefixes(prefixes: Iterable[str]) -> dict[str, Any]:
    """Set the runtime global flag prefix whitelist."""
    normalized = _normalize_prefixes(prefixes)
    if not normalized:
        raise ValueError("at least one flag prefix is required")
    settings = get_settings()
    with _runtime_lock:
        settings.flag_prefixes = normalized
    return public_flag_prefix_config(settings)


def public_group_flag_prefix_config(session: Session, group_id: str) -> dict[str, Any]:
    """Describe the effective prefix policy for one challenge group."""
    group = session.get(ChallengeGroup, group_id)
    if group is None:
        raise ValueError("challenge group not found")
    override = [str(prefix).strip().lower() for prefix in (group.flag_prefixes or []) if str(prefix).strip()]
    return {
        "group_id": group_id,
        "prefixes": override or list(current_flag_prefixes()),
        "overridden": bool(override),
        "override": override or None,
        "global": list(current_flag_prefixes()),
        "case_insensitive": True,
        "submit_preserves_case": True,
    }


def configure_group_flag_prefixes(session: Session, group_id: str, prefixes: Iterable[str]) -> dict[str, Any]:
    """Set a per-group flag prefix override; an empty list clears the override.

    Clearing the override makes the group inherit the global whitelist.
    """
    group = session.get(ChallengeGroup, group_id)
    if group is None:
        raise ValueError("challenge group not found")
    normalized = _normalize_prefixes(prefixes)
    group.flag_prefixes = normalized or None
    group.updated_at = now_utc()
    session.add(group)
    session.commit()
    return public_group_flag_prefix_config(session, group_id)
