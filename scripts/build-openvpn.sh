#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
VPN_IMAGE="${AURORA_OPENVPN_IMAGE:-aurora-openvpn:latest}"
docker build -t "${VPN_IMAGE}" -f container/openvpn/Dockerfile .
docker run --rm --entrypoint openvpn "${VPN_IMAGE}" --version >/dev/null
printf 'Aurora OpenVPN gateway image is ready: %s\n' "${VPN_IMAGE}"
