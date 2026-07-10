#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() {
  cat <<'USAGE'
Usage:
  restore-control-plane-postgres.sh --artifact PATH.age [options]

Default behavior is verification-only: verify both checksums, decrypt to an
ephemeral file, and run pg_restore --list. No database is contacted.

Required:
  --artifact PATH.age            Encrypted backup artifact.
  --identity PATH                age identity file (or AGE_IDENTITY_FILE).

Options:
  --manifest PATH                Default: PATH.age.manifest.json.
  --checksum PATH                Default: PATH.age.sha256.
  --execute                      Restore into an explicitly empty target DB.
  --evidence PATH.json           Mandatory with --execute; must not exist.
  --allow-production-target      Additional production-only guard.
  -h, --help                     Show this help.

Execution additionally requires PGDATABASE (name only), RESTORE_TARGET_ID,
RESTORE_TARGET_CLASS, and exact confirmation:
  RESTORE_CONFIRMATION=RESTORE:<target-id>:<backup-id>

RESTORE_TARGET_CLASS is one of isolated-drill, staging, or production. A
production restore also requires --allow-production-target,
RESTORE_CHANGE_TICKET, and RESTORE_APPROVED_BY. Every target must contain zero
non-system relations; this script never drops or truncates an existing object.
USAGE
}

