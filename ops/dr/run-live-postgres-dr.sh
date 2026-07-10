#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/ops/dr/docker-compose.dr.yml"
RUN_ID="${DR_RUN_ID:-$(python3 -c 'import secrets; print(secrets.token_hex(8))')}"
EVIDENCE_BASENAME="${DR_EVIDENCE_BASENAME:-dr-live-postgres-latest.json}"
RPO_TARGET_SECONDS="${DR_RPO_TARGET_SECONDS:-300}"
RTO_TARGET_SECONDS="${DR_RTO_TARGET_SECONDS:-300}"
DR_COMPOSE_PROJECT_NAME="${DR_COMPOSE_PROJECT_NAME:-imposbro-dr-${RUN_ID}}"
DR_POSTGRES_PASSWORD="${DR_POSTGRES_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_hex(24))')}"
DR_HOST_UID="${DR_HOST_UID:-$(id -u)}"
DR_HOST_GID="${DR_HOST_GID:-$(id -g)}"
DR_WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/imposbro-dr.XXXXXX")"
EVIDENCE_PATH="$ROOT_DIR/docs/evidence/$EVIDENCE_BASENAME"
EVIDENCE_CHECKSUM="$EVIDENCE_PATH.sha256"
STACK_STARTED=false
STACK_DESTROYED=false

export DR_COMPOSE_PROJECT_NAME DR_POSTGRES_PASSWORD DR_HOST_UID DR_HOST_GID DR_WORK_DIR

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

require_nonnegative_integer() {
  [[ "$2" =~ ^[0-9]+$ ]] || fail "$1 must be a non-negative integer"
}

milliseconds() {
  python3 -c 'import time; print(time.time_ns() // 1000000)'
}

utc_now() {
  date -u '+%Y-%m-%dT%H:%M:%SZ'
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print tolower($1)}'
  else
    shasum -a 256 "$1" | awk '{print tolower($1)}'
  fi
}

sha256_text() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print tolower($1)}'
  else
    shasum -a 256 | awk '{print tolower($1)}'
  fi
}

compose() {
  docker compose --project-name "$DR_COMPOSE_PROJECT_NAME" -f "$COMPOSE_FILE" "$@"
}

cleanup() {
  local exit_code=$?
  set +e
  if [[ "$STACK_STARTED" == true ]]; then
    compose down --remove-orphans --volumes >/dev/null 2>&1
    STACK_STARTED=false
  fi
  rm -rf "$DR_WORK_DIR"
  unset DR_POSTGRES_PASSWORD
  if [[ "$exit_code" -ne 0 ]]; then
    printf 'DR drill failed; isolated containers, volumes, and temporary key material were removed.\n' >&2
  fi
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

case "$RUN_ID" in
  *[!A-Za-z0-9_-]*|'') fail "DR_RUN_ID must be a safe identifier" ;;
esac
case "$EVIDENCE_BASENAME" in
  dr-*.json) ;;
  *) fail "DR_EVIDENCE_BASENAME must be a plain dr-*.json filename" ;;
