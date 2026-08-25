#!/bin/sh
set -eu

: "${AURORA_LLM_BASE_URL:?AURORA_LLM_BASE_URL is required}"
: "${AURORA_LLM_API_KEY:?AURORA_LLM_API_KEY is required}"
: "${AURORA_LLM_MODEL:?AURORA_LLM_MODEL is required}"

provider_id="aurora-llm"
mkdir -p "${CC_SWITCH_CONFIG_DIR}" "${HOME}" "${XDG_RUNTIME_DIR}"
chmod 0700 "${CC_SWITCH_CONFIG_DIR}" "${HOME}" "${XDG_RUNTIME_DIR}"

# cc-switch's field-mode registration writes a Codex provider with thinking
# enabled even when ``--api-format chat`` is selected.  That combination
# breaks DeepSeek-style providers because the proxy cannot pass the returned
# ``reasoning_content`` back to Codex.  Build the provider settings_config
# explicitly with reasoning disabled and keep the Chat Completions routing
# format, then reconcile the persisted provider on every start so existing
# containers are migrated instead of being silently left on the old config.
provider_config="${CC_SWITCH_CONFIG_DIR}/aurora-provider.json"
python3 - "${AURORA_LLM_BASE_URL}" "${AURORA_LLM_API_KEY}" "${AURORA_LLM_MODEL}" "${provider_config}" <<'PY'
import json
import sys

base_url, api_key, model, config_path = sys.argv[1:5]
settings_config = {
    "config": (
        'model_provider = "custom"\n'
        f'model = "{model}"\n'
        'model_reasoning_effort = "none"\n'
        'disable_response_storage = true\n'
        '\n'
        '[model_providers.custom]\n'
        'name = "Aurora LLM"\n'
        f'base_url = "{base_url}"\n'
        'wire_api = "responses"\n'
        'requires_openai_auth = true\n'
    ),
    "auth": {"OPENAI_API_KEY": api_key},
}
with open(config_path, "w", encoding="utf-8") as handle:
    json.dump(settings_config, handle)
PY
chmod 0600 "${provider_config}"

provider_list="$(cc-switch --app codex provider list 2>/dev/null || true)"
if printf '%s\n' "${provider_list}" | grep -Eq "(^|[^[:alnum:]_-])${provider_id}([^[:alnum:]_-]|$)"; then
  python3 - "${provider_config}" "${provider_id}" <<'PY'
import json
import os
import sqlite3
import sys
from pathlib import Path

config_path, provider_id = sys.argv[1:3]
db_path = Path(os.environ["CC_SWITCH_CONFIG_DIR"]) / "cc-switch.db"
settings_config = json.loads(Path(config_path).read_text(encoding="utf-8"))
connection = sqlite3.connect(str(db_path))
try:
    connection.execute(
        "UPDATE providers SET settings_config = ?, meta = ?, is_current = 1 "
        "WHERE id = ? AND app_type = 'codex'",
        (json.dumps(settings_config), json.dumps({"apiFormat": "openai_chat"}), provider_id),
    )
    connection.execute(
        "UPDATE providers SET is_current = 0 WHERE app_type = 'codex' AND id != ?",
        (provider_id,),
    )
    connection.commit()
finally:
    connection.close()
PY
else
  cc-switch --app codex provider add \
    --id "${provider_id}" \
    --name "Aurora LLM" \
    --config-file "${provider_config}" \
    --api-format chat
fi
cc-switch --app codex provider switch "${provider_id}"

exec cc-switch --app codex proxy serve \
  --listen-address 0.0.0.0 \
  --listen-port "${CC_SWITCH_LISTEN_PORT}"
