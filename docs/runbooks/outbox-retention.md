# Runbook: PostgreSQL owned-outbox retention

Use `scripts/ops/outbox_retention.py` only for the two PostgreSQL outboxes owned
by IMPOSBRO. It never prunes the audit chain, state, heads, checkpoints, Kafka,
Typesense, logs, or backups. Dry-run is the default; apply is intentionally
multi-gated because deletion is irreversible.

## Preconditions and ownership

- Data owner approves the retention interval and confirms the downstream replay
  and restore window cannot resurrect an older mutation.
- Legal-hold owner exports the current complete hold set. A missing export means
  no run, not an empty hold set.
- Platform owner confirms the unpublished backlog ceilings and alerts are
  healthy, then supplies database connectivity through secret-managed
  `RETENTION_DATABASE_URL` or libpq environment variables. Never place the URL
  in command arguments, logs, evidence, or the policy.
- Operator creates a unique evidence path on access-controlled storage.
- Approver reviews the exact policy bytes and records its SHA-256. Policy and
  evidence must be mode 0600 and must not be symlinks.

The shipped example has `deny_all: true`, an expired placeholder window, and
zero backlog ceilings. Copy it outside the repository, set the exact database
identity and supported schema revision, review the allowlisted tables, holds,
retention seconds, backlog ceilings, and validity window, then `chmod 0600`.

## Dry-run

```text
scripts/ops/outbox_retention.py \
  --policy <controlled>/retention-policy.json \
  --evidence <controlled>/retention-dry-run-<job-id>.json \
  --batch-size 500 \
  --max-batches 1000 \
  --lock-timeout-ms 2000 \
  --statement-timeout-ms 30000
```

Review candidate counts and timestamp ranges for both tables. Confirm the
reported unpublished count, hold-entry count, database ID hash, Alembic
revision, policy validity, and evidence checksum. Dry-run acquires the same
retention advisory lock but deletes zero rows.

## Apply

Calculate the policy digest only after review:

```text
if command -v sha256sum >/dev/null 2>&1; then
  POLICY_SHA256=$(sha256sum <controlled>/retention-policy.json | awk '{print $1}')
else
  POLICY_SHA256=$(shasum -a 256 <controlled>/retention-policy.json | awk '{print $1}')
fi
export RETENTION_CONFIRMATION=APPLY:<database-id>:<policy-id>:<first-12-policy-sha256>

scripts/ops/outbox_retention.py \
  --policy <controlled>/retention-policy.json \
  --evidence <controlled>/retention-apply-<job-id>.json \
  --apply \
  --policy-sha256 "$POLICY_SHA256" \
  --batch-size 500 \
  --max-batches 1000 \
  --lock-timeout-ms 2000 \
  --statement-timeout-ms 30000
```

Never reuse an evidence filename or silently retry with a changed policy. Exit
0 means the reviewed candidate set was exhausted. Exit 2 means the configured
batch bound stopped safe partial progress; retain the evidence, inspect backlog
and latency, and start a new reviewed run. Exit 1 is a failure and may still
produce sanitized failure evidence.

## Safety invariants

For every apply run, verify:

- only published rows older than the policy cutoff were removed;
- `control_plane_outbox` still contains its greatest revision;
- every `indexing_event_heads.last_sequence` still has its corresponding latest
  outbox row when that identity was in scope;
- held revisions and identity hashes retain all rows;
- unpublished counts never decreased because of retention;
- audit row count, audit-head sequence, and audit-head hash are unchanged;
- evidence and `.sha256` are mode 0600, contain no connection URL or payload,
  and are transferred to the approved immutable evidence store.

The live fixture in `ops/dr/run-live-postgres-dr.sh` verifies these invariants
against real PostgreSQL before backing up and restoring the retained state.

## Failure response

- Database/schema/policy identity mismatch: stop and correct the deployment or
  review a new policy; never add a bypass flag.
- Rising/excess unpublished backlog: stop retention, restore publisher health,
  and reassess the replay horizon.
- Advisory lock unavailable: another run is active; do not run a second binary.
- Lock/statement timeout: keep the failure evidence, reduce batch size or fix
  contention, and issue a new reviewed run.
- Unexpected deletion count: stop scheduling, preserve evidence and database
  logs, classify an incident, and restore only into a new isolated target for
  investigation. Do not overwrite the source in place.

The policy file is an approved snapshot, not a legal-hold service. If the
authoritative hold service is unavailable, stale, or cannot produce the full
scope, set `deny_all: true` and stop.
