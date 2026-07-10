#!/usr/bin/env bash
set -euo pipefail

version="1.7.12"
os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"

case "$os/$arch" in
  linux/x86_64)
    platform="linux_amd64"
    expected_sha256="8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"
    ;;
  darwin/arm64)
    platform="darwin_arm64"
    expected_sha256="aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f"
    ;;
  darwin/x86_64)
    platform="darwin_amd64"
    expected_sha256="5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644"
    ;;
  *)
    echo "Unsupported actionlint platform: $os/$arch" >&2
    exit 1
    ;;
esac

archive="actionlint_${version}_${platform}.tar.gz"
url="https://github.com/rhysd/actionlint/releases/download/v${version}/${archive}"
install_dir="${ACTIONLINT_INSTALL_DIR:-${RUNNER_TEMP:-/tmp}/actionlint-${version}}"
mkdir -p "$install_dir"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
curl --fail --location --proto '=https' --tlsv1.2 \
  --retry 3 --retry-all-errors \
  --output "$tmp_dir/$archive" "$url"
printf '%s  %s\n' "$expected_sha256" "$tmp_dir/$archive" | shasum -a 256 --check >&2
tar -xzf "$tmp_dir/$archive" -C "$install_dir" actionlint

printf '%s\n' "$install_dir/actionlint"
