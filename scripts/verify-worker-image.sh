#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${AURORA_WORKER_IMAGE:-aurora-kali-codex:latest}"
docker image inspect "${IMAGE_NAME}" >/dev/null
docker run --rm "${IMAGE_NAME}" bash -lc 'test -x "$(command -v codex)" && codex --version'
