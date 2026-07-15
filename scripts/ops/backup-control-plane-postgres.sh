#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() {
  cat <<'USAGE'
Usage:
  backup-control-plane-postgres.sh --output PATH.age [options]

Required configuration:
  --output PATH.age              Encrypted artifact path; it must not exist.
  --recipient AGE_RECIPIENT      age recipient (or AGE_RECIPIENT environment).
  --source-id ID                 Stable environment/source identifier
                                 (or BACKUP_SOURCE_ID environment).
  PGDATABASE                     Database name only. Use libpq PGHOST, PGPORT,
                                 PGUSER, PGPASSWORD/PGPASSFILE for connectivity.

Options:
  --retention-class CLASS        Policy class recorded in the manifest
                                 (default: daily-35d).
  --expected-revision REVISION   Fail unless alembic_version equals this value
                                 (or BACKUP_EXPECTED_ALEMBIC_REVISION).
  -h, --help                     Show this help.

Produces PATH.age, PATH.age.sha256, and PATH.age.manifest.json with mode 0600.
The artifact is a PostgreSQL custom-format dump encrypted with age. Credentials
are never accepted on the command line and are not included in the manifest.
USAGE
}

output=""
recipient="${AGE_RECIPIENT:-}"
source_id="${BACKUP_SOURCE_ID:-}"
retention_class="${BACKUP_RETENTION_CLASS:-daily-35d}"
expected_revision="${BACKUP_EXPECTED_ALEMBIC_REVISION:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)
      [[ $# -ge 2 ]] || die "--output requires a value"
      output="$2"
      shift 2
      ;;
    --recipient)
      [[ $# -ge 2 ]] || die "--recipient requires a value"
      recipient="$2"
      shift 2
      ;;
    --source-id)
      [[ $# -ge 2 ]] || die "--source-id requires a value"
      source_id="$2"
      shift 2
      ;;
    --retention-class)
      [[ $# -ge 2 ]] || die "--retention-class requires a value"
      retention_class="$2"
      shift 2
      ;;
    --expected-revision)
      [[ $# -ge 2 ]] || die "--expected-revision requires a value"
      expected_revision="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

require_nonempty "--output" "$output"
require_nonempty "age recipient" "$recipient"
require_nonempty "backup source id" "$source_id"
require_nonempty "PGDATABASE" "${PGDATABASE:-}"
require_safe_identifier "backup source id" "$source_id"
require_safe_identifier "retention class" "$retention_class"
reject_connection_string_database "$PGDATABASE"
[[ "$output" == *.age ]] || die "--output must end in .age"

require_command pg_dump
require_command psql
require_command age
require_command jq

output_dir="$(dirname "$output")"
artifact_name="$(basename "$output")"
[[ "$artifact_name" =~ ^[A-Za-z0-9._-]+$ ]] || \
  die "backup artifact filename may contain only letters, digits, dot, underscore, and hyphen"
checksum_path="$output.sha256"
manifest_path="$output.manifest.json"
lock_dir="$output.lock"
[[ -d "$output_dir" ]] || die "output directory does not exist: $output_dir"
[[ ! -e "$output" && ! -e "$checksum_path" && ! -e "$manifest_path" ]] || \
  die "backup output or sidecar already exists; refusing to overwrite"

stage_dir=""
artifact_published=0
checksum_published=0
manifest_published=0
completed=0
mkdir "$lock_dir" 2>/dev/null || die "backup lock already exists: $lock_dir"

cleanup() {
  if [[ -n "$stage_dir" ]]; then
    rm -f \
      "$stage_dir/control-plane.dump" \
      "$stage_dir/$artifact_name" \
      "$stage_dir/$artifact_name.sha256" \
      "$stage_dir/$artifact_name.manifest.json"
    rmdir "$stage_dir" 2>/dev/null || true
  fi
  if [[ "$completed" -ne 1 ]]; then
    [[ "$artifact_published" -eq 0 ]] || rm -f "$output"
    [[ "$checksum_published" -eq 0 ]] || rm -f "$checksum_path"
    [[ "$manifest_published" -eq 0 ]] || rm -f "$manifest_path"
  fi
  rmdir "$lock_dir" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

stage_dir="$(mktemp -d "$output_dir/.imposbro-backup.XXXXXX")"
plain_dump="$stage_dir/control-plane.dump"
encrypted_stage="$stage_dir/$artifact_name"
checksum_stage="$stage_dir/$artifact_name.sha256"
manifest_stage="$stage_dir/$artifact_name.manifest.json"

[[ ! -e "$output" && ! -e "$checksum_path" && ! -e "$manifest_path" ]] || \
  die "backup output or sidecar appeared after lock acquisition; refusing to overwrite"

schema_revision="$(
  psql -X -A -t -v ON_ERROR_STOP=1 \
    -c 'SELECT version_num FROM alembic_version LIMIT 1;'
)"
schema_revision="$(trim_whitespace "$schema_revision")"
require_nonempty "database alembic revision" "$schema_revision"
if [[ -n "$expected_revision" && "$schema_revision" != "$expected_revision" ]]; then
  die "database revision '$schema_revision' does not match expected revision '$expected_revision'"
fi

created_at="$(utc_now)"
backup_id="$(new_backup_id)"
pg_dump_version="$(pg_dump --version | head -n 1)"

log "Creating PostgreSQL custom-format dump for source '$source_id'."
pg_dump \
  --format=custom \
  --no-owner \
  --no-acl \
  --file="$plain_dump" \
  --dbname="$PGDATABASE"

[[ -s "$plain_dump" ]] || die "pg_dump produced an empty artifact"
plaintext_archive_sha256="$(sha256_file "$plain_dump")"
log "Encrypting backup with age."
age --recipient "$recipient" --output "$encrypted_stage" "$plain_dump"
rm -f "$plain_dump"
[[ -s "$encrypted_stage" ]] || die "age produced an empty artifact"

artifact_sha256="$(sha256_file "$encrypted_stage")"
artifact_size="$(file_size_bytes "$encrypted_stage")"
printf '%s  %s\n' "$artifact_sha256" "$artifact_name" > "$checksum_stage"

jq -n \
  --arg schema "imposbro.postgres-backup.v1" \
  --arg backup_id "$backup_id" \
  --arg created_at "$created_at" \
  --arg source_id "$source_id" \
  --arg database "$PGDATABASE" \
  --arg schema_revision "$schema_revision" \
  --arg retention_class "$retention_class" \
  --arg encryption "age" \
  --arg artifact "$artifact_name" \
  --arg sha256 "$artifact_sha256" \
  --arg plaintext_archive_sha256 "$plaintext_archive_sha256" \
  --argjson size_bytes "$artifact_size" \
  --arg pg_dump_version "$pg_dump_version" \
  '{
    schema: $schema,
    backup_id: $backup_id,
    created_at: $created_at,
    source: {id: $source_id, database: $database},
    database_schema_revision: $schema_revision,
    retention_class: $retention_class,
    encryption: {format: $encryption},
    archive: {format: "postgresql-custom", plaintext_sha256: $plaintext_archive_sha256},
    artifact: {file: $artifact, sha256: $sha256, size_bytes: $size_bytes},
    tooling: {pg_dump: $pg_dump_version},
    contains_credentials: false
  }' > "$manifest_stage"

chmod 0600 "$encrypted_stage" "$checksum_stage" "$manifest_stage"
ln "$encrypted_stage" "$output"
artifact_published=1
rm -f "$encrypted_stage"
ln "$checksum_stage" "$checksum_path"
checksum_published=1
rm -f "$checksum_stage"
ln "$manifest_stage" "$manifest_path"
manifest_published=1
rm -f "$manifest_stage"
completed=1

log "Backup completed: $output"
log "Manifest: $manifest_path"
log "Checksum: $checksum_path"
