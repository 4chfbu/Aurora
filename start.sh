#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${PROJECT_DIR}"

rebuild=false
if [[ "${1:-}" == "--rebuild" ]]; then
  rebuild=true
elif [[ $# -gt 0 ]]; then
  printf 'Usage: %s [--rebuild]\n' "$0" >&2
  exit 2
fi

log() {
  printf '\n[Aurora] %s\n' "$1"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Required command is not installed: %s\n' "$1" >&2
    exit 1
  fi
}

require_command docker
require_command npm
require_command uv

if ! docker info >/dev/null 2>&1; then
  printf 'Docker daemon is unavailable or the current user cannot access it.\n' >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  printf 'Created .env from .env.example. Configure AURORA_LLM_BASE_URL, AURORA_LLM_API_KEY, and AURORA_LLM_MODEL, then run %s again.\n' "$0" >&2
  exit 1
fi

log "Synchronizing Python dependencies"
uv sync --extra dev

mapfile -t runtime_images < <(.venv/bin/python - <<'PY'
from aurora.config import get_settings
settings = get_settings()
print(settings.worker_image_core)
print(settings.worker_image_heavy)
print(settings.openvpn_image)
print(settings.api_host)
print(settings.api_port)
PY
)
core_image="${runtime_images[0]}"
heavy_image="${runtime_images[1]}"
vpn_image="${runtime_images[2]}"
api_host="${runtime_images[3]}"
api_port="${runtime_images[4]}"
manifest_sha="$(sha256sum aurora/tool_profiles.json | awk '{print $1}')"

worker_images_current() {
  local core_profile heavy_profile core_manifest heavy_manifest
  docker image inspect "${core_image}" "${heavy_image}" >/dev/null 2>&1 || return 1
  core_profile="$(docker image inspect --format '{{index .Config.Labels "io.aurora.worker.profile"}}' "${core_image}")"
  heavy_profile="$(docker image inspect --format '{{index .Config.Labels "io.aurora.worker.profile"}}' "${heavy_image}")"
  core_manifest="$(docker image inspect --format '{{index .Config.Labels "io.aurora.tool-manifest-sha256"}}' "${core_image}")"
  heavy_manifest="$(docker image inspect --format '{{index .Config.Labels "io.aurora.tool-manifest-sha256"}}' "${heavy_image}")"
  [[ "${core_profile}" == "core" && "${heavy_profile}" == "heavy" && "${core_manifest}" == "${manifest_sha}" && "${heavy_manifest}" == "${manifest_sha}" ]]
}

if [[ "${rebuild}" == true ]] || ! worker_images_current; then
  log "Building missing or outdated Solver Worker images"
  AURORA_WORKER_IMAGE_CORE="${core_image}" AURORA_WORKER_IMAGE_HEAVY="${heavy_image}" ./scripts/build-kali-codex.sh
else
  log "Reusing existing Solver Worker images"
fi

if [[ "${rebuild}" == true ]] || ! docker image inspect "${vpn_image}" >/dev/null 2>&1; then
  log "Building the optional OpenVPN gateway image"
  AURORA_OPENVPN_IMAGE="${vpn_image}" ./scripts/build-openvpn.sh
else
  log "Reusing existing OpenVPN gateway image"
fi

if [[ ! -d apps/web/node_modules || ! -f apps/web/node_modules/.package-lock.json || apps/web/package-lock.json -nt apps/web/node_modules/.package-lock.json ]]; then
  log "Installing Web dependencies"
  npm --prefix apps/web ci
fi
log "Building the Web application"
npm --prefix apps/web run build

if [[ "${rebuild}" == true ]] || ! docker image inspect aurora-cc-switch:5.9.3 >/dev/null 2>&1; then
  log "Preparing and building CC Switch"
  ./scripts/prepare-cc-switch.sh
  docker compose -f compose.runtime.yaml build cc-switch
else
  log "Reusing existing CC Switch image"
fi

log "Starting the private runtime network and CC Switch"
docker compose -f compose.runtime.yaml up -d --no-build --wait

display_host="${api_host}"
if [[ "${display_host}" == "0.0.0.0" || "${display_host}" == "::" ]]; then
  display_host="127.0.0.1"
fi
log "Aurora is ready at http://${display_host}:${api_port} (Ctrl+C stops the API; ./scripts/runtime-down.sh stops runtime containers)"
exec uv run uvicorn apps.api.main:app --host "${api_host}" --port "${api_port}"
