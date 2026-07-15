#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SCRIPT="$SCRIPT_DIR/backup-control-plane-postgres.sh"
RESTORE_SCRIPT="$SCRIPT_DIR/restore-control-plane-postgres.sh"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

expect_failure() {
  local label="$1"
  shift
  if "$@" >"$test_root/$label.stdout" 2>"$test_root/$label.stderr"; then
    fail "$label unexpectedly succeeded"
  fi
}

assert_no_restore_call() {
  if grep -q '^RESTORE ' "$call_log"; then
    fail "a database restore occurred before all guardrails passed"
  fi
}

assert_no_database_call() {
  if grep -Eq '^(PSQL|RESTORE) ' "$call_log"; then
    fail "verification-only mode contacted the database"
  fi
}

test_root="$(mktemp -d "${TMPDIR:-/tmp}/imposbro-ops-tests.XXXXXX")"
fake_bin="$test_root/bin"
call_log="$test_root/calls.log"
mkdir -p "$fake_bin" "$test_root/output"
: > "$call_log"
cleanup() {
  rm -rf "$test_root"
}
trap cleanup EXIT INT TERM

cat > "$fake_bin/pg_dump" <<'FAKE'
#!/bin/sh
if [ "${1:-}" = "--version" ]; then
  printf '%s\n' 'pg_dump (PostgreSQL) 17.5-fake'
  exit 0
fi
output=''
for argument in "$@"; do
  case "$argument" in
    --file=*) output=${argument#--file=} ;;
  esac
done
[ -n "$output" ] || exit 20
printf '%s\n' 'FAKE POSTGRESQL CUSTOM ARCHIVE' > "$output"
FAKE

cat > "$fake_bin/psql" <<'FAKE'
#!/bin/sh
arguments=$*
[ -z "${FAKE_CALL_LOG:-}" ] || printf 'PSQL %s\n' "$arguments" >> "$FAKE_CALL_LOG"
case "$arguments" in
  *IMPOSBRO_EMPTY_TARGET_CHECK*) printf '%s\n' "${FAKE_EXISTING_RELATIONS:-0}" ;;
  *IMPOSBRO_REQUIRED_TABLES_CHECK*) printf '%s\n' '10' ;;
  *IMPOSBRO_STATE_ROWS_CHECK*) printf '%s\n' '1' ;;
  *IMPOSBRO_AUDIT_HEAD_CHECK*) printf '%s\n' '1' ;;
  *version_num*) printf '%s\n' '0003_audit_delivery_deletion' ;;
  *) printf 'unexpected fake psql invocation: %s\n' "$arguments" >&2; exit 21 ;;
esac
FAKE

cat > "$fake_bin/age" <<'FAKE'
#!/bin/sh
output=''
input=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --recipient|--identity) shift 2 ;;
    --output) output=$2; shift 2 ;;
    --decrypt) shift ;;
    *) input=$1; shift ;;
  esac
done
[ -n "$output" ] && [ -n "$input" ] || exit 22
cp "$input" "$output"
FAKE

cat > "$fake_bin/pg_restore" <<'FAKE'
#!/bin/sh
list=0
for argument in "$@"; do
  [ "$argument" = "--list" ] && list=1
done
if [ "$list" -eq 1 ]; then
  printf '%s\n' '; fake archive table of contents'
  exit 0
fi
printf 'RESTORE %s\n' "$*" >> "$FAKE_CALL_LOG"
FAKE

chmod 0700 "$fake_bin/pg_dump" "$fake_bin/psql" "$fake_bin/age" "$fake_bin/pg_restore"
identity="$test_root/age-identity.txt"
printf '%s\n' 'not-a-real-age-identity' > "$identity"

artifact="$test_root/output/control-plane.dump.age"
env \
  PATH="$fake_bin:$PATH" \
  PGDATABASE="imposbro_source" \
  AGE_RECIPIENT="age1fake" \
  BACKUP_SOURCE_ID="guardrail-test" \
  "$BACKUP_SCRIPT" --output "$artifact" --expected-revision 0003_audit_delivery_deletion \
  >"$test_root/backup.stdout" 2>"$test_root/backup.stderr"

[[ -s "$artifact" ]] || fail "encrypted artifact was not created"
[[ -s "$artifact.sha256" ]] || fail "checksum sidecar was not created"
[[ -s "$artifact.manifest.json" ]] || fail "manifest was not created"
jq -e '
  .schema == "imposbro.postgres-backup.v1" and
  .contains_credentials == false and
  .database_schema_revision == "0003_audit_delivery_deletion" and
  (.archive.plaintext_sha256 | test("^[0-9a-f]{64}$"))
' "$artifact.manifest.json" >/dev/null || fail "backup manifest contract failed"

expect_failure overwrite \
  env PATH="$fake_bin:$PATH" PGDATABASE="imposbro_source" AGE_RECIPIENT="age1fake" BACKUP_SOURCE_ID="guardrail-test" \
  "$BACKUP_SCRIPT" --output "$artifact"
grep -q 'refusing to overwrite' "$test_root/overwrite.stderr" || fail "overwrite guard did not report its cause"

