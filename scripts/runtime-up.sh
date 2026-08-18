#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

CORE_IMAGE="${AURORA_WORKER_IMAGE_CORE:-${AURORA_WORKER_IMAGE:-aurora-kali-codex:core}}"
HEAVY_IMAGE="${AURORA_WORKER_IMAGE_HEAVY:-aurora-kali-codex:heavy}"

AURORA_WORKER_IMAGE_CORE="${CORE_IMAGE}" AURORA_WORKER_IMAGE_HEAVY="${HEAVY_IMAGE}" ./scripts/build-kali-codex.sh
./scripts/build-openvpn.sh
./scripts/prepare-cc-switch.sh
docker compose -f compose.runtime.yaml up -d --build --wait
docker image inspect "${CORE_IMAGE}" "${HEAVY_IMAGE}" >/dev/null

printf 'Aurora runtime is ready: cc-switch is healthy; Worker images %s and %s are built.\n' "${CORE_IMAGE}" "${HEAVY_IMAGE}"
