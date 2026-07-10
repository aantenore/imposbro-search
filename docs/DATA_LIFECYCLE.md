# Data lifecycle, retention, and deletion convergence

Status: **repository-owned PostgreSQL outbox retention and durable deletion
suppression are implemented; live provider reconciliation and deployment
retention remain open**.

IMPOSBRO copies data through PostgreSQL, Kafka, Typesense, Redis, telemetry, and
encrypted backups. Deletion is complete only when every active copy has
converged and every retained copy is either expired under policy or covered by
an approved legal hold. Configuration, retention, and identifiers must be
environment-owned; values below are safe initial targets, not hard-coded runtime
behavior.

## Data inventory and target retention

| Store / object | Purpose and possible sensitive content | Initial retention target | Deletion / expiry owner | Enforcement status |
|---|---|---|---|---|
| PostgreSQL `control_plane_state` | Current clusters, routing, and policy configuration | Current version while service exists | Service owner | Replaced transactionally; no subject-erasure workflow |
| PostgreSQL `control_plane_audit*` | Actor, action, resource IDs, request IDs, change details and hash chain | 400 days, subject to audit/legal policy | Security/data owner | Append-only application behavior; no scheduled archive/purge |
| PostgreSQL `control_plane_outbox` | Configuration event payload and delivery errors | Unpublished until delivered; published 30 days | Platform owner | `outbox_retention.py` can purge eligible published rows while preserving the newest global revision; scheduling and approved production policy are deployment-owned |
| PostgreSQL `indexing_event_outbox` | Document event payload, tenant, identity, idempotency and errors | Unpublished until delivered; published 7 days after the replay horizon is safe | Data-plane owner | `outbox_retention.py` can purge eligible published rows while preserving the newest mutation per identity and held identities; scheduling and replay-horizon approval are deployment-owned |
| PostgreSQL `indexing_event_heads` | Tenant, collection, document ID and last sequence | Active identity plus 30 days after last event | Data-plane owner | Durable checkpoint; no scheduled purge |
| PostgreSQL `deletion_ledger*` | Identity, latest tombstone boundary, target receipts, legal hold and restore suppression; no document payload | At least the maximum Kafka replay and backup restore horizon; indefinite when no approved horizon exists | Data/security owner | Tombstones commit atomically with the indexing outbox; provider reconciliation remains deployment-wired |
| Kafka primary indexing topic | Full upsert/delete/tombstone envelopes | 7 days | Streaming owner | Broker configuration is environment-specific |
| Kafka dead-letter topic | Failed envelopes and error context | 30 days, access-restricted | Streaming/security owner | Broker configuration is environment-specific |
| Typesense data collections | Searchable customer documents | Product contract; remove within 15 minutes of accepted delete | Data-plane owner | Asynchronous delete/tombstone path exists; no end-to-end erasure ledger |
| Typesense internal state | Legacy control-plane state if that backend is enabled | Until migrated, then 30-day rollback window | Service owner | Backend-dependent; enterprise PostgreSQL should be authoritative |
| Redis cache / Pub/Sub | Ephemeral configuration propagation and rate-limit state | TTL or process lifetime; no durable retention | Platform owner | Configuration-dependent |
| Application logs | Request IDs, errors, operational metadata; document bodies must not be logged | 30 days hot, 90 days restricted archive | Observability/security owner | Sink policy is environment-specific |
| Prometheus metrics | Low-cardinality operational labels; no tenant/document IDs | 15 days raw, up to 13 months approved aggregates | Observability owner | Storage policy is environment-specific |
| Encrypted PostgreSQL backups | All persisted control-plane/outbox/checkpoint content | Daily 35 days; monthly 12 months if approved | DR/data owner | Encrypted backup and clean restore are automated and exercised locally; scheduling, immutable off-site storage, key custody, and expiry are deployment-owned |
| Restore drill copies | Decrypted restored database in isolated target | Delete immediately after evidence capture; maximum 24 hours after sign-off | DR test owner | Local harness destroys its containers, volumes, decrypted dump, and temporary identity; managed-platform snapshot/volume destruction still needs platform evidence |

Before production, the data owner must replace targets where contractual,
regulatory, residency, or legal requirements differ. Published outbox rows must
never be purged until the downstream replay horizon and recovery design prove
that deletion cannot resurrect an older upsert.

## Deletion convergence contract

Each deletion case needs an external case ID because the repository does not
yet implement a complete erasure ledger. Record tenant, collection, document or
subject selector, authorization decision, requested timestamp, legal-hold
decision, and evidence links. Do not place unnecessary subject data in the case.

1. **Authorize and freeze scope.** Verify tenant ownership and translate the
   request into stable document identities. Reject broad or ambiguous filters.
2. **Publish a versioned tombstone.** Use the normal authenticated delete path
   with idempotency and request IDs. The durable sequence must be later than any
   accepted upsert for the same identity.
3. **Converge active indexes.** Every routed Typesense target must acknowledge
   delete or an idempotent not-found result. Initial target: 15 minutes.
4. **Converge retry paths.** Resolve or quarantine matching primary-topic,
   retry, outbox, and DLQ entries. A failed older upsert must not replay after
   the tombstone. Initial DLQ target: 24 hours.
5. **Expire transient and persisted payload copies.** Remove eligible published
   outbox rows, cache values, and logs under approved policy. Preserve minimum
   non-sensitive evidence such as case ID, hash, timestamps, and outcome.
