#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../apps/web"
npm install
npm run build
