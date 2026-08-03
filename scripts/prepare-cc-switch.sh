#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

version="5.9.3"
archive_sha256="a581ec26efda795182949243665ea725d42029c58bb4b9137d0708b255a4fb91"
binary_sha256="975eac469c7b75ae0d8747169824aa863145a209106a93043149cbc4cafe3e29"
cache_dir=".runtime-cache"
target="${cache_dir}/cc-switch"

mkdir -p "${cache_dir}"

verify_binary() {
  [[ -x "$1" ]] \
    && [[ "$("$1" --version)" == "cc-switch ${version}" ]] \
    && [[ "$(sha256sum "$1" | awk '{print $1}')" == "${binary_sha256}" ]]
}

if verify_binary "${target}"; then
  exit 0
fi

installed="$(command -v cc-switch || true)"
if [[ -n "${installed}" ]] && verify_binary "${installed}"; then
  install -m 0755 "${installed}" "${target}"
  exit 0
fi

temp_dir="$(mktemp -d)"
trap 'rm -rf "${temp_dir}"' EXIT
archive="${temp_dir}/cc-switch.tar.gz"
curl -fL --retry 5 --retry-delay 2 \
  "https://github.com/SaladDay/cc-switch-cli/releases/download/v${version}/cc-switch-cli-linux-x64-musl.tar.gz" \
  -o "${archive}"
echo "${archive_sha256}  ${archive}" | sha256sum -c -
tar -xzf "${archive}" -C "${temp_dir}"
extracted="$(find "${temp_dir}" -maxdepth 2 -type f -name 'cc-switch*' ! -name '*.tar.gz' | head -n 1)"
test -n "${extracted}"
install -m 0755 "${extracted}" "${target}"
verify_binary "${target}"
