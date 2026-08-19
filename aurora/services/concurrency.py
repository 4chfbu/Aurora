from __future__ import annotations

import os
import threading
from typing import Any

from aurora.config import Settings, get_settings

_runtime_lock = threading.RLock()

MIN_MAX_AGENTS = 1
MAX_MAX_AGENTS = 8

_ENV_VAR = "AURORA_MAX_CHALLENGE_GROUP_CONCURRENT"
_ENV_DEFAULT = 2


def public_concurrency_config(settings: Settings | None = None) -> dict[str, Any]:
    """Return the current runtime concurrency limits for solver agents."""
    current = settings or get_settings()
    with _runtime_lock:
        env_override = os.getenv(_ENV_VAR)
        env_value = int(env_override) if env_override and env_override.lstrip("-").isdigit() else None
        return {
            "max_agents": int(current.max_challenge_group_concurrent or _ENV_DEFAULT),
            "min": MIN_MAX_AGENTS,
            "max": MAX_MAX_AGENTS,
            "source": "environment" if env_value is not None else ("runtime" if current.max_challenge_group_concurrent != _ENV_DEFAULT else "default"),
            "env_value": env_value,
        }


def configure_concurrency(*, max_agents: int) -> dict[str, Any]:
    """Set the global concurrent solver-agent cap at runtime.

    The cap bounds how many projects a challenge group runs in parallel
    (see ``ChallengeGroupRunner._max_workers``). It takes effect for the next
    group dispatch; a running pool keeps its original worker count.
    """
    if not MIN_MAX_AGENTS <= max_agents <= MAX_MAX_AGENTS:
        raise ValueError(f"max_agents must be between {MIN_MAX_AGENTS} and {MAX_MAX_AGENTS}")
    settings = get_settings()
    with _runtime_lock:
        settings.max_challenge_group_concurrent = int(max_agents)
    return public_concurrency_config(settings)
