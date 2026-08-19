from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from aurora.config import Settings, get_settings

RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class LLMRequestError(RuntimeError):
    """Raised when an LLM chat completion request fails after all retries."""


def chat_completion(
    *,
    settings: Settings | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str,
    messages: list[dict[str, str]],
    timeout: int | None = None,
    temperature: float = 0.2,
    json_mode: bool = True,
    max_retries: int = 3,
) -> dict[str, Any]:
    """Send an OpenAI-compatible chat completion request with bounded retry.

    Retries 429/5xx HTTP responses and transient URLErrors using exponential
    backoff. Returns the parsed JSON body. Raises :class:`LLMRequestError`
    when every attempt fails or the response carries a non-retryable HTTP error.
    """
    resolved_settings = settings or get_settings()
    resolved_base = (base_url or resolved_settings.llm_base_url).rstrip("/")
    resolved_key = api_key if api_key is not None else resolved_settings.llm_api_key
    resolved_timeout = timeout if timeout is not None else resolved_settings.llm_timeout_seconds

    url = f"{resolved_base}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    last_error: str | None = None
    for attempt in range(max_retries):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {resolved_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=resolved_timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            last_error = f"HTTP {exc.code}: {detail[:1000]}"
            if exc.code in RETRYABLE_STATUS_CODES and attempt < max_retries - 1:
                time.sleep(min(8.0, 1.5 ** (attempt + 1)))
                continue
            raise LLMRequestError(f"LLM API request failed: {last_error}") from exc
        except urllib.error.URLError as exc:
            last_error = str(exc)
            if attempt < max_retries - 1:
                time.sleep(min(8.0, 1.5 ** (attempt + 1)))
                continue
            raise LLMRequestError(f"LLM API request failed: {last_error}") from exc
        except Exception as exc:  # noqa: BLE001 - surface any remaining transport fault
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max_retries - 1:
                time.sleep(min(8.0, 1.5 ** (attempt + 1)))
                continue
            raise LLMRequestError(f"LLM API request failed: {last_error}") from exc

    raise LLMRequestError(f"LLM API request failed after {max_retries} attempts: {last_error}")
