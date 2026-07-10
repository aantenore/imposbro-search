# Runbook: encrypted PostgreSQL backup and guarded restore

The scripts in `scripts/ops/` implement an age-encrypted custom-format backup,
encrypted-artifact and plaintext-archive checksums, verification-only restore
mode, and empty-target execution guards. `ops/dr/run-live-postgres-dr.sh`
exercises them with real PostgreSQL and age in two disposable containers. Its
redacted report is `docs/evidence/dr-live-postgres-latest.json` with a SHA-256
sidecar.

That local evidence proves only the isolated fixture contract. It does **not**
prove production scheduling, immutable/off-site storage, key-custody recovery,
backup expiry, region recovery, application readiness, or production RPO/RTO.

## Roles and prerequisites

- Backup operator: read-only backup role and public age recipient.
- Restore operator: target database owner in an isolated environment.
- Independent approver: required for any production target.
- Evidence owner: stores manifests, job logs, restore evidence, RPO/RTO results,
  and cleanup proof in an immutable evidence system.

Install compatible `pg_dump`, `pg_restore`, `psql`, `age`, and `jq`. Inject
libpq connection values through `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`,
`PGSSLMODE`, and a secret-managed `PGPASSFILE` or platform identity. The scripts
reject a connection URI in `PGDATABASE` so credentials cannot enter command
arguments or manifests.

Store the age identity outside the database, backup artifact, repository, and
primary failure domain. Test identity recovery under dual control.

## Create a backup

Select a new path on encrypted staging storage; existing artifacts are never
overwritten.

```text
export BACKUP_SOURCE_ID=<environment-id>
export BACKUP_RETENTION_CLASS=daily-35d
export BACKUP_EXPECTED_ALEMBIC_REVISION=<approved-revision>
export AGE_RECIPIENT=<public-age-recipient>
export PGDATABASE=<database-name>

scripts/ops/backup-control-plane-postgres.sh \
  --output <controlled-path>/control-plane-<utc>.dump.age
```

The script:

1. reads the live Alembic revision;
2. runs `pg_dump --format=custom --no-owner --no-acl` into a mode-0600 temporary
   directory;
3. encrypts with `age` and removes plaintext;
4. writes `<artifact>.sha256` and `<artifact>.manifest.json` containing encrypted
   artifact hash/size, plaintext custom-archive hash, source ID, database name,
   schema revision, retention class, and tool version, but no credentials or
   document payload;
5. refuses overwrite and cleans partial outputs on failure.

After success, upload all three files atomically to versioned, access-logged,
immutable, off-site storage. Apply retention through the storage platform, not
ad hoc deletion. Record scheduler/job ID, storage object version, immutability
mode, checksum, and alert result. Never log the age identity or decrypted dump.
SHA-256 sidecars detect accidental corruption but do not prove who created the
backup; origin assurance depends on restricted workload identity, immutable
storage audit logs, and an approved signed-attestation control where required.

## Verify without database contact

Run this routinely on newly stored copies and before any restore:

```text
export AGE_IDENTITY_FILE=<secret-managed-identity-path>
scripts/ops/restore-control-plane-postgres.sh \
  --artifact <controlled-path>/control-plane-<utc>.dump.age
```

Verification checks the checksum sidecar and manifest independently, decrypts
to a restricted temporary path, compares its SHA-256 with the pre-encryption
archive SHA-256, and runs `pg_restore --list`. It exits before requiring
`PGDATABASE` or running `psql`. Verify the temporary directory is removed after
forced-failure tests as part of platform qualification.

## Restore into an isolated empty target

Create a new isolated database. Disable production traffic and external side
effects. Confirm it has zero non-system relations; the script checks again and
will not drop or truncate anything.

Read the `backup_id` from the verified manifest, then set:

```text
export PGDATABASE=<new-empty-database-name>
export RESTORE_TARGET_ID=<stable-target-id>
export RESTORE_TARGET_CLASS=isolated-drill
export RESTORE_CONFIRMATION=RESTORE:<target-id>:<backup-id>

scripts/ops/restore-control-plane-postgres.sh \
  --artifact <controlled-path>/control-plane-<utc>.dump.age \
  --execute \
  --evidence <controlled-path>/restore-evidence-<utc>.json
```

Execution uses a single transaction, no owner/privilege restore, verifies the
manifest Alembic revision, requires all ten application tables, checks the
control-plane singleton/digest and audit-head invariants, and writes
`imposbro.restore-evidence.v1` JSON including the decrypted archive hash. The
evidence path must be new and is written mode 0600.

A production target additionally requires
`--allow-production-target`, `RESTORE_CHANGE_TICKET`, and
`RESTORE_APPROVED_BY`. These are necessary but not sufficient: follow the
approved change, traffic, failover, security, and incident procedures. Prefer a
new database and controlled cutover over in-place restore.

## Application validation and RPO/RTO

Use `docs/DR_TEST_TEMPLATE.md`. At minimum verify audit-chain boundaries,
unpublished outbox counts, authoritative/applied revision convergence, worker
readiness and checkpoint fencing, representative authenticated search/read/
ingest/delete journeys, replay without unexpected DLQ growth, and previously
completed erasure tombstones before any traffic.

Before a restored Typesense target serves traffic, run the configured
`DeletionReconciler.reconcile_restore()` adapter over every ledger entry. It
must reapply delete/prove absence on every recorded target even when the backup
contains an older successful receipt; then prove replay at or below each
tombstone sequence is suppressed. PostgreSQL restore alone does not satisfy
this cross-store gate.

Calculate RPO from the newest durable business change expected versus present,
not only backup timestamps. Calculate RTO from exercise declaration to validated
service recovery. A listed archive or restored schema alone is not a DR pass.

For a reproducible local regression drill, run:

```text
DR_RPO_TARGET_SECONDS=300 DR_RTO_TARGET_SECONDS=300 \
  ops/dr/run-live-postgres-dr.sh
```

The harness measures RTO from declaration through decrypt, transactional
restore, invariant checks, and source/target logical fingerprint equality. Its
RPO measurement is zero known lost mutations only because the synthetic source
is quiesced and the pre-backup and restored fingerprints match. Do not transfer
that number to a production workload; use the actual scheduled-backup age and
durable business marker there.

## Failure and rollback

- Checksum/manifest/archive-list failure: quarantine the artifact; do not try to
  repair sidecars. Select another independently verified backup and investigate
  storage/key integrity.
- Non-empty target: stop. Create a new isolated target; never add a drop flag.
- Restore transaction failure: preserve sanitized output and destroy/recreate
  the target before retrying.
- Post-restore invariant failure: do not start the application or mutate the
  restored data. Select a valid artifact or correct the tested procedure.
- Key unavailable: escalate to key custody; do not copy identities into tickets
  or bypass encryption.

After evidence sign-off, destroy the drill database, volumes, snapshots,
decrypted temporary data, and target credentials within 24 hours. Attach
platform-level destruction evidence. Keep only approved encrypted artifacts and
sanitized evidence for the required retention.

The local harness performs and verifies container/volume destruction before it
writes a passing report, then removes its encrypted backup, decrypted temporary
archive, and temporary age identity. Managed platforms still require their own
volume/snapshot/key cleanup evidence.
