#!/usr/bin/env bash
set -euo pipefail

CORE_IMAGE="${AURORA_WORKER_IMAGE_CORE:-${AURORA_WORKER_IMAGE:-aurora-kali-codex:core}}"
HEAVY_IMAGE="${AURORA_WORKER_IMAGE_HEAVY:-aurora-kali-codex:heavy}"
MANIFEST_SHA="$(sha256sum aurora/tool_profiles.json | awk '{print $1}')"
build_args=()
if [[ -n "${AURORA_BUILD_PROXY:-}" ]]; then
  build_args+=(
    --build-arg "HTTP_PROXY=${AURORA_BUILD_PROXY}"
    --build-arg "HTTPS_PROXY=${AURORA_BUILD_PROXY}"
    --build-arg "NO_PROXY=127.0.0.1,localhost"
  )
fi
build_args+=(--build-arg "AURORA_TOOL_MANIFEST_SHA=${MANIFEST_SHA}")
docker build "${build_args[@]}" --target core -t "${CORE_IMAGE}" -t aurora-kali-codex:latest -f container/kali-codex/Dockerfile .
docker build "${build_args[@]}" --target heavy -t "${HEAVY_IMAGE}" -f container/kali-codex/Dockerfile .
docker run --rm "${CORE_IMAGE}" bash -lc 'set -e; cat /etc/os-release | sed -n "1,3p"; codex --version; rizin -v | head -n 1; gdb --version | head -n 1; pwndbg --version; test -f /opt/aurora-skills/glibc-heap-primitives/SKILL.md; grep -q "aurora-skills" /root/.codex/config.toml'
docker run --rm -e AURORA_SMOKE_GHIDRA=1 -v "$(pwd)/scripts/smoke-local-mcp.py:/tmp/smoke-local-mcp.py:ro" "${HEAVY_IMAGE}" bash -lc 'set -e; command -v analyzeHeadless ghidra hashcat vol; python -c "import angr, volatility3, lief"; python /tmp/smoke-local-mcp.py; test -x "$(command -v glibc-aio)"; hashcat -I'