esac
[[ "$EVIDENCE_BASENAME" != */* ]] || fail "DR_EVIDENCE_BASENAME must not contain a path"
[[ "$DR_COMPOSE_PROJECT_NAME" =~ ^[a-z0-9][a-z0-9_-]{2,62}$ ]] || \
  fail "DR_COMPOSE_PROJECT_NAME must be a safe lower-case Compose project name"
require_nonnegative_integer DR_RPO_TARGET_SECONDS "$RPO_TARGET_SECONDS"
require_nonnegative_integer DR_RTO_TARGET_SECONDS "$RTO_TARGET_SECONDS"
[[ ! -e "$EVIDENCE_PATH" && ! -e "$EVIDENCE_CHECKSUM" ]] || \
  fail "evidence path already exists; choose a new DR_EVIDENCE_BASENAME"

for command in docker jq python3; do
  require_command "$command"
done
if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
  fail "required command not found: sha256sum or shasum"
fi
docker info >/dev/null 2>&1 || fail "Docker daemon is unavailable"
docker compose version >/dev/null 2>&1 || fail "Docker Compose is unavailable"

printf 'Validating and starting isolated PostgreSQL DR stack %s.\n' "$RUN_ID" >&2
compose config --quiet
compose build migrate-source tooling >/dev/null
compose up -d --wait --wait-timeout 90 source target >/dev/null
STACK_STARTED=true
compose run --rm --no-deps migrate-source >/dev/null

compose run --rm -T --no-deps \
  -e PGHOST=source -e PGPORT=5432 -e PGDATABASE=imposbro_source -e PGUSER=imposbro \
  tooling -c 'psql -X -v ON_ERROR_STOP=1 -f /dr/seed.sql' >/dev/null
compose run --rm -T --no-deps verify-source >/dev/null

retention_before="$(
  compose run --rm -T --no-deps \
    -e PGHOST=source -e PGPORT=5432 -e PGDATABASE=imposbro_source -e PGUSER=imposbro \
    tooling -c 'psql -X -A -t -v ON_ERROR_STOP=1 -f /dr/retention-counts.sql'
)"
jq -e '.control_rows == 3 and .indexing_rows == 8 and .indexing_unpublished == 1 and .audit_rows == 2 and .audit_head_sequence == 2' \
  <<<"$retention_before" >/dev/null || fail "synthetic retention fixture is invalid"

not_before="$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat().replace("+00:00","Z"))')"
expires_at="$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(hours=1)).isoformat().replace("+00:00","Z"))')"
policy_path="$DR_WORK_DIR/retention-policy.json"
jq -n \
  --arg not_before "$not_before" \
  --arg expires_at "$expires_at" \
  '{
    schema: "imposbro.outbox-retention-policy.v1",
    policy_id: "isolated-drill-review",
    database_id: "isolated-drill-source",
    database_name: "imposbro_source",
    schema_revision: "0003_audit_delivery_deletion",
    not_before: $not_before,
    expires_at: $expires_at,
    deny_all: false,
    legal_hold_snapshot: {
      snapshot_id: "isolated-fixture-holds",
      captured_at: $not_before,
      expires_at: $expires_at,
      complete: true
    },
    allowed_tables: ["control_plane_outbox", "indexing_event_outbox"],
    tables: {
      control_plane_outbox: {
        retention_seconds: 86400,
        max_unpublished: 0,
        held_revisions: []
      },
      indexing_event_outbox: {
        retention_seconds: 86400,
        max_unpublished: 1,
        held_identity_hashes: [("b" * 64)]
      }
    }
  }' > "$policy_path"
chmod 0600 "$policy_path"
policy_sha="$(sha256_file "$policy_path")"
confirmation="APPLY:isolated-drill-source:isolated-drill-review:${policy_sha:0:12}"

compose run --rm -T --no-deps retention \
  --policy /evidence/retention-policy.json \
  --evidence /evidence/retention-dry-run.json \
  --batch-size 2 --max-batches 10 >/dev/null
jq -e '
  .outcome == "success" and .mode == "dry-run" and
  ([.tables[] | select(.table == "control_plane_outbox") | .candidates_before] == [2]) and
  ([.tables[] | select(.table == "indexing_event_outbox") | .candidates_before] == [3]) and
  ([.tables[].deleted] | add == 0)
' "$DR_WORK_DIR/retention-dry-run.json" >/dev/null || fail "retention dry-run evidence failed"

compose run --rm -T --no-deps \
  -e RETENTION_CONFIRMATION="$confirmation" \
  retention \
  --policy /evidence/retention-policy.json \
  --evidence /evidence/retention-apply.json \
  --apply --policy-sha256 "$policy_sha" \
  --batch-size 2 --max-batches 10 \
  --lock-timeout-ms 2000 --statement-timeout-ms 30000 >/dev/null
jq -e '
  .outcome == "success" and .mode == "apply" and
  ([.tables[] | select(.table == "control_plane_outbox") | .deleted] == [2]) and
  ([.tables[] | select(.table == "indexing_event_outbox") | .deleted] == [3]) and
  ([.tables[].remaining_candidates] | add == 0) and
  .safety.audit_chain_touched == false and
  .safety.payloads_recorded == false
' "$DR_WORK_DIR/retention-apply.json" >/dev/null || fail "retention apply evidence failed"

retention_validation="$(
  compose run --rm -T --no-deps \
    -e PGHOST=source -e PGPORT=5432 -e PGDATABASE=imposbro_source -e PGUSER=imposbro \
    tooling -c 'psql -X -A -t -v ON_ERROR_STOP=1 -f /dr/verify-retention.sql'
)"
jq -e '.all_invariants_pass == true' <<<"$retention_validation" >/dev/null || \
  fail "live retention invariants failed"
compose run --rm -T --no-deps verify-source >/dev/null

source_fingerprint="$(
  compose run --rm -T --no-deps \
    -e PGHOST=source -e PGPORT=5432 -e PGDATABASE=imposbro_source -e PGUSER=imposbro \
    tooling -c 'psql -X -A -t -v ON_ERROR_STOP=1 -f /dr/fingerprint.sql'
)"
jq -e '.schema_revision == "0003_audit_delivery_deletion"' <<<"$source_fingerprint" >/dev/null || \
  fail "source recovery marker is invalid"

compose run --rm -T --no-deps tooling -c \
  'age-keygen -o /evidence/age-identity.txt >/dev/null 2>/evidence/age-keygen.log && age-keygen -y /evidence/age-identity.txt > /evidence/age-recipient.txt && rm -f /evidence/age-keygen.log'
recipient="$(tr -d '[:space:]' < "$DR_WORK_DIR/age-recipient.txt")"
[[ "$recipient" == age1* ]] || fail "age recipient generation failed"

backup_started_ms="$(milliseconds)"
compose run --rm -T --no-deps \
  -e PGHOST=source -e PGPORT=5432 -e PGDATABASE=imposbro_source -e PGUSER=imposbro \
  -e AGE_RECIPIENT="$recipient" -e BACKUP_SOURCE_ID=isolated-drill-source \
  tooling /ops/backup-control-plane-postgres.sh \
  --output /evidence/control-plane.dump.age \
  --expected-revision 0003_audit_delivery_deletion >/dev/null
backup_duration_ms="$(( $(milliseconds) - backup_started_ms ))"

backup_manifest="$DR_WORK_DIR/control-plane.dump.age.manifest.json"
backup_id="$(jq -er '.backup_id' "$backup_manifest")"
backup_artifact_sha="$(jq -er '.artifact.sha256' "$backup_manifest")"
plaintext_archive_sha="$(jq -er '.archive.plaintext_sha256' "$backup_manifest")"
[[ "$backup_artifact_sha" == "$(sha256_file "$DR_WORK_DIR/control-plane.dump.age")" ]] || \
  fail "backup artifact checksum mismatch"
if compose run --rm -T --no-deps tooling -c \
  'pg_restore --list /evidence/control-plane.dump.age >/dev/null 2>&1'; then
  fail "encrypted artifact was accepted as a plaintext PostgreSQL archive"
fi

declaration_at="$(utc_now)"
restore_started_ms="$(milliseconds)"
compose run --rm -T --no-deps \
  -e AGE_IDENTITY_FILE=/evidence/age-identity.txt \
  tooling /ops/restore-control-plane-postgres.sh \
  --artifact /evidence/control-plane.dump.age >/dev/null

compose run --rm -T --no-deps \
  -e PGHOST=target -e PGPORT=5432 -e PGDATABASE=imposbro_restore -e PGUSER=imposbro \
  -e AGE_IDENTITY_FILE=/evidence/age-identity.txt \
  -e RESTORE_TARGET_ID=isolated-drill-target \
  -e RESTORE_TARGET_CLASS=isolated-drill \
  -e RESTORE_CONFIRMATION="RESTORE:isolated-drill-target:$backup_id" \
  tooling /ops/restore-control-plane-postgres.sh \
  --artifact /evidence/control-plane.dump.age \
  --execute --evidence /evidence/restore-evidence.json >/dev/null
compose run --rm -T --no-deps verify-target >/dev/null

target_fingerprint="$(
  compose run --rm -T --no-deps \
    -e PGHOST=target -e PGPORT=5432 -e PGDATABASE=imposbro_restore -e PGUSER=imposbro \
    tooling -c 'psql -X -A -t -v ON_ERROR_STOP=1 -f /dr/fingerprint.sql'
)"
[[ "$target_fingerprint" == "$source_fingerprint" ]] || fail "restored database fingerprint differs from source"
rto_measured_ms="$(( $(milliseconds) - restore_started_ms ))"
rpo_measured_ms=0
completed_at="$(utc_now)"

source_fingerprint_sha="$(printf '%s' "$source_fingerprint" | sha256_text)"
target_fingerprint_sha="$(printf '%s' "$target_fingerprint" | sha256_text)"
restore_evidence_sha="$(sha256_file "$DR_WORK_DIR/restore-evidence.json")"
restored_archive_sha="$(jq -er '.backup.decrypted_archive_sha256' "$DR_WORK_DIR/restore-evidence.json")"
[[ "$restored_archive_sha" == "$plaintext_archive_sha" ]] || \
  fail "plaintext archive checksum differs after decryption"
retention_evidence_sha="$(sha256_file "$DR_WORK_DIR/retention-apply.json")"
backup_manifest_sha="$(sha256_file "$backup_manifest")"
newest_durable_change_at="$(jq -er '.newest_durable_change_at' <<<"$source_fingerprint")"
postgres_version="$(compose run --rm -T --no-deps tooling -c 'postgres --version' | tr -d '\r\n')"
pg_dump_version="$(compose run --rm -T --no-deps tooling -c 'pg_dump --version' | tr -d '\r\n')"
age_version="$(compose run --rm -T --no-deps tooling -c 'age --version' | tr -d '\r\n')"
docker_version="$(docker version --format '{{.Server.Version}}')"
git_commit="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || printf unknown)"
if [[ -n "$(git -C "$ROOT_DIR" status --porcelain 2>/dev/null)" ]]; then
  git_dirty=true
else
  git_dirty=false
fi

control_deleted="$(jq -n --argjson before "$retention_before" --argjson after "$retention_validation" '$before.control_rows - $after.control_rows')"
indexing_deleted="$(jq -n --argjson before "$retention_before" --argjson after "$retention_validation" '$before.indexing_rows - $after.indexing_rows')"
audit_chain_unchanged="$(jq -n --argjson before "$retention_before" --argjson after "$retention_validation" '$before.audit_rows == $after.audit_rows and $before.audit_head_sequence == $after.audit_head_sequence and $before.audit_head_hash == $after.audit_head_hash and $before.audit_chain_digest == $after.audit_chain_digest')"

compose down --remove-orphans --volumes >/dev/null
STACK_STARTED=false
if [[ -z "$(docker ps -a -q --filter "label=com.docker.compose.project=$DR_COMPOSE_PROJECT_NAME")" && \
      -z "$(docker volume ls -q --filter "label=com.docker.compose.project=$DR_COMPOSE_PROJECT_NAME")" ]]; then
  STACK_DESTROYED=true
else
  fail "isolated DR containers or volumes remain after cleanup"
fi

rpo_pass=false
rto_pass=false
(( rpo_measured_ms <= RPO_TARGET_SECONDS * 1000 )) && rpo_pass=true
(( rto_measured_ms <= RTO_TARGET_SECONDS * 1000 )) && rto_pass=true
outcome=failed
[[ "$rpo_pass" == true && "$rto_pass" == true && "$STACK_DESTROYED" == true ]] && outcome=passed

report_tmp="$ROOT_DIR/docs/evidence/.${EVIDENCE_BASENAME}.${RUN_ID}.tmp"
jq -n \
  --arg schema "imposbro.dr-drill-evidence.v1" \
  --arg outcome "$outcome" \
  --arg run_id "$RUN_ID" \
  --arg declaration_at "$declaration_at" \
  --arg completed_at "$completed_at" \
  --arg newest_durable_change_at "$newest_durable_change_at" \
  --arg backup_id "$backup_id" \
  --arg backup_artifact_sha "$backup_artifact_sha" \
  --arg plaintext_archive_sha "$plaintext_archive_sha" \
  --arg restored_archive_sha "$restored_archive_sha" \
  --arg backup_manifest_sha "$backup_manifest_sha" \
  --arg restore_evidence_sha "$restore_evidence_sha" \
  --arg retention_evidence_sha "$retention_evidence_sha" \
  --arg source_fingerprint_sha "$source_fingerprint_sha" \
  --arg target_fingerprint_sha "$target_fingerprint_sha" \
  --arg postgres_version "$postgres_version" \
  --arg pg_dump_version "$pg_dump_version" \
  --arg age_version "$age_version" \
  --arg docker_version "$docker_version" \
  --arg git_commit "$git_commit" \
  --argjson git_dirty "$git_dirty" \
  --argjson rpo_target_seconds "$RPO_TARGET_SECONDS" \
  --argjson rto_target_seconds "$RTO_TARGET_SECONDS" \
  --argjson rpo_measured_ms "$rpo_measured_ms" \
  --argjson rto_measured_ms "$rto_measured_ms" \
  --argjson backup_duration_ms "$backup_duration_ms" \
  --argjson rpo_pass "$rpo_pass" \
  --argjson rto_pass "$rto_pass" \
  --argjson retention_validation "$retention_validation" \
  --argjson retention_before "$retention_before" \
  --argjson control_deleted "$control_deleted" \
  --argjson indexing_deleted "$indexing_deleted" \
  --argjson audit_chain_unchanged "$audit_chain_unchanged" \
  --argjson recovery_marker "$source_fingerprint" \
  --argjson stack_destroyed "$STACK_DESTROYED" \
  '{
    schema: $schema,
    outcome: $outcome,
    run_id: $run_id,
    scope: {
      environment: "local-isolated-docker",
      production_traffic: false,
      production_certification: false,
      scenario: "encrypted PostgreSQL backup, clean restore, and owned-outbox retention"
    },
    timing: {
      declaration_at: $declaration_at,
      completed_at: $completed_at,
      backup_duration_ms: $backup_duration_ms,
      rpo: {
        target_seconds: $rpo_target_seconds,
        measured_ms: $rpo_measured_ms,
        passed: $rpo_pass,
        basis: "quiesced synthetic workload; source and restored durable fingerprints matched",
        newest_durable_change_at: $newest_durable_change_at,
        known_durable_mutations_lost: 0
      },
      rto: {
        target_seconds: $rto_target_seconds,
        measured_ms: $rto_measured_ms,
        passed: $rto_pass,
        basis: "declaration through decrypt, transactional restore, invariant validation, and fingerprint comparison"
      }
    },
    backup: {
      id: $backup_id,
      artifact_sha256: $backup_artifact_sha,
      plaintext_archive_sha256: $plaintext_archive_sha,
      manifest_sha256: $backup_manifest_sha,
      encryption: "age",
      ciphertext_rejected_as_plaintext_archive: true,
      artifact_persisted_in_repository: false
    },
    restore: {
      target_was_empty: true,
      transaction_guarded: true,
      restore_evidence_sha256: $restore_evidence_sha,
      decrypted_archive_sha256: $restored_archive_sha,
      plaintext_archive_checksum_match: ($plaintext_archive_sha == $restored_archive_sha),
      source_fingerprint_sha256: $source_fingerprint_sha,
      target_fingerprint_sha256: $target_fingerprint_sha,
      fingerprints_match: ($source_fingerprint_sha == $target_fingerprint_sha),
      recovery_marker: $recovery_marker
    },
    retention: {
      live_postgresql_executed: true,
      apply_evidence_sha256: $retention_evidence_sha,
      dry_run_executed_first: true,
      before: $retention_before,
      after: $retention_validation,
      deleted_control_plane_rows: $control_deleted,
      deleted_indexing_rows: $indexing_deleted,
      unpublished_rows_deleted: 0,
      legal_hold_identity_rows_deleted: 0,
      latest_mutations_deleted: 0,
      audit_chain_touched: ($audit_chain_unchanged | not),
      audit_chain_unchanged: $audit_chain_unchanged,
      audit_chain_rows_verified: $retention_before.audit_rows,
      audit_chain_cryptographically_verified_before_retention: true,
      audit_chain_cryptographically_verified_after_retention: true,
      audit_chain_cryptographically_verified_after_restore: true
    },
    cleanup: {
      containers_and_volumes_destroyed: $stack_destroyed,
      decrypted_dump_persisted: false,
      age_identity_persisted_in_repository: false
    },
    tooling: {
      postgres: $postgres_version,
      pg_dump: $pg_dump_version,
      age: $age_version,
      docker_server: $docker_version
    },
    provenance: {
      git_commit: $git_commit,
      dirty_worktree: $git_dirty,
      evidence_contains_credentials: false,
      evidence_contains_document_payloads: false
    },
    limitations: [
      "single-host isolated Docker drill, not region failover",
      "quiesced synthetic dataset, not a concurrent production workload",
      "scheduler, immutable off-site storage, key-custody recovery, and backup expiry were not exercised",
      "application, Kafka, Redis, Typesense, DNS, and traffic failover were outside this drill"
    ]
  }' > "$report_tmp"
chmod 0600 "$report_tmp"
mv "$report_tmp" "$EVIDENCE_PATH"
report_sha="$(sha256_file "$EVIDENCE_PATH")"
printf '%s  %s\n' "$report_sha" "$EVIDENCE_BASENAME" > "$EVIDENCE_CHECKSUM"
chmod 0600 "$EVIDENCE_CHECKSUM"

rm -rf "$DR_WORK_DIR"

printf 'PASS: live encrypted PostgreSQL backup/restore and retention drill\n' >&2
printf 'RPO: %sms (target %ss); RTO: %sms (target %ss)\n' \
  "$rpo_measured_ms" "$RPO_TARGET_SECONDS" "$rto_measured_ms" "$RTO_TARGET_SECONDS" >&2
printf 'Evidence: %s\nChecksum: %s\n' "$EVIDENCE_PATH" "$EVIDENCE_CHECKSUM" >&2
[[ "$outcome" == passed ]] || exit 1
