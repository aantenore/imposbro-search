#!/usr/bin/env bash
set -euo pipefail

release_tag="${1:-}"
if [[ ! "$release_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([+-][0-9A-Za-z.-]+)?$ ]]; then
  echo "Release tag must be SemVer prefixed with v (for example v1.2.3 or v1.2.3-rc.1)." >&2
  exit 1
fi

printf '%s\n' "$release_tag"
