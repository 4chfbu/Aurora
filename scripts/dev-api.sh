#!/usr/bin/env bash
set -euo pipefail

uv sync --extra dev
uv run uvicorn apps.api.main:app \
  --reload \
  --reload-dir apps \
  --reload-dir aurora \
  --host 0.0.0.0 \
  --port 8000
