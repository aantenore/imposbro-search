#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
expected_uv_version="${EXPECTED_UV_VERSION:-0.11.21}"
lock_python_version="${LOCK_PYTHON_VERSION:-3.12}"
uv_bin="${UV:-uv}"

if ! command -v "$uv_bin" >/dev/null 2>&1; then
  echo "uv is required to verify generated dependency locks (tried: $uv_bin)" >&2
  exit 1
fi

actual_uv_version="$("$uv_bin" --version | awk '{print $2}')"
if [[ "$actual_uv_version" != "$expected_uv_version" ]]; then
  echo "Expected uv $expected_uv_version, found $actual_uv_version" >&2
  exit 1
fi

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

status=0
for relative_dir in query_api indexing_service; do
  mkdir -p "$work_dir/$relative_dir"
  cp "$repo_root/$relative_dir/requirements.txt" "$work_dir/$relative_dir/requirements.txt"
  cp "$repo_root/$relative_dir/requirements.lock" "$work_dir/$relative_dir/requirements.lock"

  (
    cd "$work_dir/$relative_dir"
    "$uv_bin" pip compile requirements.txt \
      --universal \
      --python-version "$lock_python_version" \
      --generate-hashes \
      --output-file requirements.lock >/dev/null
  )

  if ! cmp -s "$repo_root/$relative_dir/requirements.lock" "$work_dir/$relative_dir/requirements.lock"; then
    echo "Dependency lock is stale: $relative_dir/requirements.lock" >&2
    diff -u \
      "$repo_root/$relative_dir/requirements.lock" \
      "$work_dir/$relative_dir/requirements.lock" || true
    status=1
  fi
done

mkdir -p "$work_dir/scripts/ci" "$work_dir/query_api" "$work_dir/indexing_service"
cp "$repo_root/scripts/ci/python-tools-requirements.txt" "$work_dir/scripts/ci/"
cp "$repo_root/scripts/ci/python-tools-query.lock" "$work_dir/scripts/ci/"
cp "$repo_root/scripts/ci/python-tools-indexing.lock" "$work_dir/scripts/ci/"
cp "$repo_root/query_api/requirements.lock" "$work_dir/query_api/"
cp "$repo_root/indexing_service/requirements.lock" "$work_dir/indexing_service/"

for service in query indexing; do
  if [[ "$service" == "query" ]]; then
    constraint="../../query_api/requirements.lock"
  else
    constraint="../../indexing_service/requirements.lock"
  fi
  lock_name="python-tools-${service}.lock"
  (
    cd "$work_dir/scripts/ci"
    "$uv_bin" pip compile python-tools-requirements.txt \
      --constraint "$constraint" \
      --universal \
      --python-version "$lock_python_version" \
      --generate-hashes \
      --output-file "$lock_name" >/dev/null
  )
  if ! cmp -s "$repo_root/scripts/ci/$lock_name" "$work_dir/scripts/ci/$lock_name"; then
    echo "Dependency lock is stale: scripts/ci/$lock_name" >&2
    diff -u \
      "$repo_root/scripts/ci/$lock_name" \
      "$work_dir/scripts/ci/$lock_name" || true
    status=1
  fi
done

if (( status != 0 )); then
  echo "Regenerate locks with uv $expected_uv_version and commit the exact output." >&2
  exit "$status"
fi

echo "All generated dependency locks are byte-for-byte reproducible with uv $expected_uv_version."