artifact=""
identity="${AGE_IDENTITY_FILE:-}"
manifest=""
checksum=""
evidence=""
execute_restore=0
allow_production=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --artifact)
      [[ $# -ge 2 ]] || die "--artifact requires a value"
      artifact="$2"
      shift 2
      ;;
    --identity)
      [[ $# -ge 2 ]] || die "--identity requires a value"
      identity="$2"
      shift 2
      ;;
    --manifest)
      [[ $# -ge 2 ]] || die "--manifest requires a value"
      manifest="$2"
      shift 2
      ;;
    --checksum)
      [[ $# -ge 2 ]] || die "--checksum requires a value"
      checksum="$2"
      shift 2
      ;;
    --evidence)
      [[ $# -ge 2 ]] || die "--evidence requires a value"
      evidence="$2"
      shift 2
      ;;
    --execute)
      execute_restore=1
      shift
      ;;
    --allow-production-target)
      allow_production=1
      shift
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

require_nonempty "--artifact" "$artifact"
require_nonempty "age identity" "$identity"
manifest="${manifest:-$artifact.manifest.json}"
checksum="${checksum:-$artifact.sha256}"

require_regular_not_symlink "$artifact"
require_regular_not_symlink "$manifest"
require_regular_not_symlink "$checksum"
[[ -f "$identity" && -r "$identity" ]] || die "age identity is not a readable regular file: $identity"
require_command age
require_command pg_restore
require_command jq

expected_sidecar_sha="$(awk 'NR == 1 {print tolower($1)}' "$checksum")"
expected_sidecar_file="$(awk 'NR == 1 {print $2}' "$checksum")"
[[ "$expected_sidecar_sha" =~ ^[0-9a-f]{64}$ ]] || die "checksum sidecar does not contain a SHA-256 digest"
[[ "$expected_sidecar_file" == "$(basename "$artifact")" ]] || die "checksum sidecar filename does not match --artifact"
actual_sha="$(sha256_file "$artifact")"
[[ "$actual_sha" == "$expected_sidecar_sha" ]] || die "artifact SHA-256 does not match checksum sidecar"

jq -e '
  .schema == "imposbro.postgres-backup.v1" and
  (.backup_id | type == "string" and length > 0) and
  (.created_at | type == "string" and length > 0) and
  (.source.id | type == "string" and length > 0) and
  (.source.database | type == "string" and length > 0) and
  (.database_schema_revision | type == "string" and length > 0) and
  (.retention_class | type == "string" and length > 0) and
  .encryption.format == "age" and
  ((.archive.plaintext_sha256 // "") | test("^$|^[0-9a-f]{64}$")) and
  .contains_credentials == false and
  (.artifact.file | type == "string" and length > 0) and
  (.artifact.sha256 | test("^[0-9a-f]{64}$")) and
  (.artifact.size_bytes | type == "number" and . > 0)
' "$manifest" >/dev/null || die "manifest schema or required fields are invalid"

manifest_file="$(jq -r '.artifact.file' "$manifest")"
manifest_sha="$(jq -r '.artifact.sha256 | ascii_downcase' "$manifest")"
manifest_size="$(jq -r '.artifact.size_bytes' "$manifest")"
backup_id="$(jq -r '.backup_id' "$manifest")"
source_id="$(jq -r '.source.id' "$manifest")"
schema_revision="$(jq -r '.database_schema_revision' "$manifest")"
manifest_plaintext_sha="$(jq -r '.archive.plaintext_sha256 // empty | ascii_downcase' "$manifest")"

require_safe_identifier "manifest backup id" "$backup_id"
require_safe_identifier "manifest source id" "$source_id"
[[ "$manifest_file" == "$(basename "$artifact")" ]] || die "manifest artifact filename does not match --artifact"
[[ "$manifest_sha" == "$actual_sha" ]] || die "artifact SHA-256 does not match manifest"
[[ "$manifest_size" == "$(file_size_bytes "$artifact")" ]] || die "artifact size does not match manifest"

stage_dir="$(mktemp -d "${TMPDIR:-/tmp}/imposbro-restore.XXXXXX")"
plain_dump="$stage_dir/control-plane.dump"
toc_file="$stage_dir/archive.toc"
evidence_tmp=""
cleanup() {
  rm -f "$plain_dump" "$toc_file"
  [[ -z "$evidence_tmp" ]] || rm -f "$evidence_tmp"
  rmdir "$stage_dir" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

log "Checksum and manifest verified for backup '$backup_id' from '$source_id'."
age --decrypt --identity "$identity" --output "$plain_dump" "$artifact"
[[ -s "$plain_dump" ]] || die "decryption produced an empty artifact"
decrypted_archive_sha="$(sha256_file "$plain_dump")"
plaintext_checksum_verified=false
if [[ -n "$manifest_plaintext_sha" && "$decrypted_archive_sha" != "$manifest_plaintext_sha" ]]; then
  die "decrypted archive SHA-256 does not match backup manifest"
fi
if [[ -n "$manifest_plaintext_sha" ]]; then
  plaintext_checksum_verified=true
fi
pg_restore --list "$plain_dump" > "$toc_file"
[[ -s "$toc_file" ]] || die "pg_restore could not list archive contents"

if [[ "$execute_restore" -ne 1 ]]; then
  log "Verification-only completed; no database was contacted."
  exit 0
fi

require_nonempty "--evidence" "$evidence"
[[ "$evidence" == *.json ]] || die "--evidence must end in .json"
[[ ! -e "$evidence" ]] || die "evidence path already exists; refusing to overwrite"
[[ -d "$(dirname "$evidence")" ]] || die "evidence output directory does not exist"
require_nonempty "PGDATABASE" "${PGDATABASE:-}"
require_nonempty "RESTORE_TARGET_ID" "${RESTORE_TARGET_ID:-}"
require_nonempty "RESTORE_TARGET_CLASS" "${RESTORE_TARGET_CLASS:-}"
require_safe_identifier "RESTORE_TARGET_ID" "$RESTORE_TARGET_ID"
reject_connection_string_database "$PGDATABASE"
require_command psql

case "$PGDATABASE" in
  postgres|template0|template1)
    die "refusing to restore into reserved database '$PGDATABASE'"
    ;;
esac

case "$RESTORE_TARGET_CLASS" in
  isolated-drill|staging)
    ;;
  production)
    [[ "$allow_production" -eq 1 ]] || die "production restore requires --allow-production-target"
    require_nonempty "RESTORE_CHANGE_TICKET" "${RESTORE_CHANGE_TICKET:-}"
    require_nonempty "RESTORE_APPROVED_BY" "${RESTORE_APPROVED_BY:-}"
    ;;
  *)
    die "RESTORE_TARGET_CLASS must be isolated-drill, staging, or production"
    ;;
esac

expected_confirmation="RESTORE:${RESTORE_TARGET_ID}:${backup_id}"
[[ "${RESTORE_CONFIRMATION:-}" == "$expected_confirmation" ]] || \
  die "confirmation mismatch; expected RESTORE:<target-id>:<backup-id>"

existing_relations="$(
  psql -X -A -t -v ON_ERROR_STOP=1 -c '
    /* IMPOSBRO_EMPTY_TARGET_CHECK */
    SELECT count(*)
    FROM pg_catalog.pg_class AS c
    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname NOT IN ('"'"'pg_catalog'"'"', '"'"'information_schema'"'"')
      AND n.nspname !~ '"'"'^pg_toast'"'"'
      AND c.relkind IN ('"'"'r'"'"', '"'"'p'"'"', '"'"'v'"'"', '"'"'m'"'"', '"'"'S'"'"', '"'"'f'"'"');
  '
)"
existing_relations="$(trim_whitespace "$existing_relations")"
[[ "$existing_relations" =~ ^[0-9]+$ ]] || die "empty-target check returned an invalid value"
[[ "$existing_relations" == "0" ]] || die "target database is not empty ($existing_relations non-system relations); no changes made"

started_at="$(utc_now)"
log "Restoring backup '$backup_id' into empty target '$RESTORE_TARGET_ID'."
pg_restore \
  --exit-on-error \
  --single-transaction \
  --no-owner \
  --no-privileges \
  --dbname="$PGDATABASE" \
  "$plain_dump"

restored_revision="$(
  psql -X -A -t -v ON_ERROR_STOP=1 \
    -c 'SELECT version_num FROM alembic_version LIMIT 1;'
)"
restored_revision="$(trim_whitespace "$restored_revision")"
[[ "$restored_revision" == "$schema_revision" ]] || \
  die "restored Alembic revision '$restored_revision' does not match manifest '$schema_revision'"

required_tables="$(
  psql -X -A -t -v ON_ERROR_STOP=1 -c '
    /* IMPOSBRO_REQUIRED_TABLES_CHECK */
    SELECT count(*)
    FROM information_schema.tables
    WHERE table_schema = '"'"'public'"'"'
      AND table_name IN (
        '"'"'control_plane_state'"'"',
        '"'"'control_plane_audit_head'"'"',
        '"'"'control_plane_audit'"'"',
        '"'"'control_plane_outbox'"'"',
        '"'"'indexing_event_heads'"'"',
        '"'"'indexing_event_outbox'"'"',
        '"'"'indexing_checkpoints'"'"',
        '"'"'audit_delivery_checkpoints'"'"',
        '"'"'deletion_ledger'"'"',
        '"'"'deletion_ledger_targets'"'"'
      );
  '
)"
required_tables="$(trim_whitespace "$required_tables")"
[[ "$required_tables" == "10" ]] || die "restore validation found $required_tables of 10 required tables"

