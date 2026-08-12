#!/usr/bin/env bash
set -euo pipefail

CORE_IMAGE="${AURORA_WORKER_IMAGE_CORE:-${AURORA_WORKER_IMAGE:-aurora-kali-codex:core}}"
HEAVY_IMAGE="${AURORA_WORKER_IMAGE_HEAVY:-aurora-kali-codex:heavy}"
docker image inspect "${CORE_IMAGE}" "${HEAVY_IMAGE}" >/dev/null
docker run --rm -v "$(pwd)/scripts/smoke-local-mcp.py:/tmp/smoke-local-mcp.py:ro" "${CORE_IMAGE}" bash -lc 'test -x "$(command -v codex)"; test -x "$(command -v rizin)"; test -x "$(command -v gdb)"; test -x "$(command -v tesseract)"; python -c "from PIL import Image"; codex mcp list; python /tmp/smoke-local-mcp.py'
docker run --rm -e AURORA_SMOKE_GHIDRA=1 -v "$(pwd)/scripts/smoke-local-mcp.py:/tmp/smoke-local-mcp.py:ro" "${HEAVY_IMAGE}" bash -lc 'test -x "$(command -v analyzeHeadless)"; test -x "$(command -v hashcat)"; python -c "import angr, volatility3"; python /tmp/smoke-local-mcp.py; hashcat -I >/dev/null'
