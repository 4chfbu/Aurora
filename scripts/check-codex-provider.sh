#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

container_id="$(docker compose -f compose.runtime.yaml ps -q cc-switch)"
if [[ -z "${container_id}" ]]; then
  printf 'CC Switch runtime is not running. Start it with ./scripts/runtime-up.sh\n' >&2
  exit 1
fi

proxy_ip="$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "${container_id}")"
if [[ -z "${proxy_ip}" ]]; then
  printf 'Could not resolve the CC Switch container address.\n' >&2
  exit 1
fi

AURORA_CHECK_PROXY_URL="http://${proxy_ip}:15723/v1" uv run python - <<'PY'
import json
import os
import urllib.error
import urllib.request

from aurora.config import get_settings

settings = get_settings()
base = os.environ["AURORA_CHECK_PROXY_URL"].rstrip("/")

def request(method: str, endpoint: str, payload: dict | None = None) -> tuple[int, str]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + endpoint,
        data=data,
        method=method,
        headers={"Authorization": "Bearer aurora-health-check", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.llm_timeout_seconds) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")

health_url = base.removesuffix("/v1") + "/health"
with urllib.request.urlopen(health_url, timeout=10) as response:
    print({"endpoint": "/health", "status": response.status, "ok": response.status == 200})

status, body = request(
    "POST",
    "/responses",
    {"model": settings.llm_model, "input": "Return exactly: ok", "stream": False},
)
print({"endpoint": "/v1/responses", "status": status, "ok": 200 <= status < 300, "body_prefix": body[:500]})
if not 200 <= status < 300:
    raise SystemExit("CC Switch did not convert the Responses request successfully.")
PY