state_rows="$(
  psql -X -A -t -v ON_ERROR_STOP=1 \
    -c "/* IMPOSBRO_STATE_ROWS_CHECK */ SELECT count(*) FROM control_plane_state WHERE id = 'current' AND revision >= 1 AND schema_version >= 1 AND length(state_digest) = 64;"
)"
state_rows="$(trim_whitespace "$state_rows")"
[[ "$state_rows" == "1" ]] || die "control_plane_state singleton or digest invariant failed"

audit_head_valid="$(
  psql -X -A -t -v ON_ERROR_STOP=1 \
    -c "/* IMPOSBRO_AUDIT_HEAD_CHECK */ SELECT count(*) FROM control_plane_audit_head WHERE id = 'head' AND sequence >= 0 AND length(event_hash) = 64;"
)"
audit_head_valid="$(trim_whitespace "$audit_head_valid")"
[[ "$audit_head_valid" == "1" ]] || die "control_plane_audit_head invariant failed"

completed_at="$(utc_now)"
change_ticket="${RESTORE_CHANGE_TICKET:-not-required}"
approved_by="${RESTORE_APPROVED_BY:-not-required}"
evidence_tmp="$(mktemp "$(dirname "$evidence")/.imposbro-restore-evidence.XXXXXX")"
jq -n \
  --arg schema "imposbro.restore-evidence.v1" \
  --arg backup_id "$backup_id" \
  --arg source_id "$source_id" \
  --arg artifact_sha256 "$actual_sha" \
  --arg decrypted_archive_sha256 "$decrypted_archive_sha" \
  --arg target_id "$RESTORE_TARGET_ID" \
  --arg target_class "$RESTORE_TARGET_CLASS" \
  --arg database "$PGDATABASE" \
  --arg schema_revision "$restored_revision" \
  --arg started_at "$started_at" \
  --arg completed_at "$completed_at" \
  --arg change_ticket "$change_ticket" \
  --arg approved_by "$approved_by" \
  --argjson state_rows "$state_rows" \
  --argjson audit_head_valid "$audit_head_valid" \
  --argjson plaintext_checksum_verified "$plaintext_checksum_verified" \
  '{
    schema: $schema,
    outcome: "success",
    backup: {id: $backup_id, source_id: $source_id, artifact_sha256: $artifact_sha256, decrypted_archive_sha256: $decrypted_archive_sha256},
    target: {id: $target_id, class: $target_class, database: $database},
    validation: {archive_listed: true, encrypted_to_plaintext_checksum_match: $plaintext_checksum_verified, empty_target_precondition: true, required_tables: 10, state_rows: $state_rows, audit_head_valid: $audit_head_valid, schema_revision: $schema_revision},
    timing: {started_at: $started_at, completed_at: $completed_at},
    approval: {change_ticket: $change_ticket, approved_by: $approved_by},
    live_restore_executed: true
  }' > "$evidence_tmp"
chmod 0600 "$evidence_tmp"
ln "$evidence_tmp" "$evidence"
rm -f "$evidence_tmp"
evidence_tmp=""
log "Restore and post-restore validation completed. Evidence: $evidence"
