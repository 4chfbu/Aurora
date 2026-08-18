#!/bin/sh
set -eu

: "${AURORA_LLM_BASE_URL:?AURORA_LLM_BASE_URL is required}"
: "${AURORA_LLM_API_KEY:?AURORA_LLM_API_KEY is required}"
: "${AURORA_LLM_MODEL:?AURORA_LLM_MODEL is required}"

provider_id="aurora-llm"
mkdir -p "${CC_SWITCH_CONFIG_DIR}" "${HOME}" "${XDG_RUNTIME_DIR}"
chmod 0700 "${CC_SWITCH_CONFIG_DIR}" "${HOME}" "${XDG_RUNTIME_DIR}"

# The container filesystem survives a normal Docker restart. The previous
# implementation deleted the active provider, which cc-switch rejects, then
# failed to add the same ID on every restart. Keep initialization idempotent;
# compose recreation still creates a fresh provider when configuration changes.
current_provider="$(cc-switch --app codex provider current 2>/dev/null || true)"
if ! printf '%s\n' "${current_provider}" | grep -Eq "ID:[[:space:]]*${provider_id}([[:space:]]|$)"; then
  provider_list="$(cc-switch --app codex provider list 2>/dev/null || true)"
  if ! printf '%s\n' "${provider_list}" | grep -Eq "(^|[^[:alnum:]_-])${provider_id}([^[:alnum:]_-]|$)"; then
    cc-switch --app codex provider add \
      --id "${provider_id}" \
      --name "Aurora LLM" \
      --base-url "${AURORA_LLM_BASE_URL}" \
      --api-key "${AURORA_LLM_API_KEY}" \
      --model "${AURORA_LLM_MODEL}" \
      --api-format chat
  fi
fi
cc-switch --app codex provider switch "${provider_id}"

exec cc-switch --app codex proxy serve \
  --listen-address 0.0.0.0 \
  --listen-port "${CC_SWITCH_LISTEN_PORT}"
