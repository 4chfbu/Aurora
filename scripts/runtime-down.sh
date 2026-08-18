#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mapfile -t vpn_containers < <(docker ps -aq --filter label=aurora.vpn=true)
if (( ${#vpn_containers[@]} > 0 )); then
  docker rm -f "${vpn_containers[@]}"
fi
docker compose -f compose.runtime.yaml down