: > "$call_log"
env \
  PATH="$fake_bin:$PATH" \
  FAKE_CALL_LOG="$call_log" \
  AGE_IDENTITY_FILE="$identity" \
  "$RESTORE_SCRIPT" --artifact "$artifact" \
  >"$test_root/verify.stdout" 2>"$test_root/verify.stderr"
assert_no_database_call

tampered="$test_root/output/tampered.dump.age"
cp "$artifact" "$tampered"
original_digest="$(awk 'NR == 1 {print $1}' "$artifact.sha256")"
printf '%s  %s\n' "$original_digest" "$(basename "$tampered")" > "$tampered.sha256"
cp "$artifact.manifest.json" "$tampered.manifest.json"
printf 'tamper\n' >> "$tampered"
expect_failure tampered \
  env PATH="$fake_bin:$PATH" FAKE_CALL_LOG="$call_log" AGE_IDENTITY_FILE="$identity" \
  "$RESTORE_SCRIPT" --artifact "$tampered"
grep -q 'SHA-256 does not match checksum sidecar' "$test_root/tampered.stderr" || fail "tamper test did not fail on checksum"
assert_no_restore_call

backup_id="$(jq -r '.backup_id' "$artifact.manifest.json")"
evidence="$test_root/output/restore-evidence.json"
expect_failure missing_confirmation \
  env PATH="$fake_bin:$PATH" FAKE_CALL_LOG="$call_log" AGE_IDENTITY_FILE="$identity" \
    PGDATABASE="imposbro_drill" RESTORE_TARGET_ID="drill-01" RESTORE_TARGET_CLASS="isolated-drill" \
  "$RESTORE_SCRIPT" --artifact "$artifact" --execute --evidence "$evidence"
grep -q 'confirmation mismatch' "$test_root/missing_confirmation.stderr" || fail "missing confirmation guard did not report its cause"
assert_no_restore_call

expect_failure nonempty_target \
  env PATH="$fake_bin:$PATH" FAKE_CALL_LOG="$call_log" AGE_IDENTITY_FILE="$identity" \
    PGDATABASE="imposbro_drill" RESTORE_TARGET_ID="drill-01" RESTORE_TARGET_CLASS="isolated-drill" \
    RESTORE_CONFIRMATION="RESTORE:drill-01:$backup_id" FAKE_EXISTING_RELATIONS=4 \
  "$RESTORE_SCRIPT" --artifact "$artifact" --execute --evidence "$evidence"
grep -q 'target database is not empty' "$test_root/nonempty_target.stderr" || fail "non-empty target guard did not report its cause"
assert_no_restore_call

expect_failure production_without_approval \
  env PATH="$fake_bin:$PATH" FAKE_CALL_LOG="$call_log" AGE_IDENTITY_FILE="$identity" \
    PGDATABASE="imposbro_production" RESTORE_TARGET_ID="prod-01" RESTORE_TARGET_CLASS="production" \
    RESTORE_CONFIRMATION="RESTORE:prod-01:$backup_id" \
  "$RESTORE_SCRIPT" --artifact "$artifact" --execute --evidence "$evidence"
grep -q 'production restore requires --allow-production-target' "$test_root/production_without_approval.stderr" || fail "production opt-in guard did not report its cause"
assert_no_restore_call

expect_failure production_without_metadata \
  env PATH="$fake_bin:$PATH" FAKE_CALL_LOG="$call_log" AGE_IDENTITY_FILE="$identity" \
    PGDATABASE="imposbro_production" RESTORE_TARGET_ID="prod-01" RESTORE_TARGET_CLASS="production" \
    RESTORE_CONFIRMATION="RESTORE:prod-01:$backup_id" \
  "$RESTORE_SCRIPT" --artifact "$artifact" --execute --allow-production-target --evidence "$evidence"
grep -q 'RESTORE_CHANGE_TICKET must be set' "$test_root/production_without_metadata.stderr" || fail "production metadata guard did not report its cause"
assert_no_restore_call

env \
  PATH="$fake_bin:$PATH" \
  FAKE_CALL_LOG="$call_log" \
  AGE_IDENTITY_FILE="$identity" \
  PGDATABASE="imposbro_drill" \
  RESTORE_TARGET_ID="drill-01" \
  RESTORE_TARGET_CLASS="isolated-drill" \
  RESTORE_CONFIRMATION="RESTORE:drill-01:$backup_id" \
  FAKE_EXISTING_RELATIONS=0 \
  "$RESTORE_SCRIPT" --artifact "$artifact" --execute --evidence "$evidence" \
  >"$test_root/restore.stdout" 2>"$test_root/restore.stderr"

grep -q '^RESTORE ' "$call_log" || fail "guarded fake restore was not invoked"
jq -e '
  .schema == "imposbro.restore-evidence.v1" and
  .outcome == "success" and
  .target.class == "isolated-drill" and
  .validation.empty_target_precondition == true and
  .validation.audit_head_valid == 1 and
  .validation.required_tables == 10 and
  .validation.encrypted_to_plaintext_checksum_match == true and
  (.backup.decrypted_archive_sha256 | test("^[0-9a-f]{64}$")) and
  .live_restore_executed == true
' "$evidence" >/dev/null || fail "restore evidence contract failed"

printf '%s\n' 'PASS: backup/restore guardrails (stub commands only; no live PostgreSQL or real age encryption exercised).'
