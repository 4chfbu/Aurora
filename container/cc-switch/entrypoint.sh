#!/bin/sh
set -eu

: "${AURORA_LLM_BASE_URL:?AURORA_LLM_BASE_URL is required}"
: "${AURORA_LLM_API_KEY:?AURORA_LLM_API_KEY is required}"
: "${AURORA_LLM_MODEL:?AURORA_LLM_MODEL is required}"

provider_id="aurora-llm"
mkdir -p "${CC_SWITCH_CONFIG_DIR}" "${HOME}" "${XDG_RUNTIME_DIR}"
chmod 0700 "${CC_SWITCH_CONFIG_DIR}" "${HOME}" "${XDG_RUNTIME_DIR}"

# This container owns an isolated database, so recreating its sole provider is
# deterministic and never changes the operator's host-side CC Switch config.
cc-switch --app codex provider delete "${provider_id}" >/dev/null 2>&1 || true
cc-switch --app codex provider add \
  --id "${provider_id}" \
  --name "Aurora LLM" \
  --base-url "${AURORA_LLM_BASE_URL}" \
  --api-key "${AURORA_LLM_API_KEY}" \
  --model "${AURORA_LLM_MODEL}" \
  --api-format chat
cc-switch --app codex provider switch "${provider_id}"

exec cc-switch --app codex proxy serve \
  --listen-address 0.0.0.0 \
  --listen-port "${CC_SWITCH_LISTEN_PORT}"
