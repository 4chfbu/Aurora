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

# A single non-streaming response does not exercise the failure mode that
# matters to Solver Workers. Run the production Codex client through one shell
# action and require it to complete the follow-up turn; DeepSeek-style
# reasoning_content incompatibilities surface only on that continuation.
model="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "${container_id}" | sed -n 's/^AURORA_LLM_MODEL=//p' | head -n 1)"
worker_image="${AURORA_WORKER_IMAGE:-aurora-kali-codex:core}"
runtime_network="$(docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' "${container_id}" | head -n 1)"

set +e
multiturn_output="$({
  printf '%s\n' 'Use the shell tool once to run: printf provider-multiturn-ok. Then reply exactly ok.'
} | docker run --rm -i \
  --network "${runtime_network}" \
  --cpus 1 \
  --memory 1g \
  --user 1000:1000 \
  -e HOME=/tmp/aurora-home \
  -e CODEX_HOME=/tmp/aurora-codex-home \
  -e OPENAI_API_KEY=aurora-health-check \
  -e OPENAI_BASE_URL=http://aurora-cc-switch:15723/v1 \
  -e OPENAI_MODEL="${model}" \
  "${worker_image}" bash -lc '
    mkdir -p "$HOME" "$CODEX_HOME"
    provider="model_providers.aurora={ name=\"Aurora CC Switch\", base_url=\"${OPENAI_BASE_URL}\", wire_api=\"responses\", requires_openai_auth=true, supports_websockets=false }"
    codex exec -m "$OPENAI_MODEL" \
      -c '\''model_provider="aurora"'\'' \
      -c "$provider" \
      -c '\''model_reasoning_effort="none"'\'' \
      --skip-git-repo-check \
      --dangerously-bypass-approvals-and-sandbox \
      --json \
      --output-last-message /tmp/aurora-final-message.txt \
      -
  ' 2>&1)"
multiturn_status=$?
set -e
printf '%s\n' "${multiturn_output}"

if [[ ${multiturn_status} -ne 0 ]] \
  || grep -q 'reasoning_content.*must be passed back' <<<"${multiturn_output}" \
  || ! grep -q '"type":"turn.completed"' <<<"${multiturn_output}"; then
  printf 'CC Switch multi-turn Codex preflight failed.\n' >&2
  exit 1
fi
