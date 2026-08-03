#!/usr/bin/env bash
set -euo pipefail

uv run --extra dev pytest
cd "$(dirname "$0")/../apps/web"
npm run build
