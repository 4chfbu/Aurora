#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${AURORA_WORKER_IMAGE:-aurora-kali-codex:latest}"
build_args=()
if [[ -n "${AURORA_BUILD_PROXY:-}" ]]; then
  build_args+=(
    --build-arg "HTTP_PROXY=${AURORA_BUILD_PROXY}"
    --build-arg "HTTPS_PROXY=${AURORA_BUILD_PROXY}"
    --build-arg "NO_PROXY=127.0.0.1,localhost"
  )
fi
docker build "${build_args[@]}" -t "${IMAGE_NAME}" -f container/kali-codex/Dockerfile .
docker run --rm "${IMAGE_NAME}" bash -lc 'cat /etc/os-release | sed -n "1,3p"; command -v codex; codex --version'
