#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

./scripts/build-kali-codex.sh
./scripts/prepare-cc-switch.sh
docker compose -f compose.runtime.yaml up -d --build --wait

printf 'Aurora runtime is ready: cc-switch is healthy and the Worker image is built.\n'