6. **Handle backups.** Do not rewrite immutable backups in place. Mark the
   identity in a restore-suppression/tombstone ledger and reapply deletion
   before any restored system serves traffic. Expire the encrypted backup at
   the end of its retention period; use approved crypto-erasure where supported.
7. **Verify and close.** Query every active target, inspect checkpoint/outbox
   state, prove no matching DLQ item remains, record backup expiry boundary, and
   obtain data-owner sign-off.

The tombstone and its durable checkpoint must live at least as long as the
maximum source replay and restore window, or a separate restore-suppression
ledger must survive longer. Otherwise an old backup or Kafka event can
reintroduce deleted content.

Migration `0003_audit_delivery_deletion` adds the durable suppression
ledger and backfills the latest existing delete/tombstone per identity. Every
new PostgreSQL delete/tombstone registers its sequence, event digest, routing
revision and expected targets in the same transaction as the indexing outbox.
An unset `retention_until` means indefinite suppression and is intentionally the
safe default. Legal-hold references and provider receipts are stored only as
SHA-256 digests.

`DeletionReconciler.reconcile_restore()` is the provider-neutral restore gate. While restored
targets remain isolated, it enumerates every suppression, invokes a configured
target adapter, and records an `applied` or `absent` receipt only when its
checkpoint is at least the tombstone sequence. Replay at or below the ledger
sequence is rejected; only a later intentional mutation may supersede it.
The restore method deliberately re-verifies every target even when a receipt
existed before the backup, because restored provider data may have resurrected
an older document. Traffic must not reopen until the restore reconciliation
completes and all pending targets are zero. The repository ships
the port and deterministic tests, not a claim that any particular Typesense or
managed-backup restore has executed it.

## Repository-owned outbox retention

`scripts/ops/outbox_retention.py` is the only repository-owned destructive
retention path. It supports only `control_plane_outbox` and
`indexing_event_outbox`; table names cannot be supplied dynamically. Its
reviewed policy is a mode-0600 strict JSON file. The deny-all example in
`ops/dr/retention-policy.example.json` must be copied, reviewed, time-bounded,
and explicitly enabled outside the repository before use.

Implemented controls are:

- dry-run mode is the default and reports candidate counts and oldest/newest
  timestamps;
- delete only rows with `published_at IS NOT NULL`; never age out an unpublished
  event;
- preserve the highest control-plane revision and the highest sequence for every
  indexing identity, whether that newest row is published or unpublished;
- exclude explicitly held control-plane revisions and indexing identity hashes;
- use bounded batches, `SKIP LOCKED`, lock/statement timeouts, a database
  advisory lock, and a maximum batch count; exit code 2 means bounded partial
  progress and requires another reviewed run;
- require exact database name, supported Alembic revision, database-clock policy
  validity, a table allowlist, and an unpublished-backlog ceiling;
- require the reviewed policy SHA-256 plus exact target/policy confirmation for
  apply mode; a missing, expired, malformed, unexpectedly permissive, or
  world-readable policy fails closed;
- stop if the unpublished backlog rises during the run;
- write a new mode-0600 JSON evidence artifact and SHA-256 sidecar containing
  only counts, timestamps, hashes, safety assertions, and redacted failure code.

The CLI deliberately does not modify `control_plane_audit` or
`control_plane_audit_head`; its evidence is the maintenance execution record.
It also does not decide that Kafka replay, backup expiry, or a deletion case is
safe. The data owner must establish those prerequisites in the signed policy
review before apply. See `docs/runbooks/outbox-retention.md`.

## Legal hold and audit-chain constraints

Legal hold overrides ordinary expiry and must record authority, scope, start,
review date, and release approval. Access to held DLQ, logs, backups, and audit
records must be restricted and logged.

The CLI consumes a reviewed snapshot of held revisions and identity hashes; it
is not the legal-hold system of record and does not sign policy files. The
deployment must export the current authoritative holds, retain approval and
policy-digest evidence, and reject the job when that export is unavailable.
Large or dynamic hold sets should move to an authoritative database adapter
rather than weakening the current bounded policy-file contract.

The control-plane audit table is hash chained. Deleting rows directly can break
verification. Archive complete signed chain segments with boundary hashes, or
pseudonymize sensitive fields through an approved design. Until that exists,
avoid placing customer document bodies or unnecessary personal identifiers in
control-plane audit details.

## Evidence and open gaps

For every lifecycle control, evidence should include policy version, deployed
configuration, job execution ID, before/after counts, error output, sample hash
(never raw sensitive content), timestamps, environment, and approver. The
runbook `docs/runbooks/data-deletion-convergence.md` defines case-level checks.

The isolated live exercise and checksum are stored as
`docs/evidence/dr-live-postgres-latest.json` and `.sha256`. It proves the local
fixture contract only: real age encryption, PostgreSQL custom backup, clean
transactional restore, logical fingerprint equality, bounded retention, legal
hold exclusion, latest-mutation preservation, and cleanup. It is explicitly not
production certification.

Open gaps that prevent an enterprise erasure or deployment-DR claim are: no
production scheduler evidence, no deployed provider adapter invoking the
restore gate, no broker/storage retention evidence, no live backup-expiry
evidence, no signed legal-hold export, and no managed-region
failover/key-custody exercise. These remain deployment/product work and must not
be inferred from deterministic repository tests.
