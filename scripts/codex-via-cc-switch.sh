#!/usr/bin/env bash
set -euo pipefail

PROMPT_FILE="${1:?prompt file required}"
SCHEMA_FILE="${2:-aurora-output-schema.json}"
LAST_MESSAGE_FILE="${3:-aurora-last-message.json}"
PROXY_BASE_URL="${OPENAI_BASE_URL:-http://aurora-cc-switch:15723/v1}"
MODEL="${OPENAI_MODEL:-gpt-4.1-mini}"
MODEL_REASONING_EFFORT="${AURORA_CODEX_MODEL_REASONING_EFFORT:-none}"
MODEL_CONTEXT_WINDOW="${AURORA_CODEX_MODEL_CONTEXT_WINDOW:-1000000}"
AUTO_COMPACT_TOKEN_LIMIT="${AURORA_CODEX_AUTO_COMPACT_TOKEN_LIMIT:-800000}"
RESUME_THREAD_ID="${AURORA_CODEX_RESUME_THREAD_ID:-}"
PROXY_ORIGIN="${PROXY_BASE_URL%/}"
PROXY_ORIGIN="${PROXY_ORIGIN%/v1}"
PROVIDER_CONFIG="model_providers.aurora={ name=\"Aurora CC Switch\", base_url=\"${PROXY_BASE_URL}\", wire_api=\"responses\", requires_openai_auth=true, supports_websockets=false }"
export CODEX_HOME="${CODEX_HOME:-/workspace/runtime/codex-home}"

mkdir -p "${CODEX_HOME}"
if [[ ! -f "${CODEX_HOME}/config.toml" && -f /workspace/runtime/codex-config.toml ]]; then
  cp /workspace/runtime/codex-config.toml "${CODEX_HOME}/config.toml"
fi

[[ "${MODEL_CONTEXT_WINDOW}" =~ ^[0-9]+$ ]] || { printf 'Invalid model context window\n' >&2; exit 64; }
[[ "${AUTO_COMPACT_TOKEN_LIMIT}" =~ ^[0-9]+$ ]] || { printf 'Invalid auto compact token limit\n' >&2; exit 64; }

MODEL_CATALOG_FILE="${CODEX_HOME}/aurora-model-catalog.json"
codex debug models | jq \
  --arg model "${MODEL}" \
  --argjson context_window "${MODEL_CONTEXT_WINDOW}" \
  --argjson auto_compact_token_limit "${AUTO_COMPACT_TOKEN_LIMIT}" \
  '{models: [
    (([.models[] | select(.slug == "gpt-5.2")][0] // .models[0])
      | .slug = $model
      | .display_name = $model
      | .description = "Aurora configured provider model"
      | .default_reasoning_level = "none"
      | .supported_reasoning_levels = [{"effort": "none", "description": "Provider configured reasoning"}]
      | .context_window = $context_window
      | .max_context_window = $context_window
      | .auto_compact_token_limit = $auto_compact_token_limit)
  ]}' > "${MODEL_CATALOG_FILE}"

curl --fail --silent --show-error --max-time 10 "${PROXY_ORIGIN}/health" >/dev/null || {
  printf 'Aurora CC Switch proxy is unavailable at %s\n' "${PROXY_ORIGIN}" >&2
  exit 69
}

# Child Solver processes reuse the same network-local CC Switch proxy.
export AURORA_SUBAGENT_CODEX_COMMAND="codex exec -m $(printf '%q' "${MODEL}") -c 'model_provider=\"aurora\"' -c $(printf '%q' "${PROVIDER_CONFIG}") -c $(printf '%q' "model_catalog_json=\"${MODEL_CATALOG_FILE}\"") -c $(printf '%q' "model_reasoning_effort=\"${MODEL_REASONING_EFFORT}\"") -c model_context_window=${MODEL_CONTEXT_WINDOW} -c model_auto_compact_token_limit=${AUTO_COMPACT_TOKEN_LIMIT} --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox --output-schema {schema_filename} --output-last-message {last_message_filename} - < {prompt_filename}"

common_args=(
  -m "${MODEL}"
  -c 'model_provider="aurora"'
  -c "${PROVIDER_CONFIG}"
  -c "model_catalog_json=\"${MODEL_CATALOG_FILE}\""
  -c "model_reasoning_effort=\"${MODEL_REASONING_EFFORT}\""
  -c "model_context_window=${MODEL_CONTEXT_WINDOW}"
  -c "model_auto_compact_token_limit=${AUTO_COMPACT_TOKEN_LIMIT}"
  --skip-git-repo-check
  --dangerously-bypass-approvals-and-sandbox
  --json
  --output-schema "${SCHEMA_FILE}"
  --output-last-message "${LAST_MESSAGE_FILE}"
)

if [[ -n "${RESUME_THREAD_ID}" ]]; then
  codex exec resume "${common_args[@]}" "${RESUME_THREAD_ID}" - < "${PROMPT_FILE}"
else
  codex exec "${common_args[@]}" - < "${PROMPT_FILE}"
fi
