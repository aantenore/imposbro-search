#!/usr/bin/env bash

# Shared helpers for operational scripts. This file is sourced, not executed.

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

log() {
  printf '%s\n' "$*" >&2
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

require_nonempty() {
  local name="$1"
  local value="$2"
  [[ -n "$value" ]] || die "$name must be set"
}

require_safe_identifier() {
  local name="$1"
  local value="$2"
  [[ "$value" =~ ^[A-Za-z0-9._:-]{1,128}$ ]] || \
    die "$name must contain only 1-128 letters, digits, dot, underscore, colon, or hyphen characters"
}

require_safe_database_name() {
  local value="$1"
  [[ "$value" =~ ^[A-Za-z_][A-Za-z0-9_.-]{0,62}$ ]] || \
    die "PGDATABASE must be a 1-63 character operational database name without whitespace or control characters"
}

utc_now() {
  date -u '+%Y-%m-%dT%H:%M:%SZ'
}

trim_whitespace() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

sha256_file() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print tolower($1)}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$path" | awk '{print tolower($1)}'
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 -r "$path" | awk '{print tolower($1)}'
  else
    die "one of sha256sum, shasum, or openssl is required"
  fi
}

file_size_bytes() {
  wc -c < "$1" | tr -d '[:space:]'
}

new_backup_id() {
  if command -v uuidgen >/dev/null 2>&1; then
    uuidgen | tr '[:upper:]' '[:lower:]'
  elif command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 16
  else
    od -An -N16 -tx1 /dev/urandom | tr -d ' \n'
  fi
}

reject_connection_string_database() {
  local value="$1"
  case "$value" in
    *://*|*=*)
      die "PGDATABASE must be a database name; pass connection fields through libpq PG* environment variables, never a command-line URI"
      ;;
  esac
  require_safe_database_name "$value"
}

require_regular_not_symlink() {
  local path="$1"
  [[ -f "$path" ]] || die "file not found: $path"
  [[ ! -L "$path" ]] || die "symbolic links are not accepted for protected artifacts: $path"
}
