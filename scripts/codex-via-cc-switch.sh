#!/usr/bin/env bash
set -euo pipefail

PROMPT_FILE="${1:?prompt file required}"
SCHEMA_FILE="${2:-aurora-output-schema.json}"
LAST_MESSAGE_FILE="${3:-aurora-last-message.json}"
PROXY_BASE_URL="${OPENAI_BASE_URL:-http://aurora-cc-switch:15723/v1}"
MODEL="${OPENAI_MODEL:-gpt-4.1-mini}"
MODEL_CONTEXT_WINDOW="${AURORA_CODEX_MODEL_CONTEXT_WINDOW:-1000000}"
AUTO_COMPACT_TOKEN_LIMIT="${AURORA_CODEX_AUTO_COMPACT_TOKEN_LIMIT:-800000}"
PROXY_ORIGIN="${PROXY_BASE_URL%/}"
PROXY_ORIGIN="${PROXY_ORIGIN%/v1}"
PROVIDER_CONFIG="model_providers.aurora={ name=\"Aurora CC Switch\", base_url=\"${PROXY_BASE_URL}\", wire_api=\"responses\", requires_openai_auth=true, supports_websockets=false }"

[[ "${MODEL_CONTEXT_WINDOW}" =~ ^[0-9]+$ ]] || { printf 'Invalid model context window\n' >&2; exit 64; }
[[ "${AUTO_COMPACT_TOKEN_LIMIT}" =~ ^[0-9]+$ ]] || { printf 'Invalid auto compact token limit\n' >&2; exit 64; }

curl --fail --silent --show-error --max-time 10 "${PROXY_ORIGIN}/health" >/dev/null || {
  printf 'Aurora CC Switch proxy is unavailable at %s\n' "${PROXY_ORIGIN}" >&2
  exit 69
}

# Child Solver processes reuse the same network-local CC Switch proxy.
export AURORA_SUBAGENT_CODEX_COMMAND="codex exec -m $(printf '%q' "${MODEL}") -c 'model_provider=\"aurora\"' -c $(printf '%q' "${PROVIDER_CONFIG}") -c model_context_window=${MODEL_CONTEXT_WINDOW} -c model_auto_compact_token_limit=${AUTO_COMPACT_TOKEN_LIMIT} --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox --output-schema {schema_filename} --output-last-message {last_message_filename} - < {prompt_filename}"

codex exec \
  -m "${MODEL}" \
  -c 'model_provider="aurora"' \
  -c "${PROVIDER_CONFIG}" \
  -c "model_context_window=${MODEL_CONTEXT_WINDOW}" \
  -c "model_auto_compact_token_limit=${AUTO_COMPACT_TOKEN_LIMIT}" \
  --skip-git-repo-check \
  --dangerously-bypass-approvals-and-sandbox \
  --output-schema "${SCHEMA_FILE}" \
  --output-last-message "${LAST_MESSAGE_FILE}" \
  - < "${PROMPT_FILE}"
