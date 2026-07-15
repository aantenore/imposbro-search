# Disaster-recovery test evidence template

Status: **blank evidence template; no live DR test is represented by this file**.

Copy this document into the controlled evidence system for each exercise. Do
not commit credentials, age identities, decrypted dumps, customer payloads, or
internal connection strings. A successful script exit is one input to the
exercise, not the whole recovery decision.

## 1. Exercise metadata

| Field | Value |
|---|---|
| Exercise ID | `<DR-YYYY-NNN>` |
| Environment / region | `<isolated target>` |
| Scenario | `<database loss / region loss / corrupt state / other>` |
| Start / end UTC | `<timestamps>` |
| Incident commander | `<name or team>` |
| Restore operator | `<name or team>` |
| Security/data approver | `<name or team>` |
| Change ticket | `<reference>` |
| Communications channel | `<reference>` |
| Production traffic involved | `No` / `<approved exception>` |

## 2. Objectives and result

| Objective | Approved target | Measured result | Pass / fail |
|---|---:|---:|---|
| RPO: newest durable control-plane change lost | `<duration>` | `<duration>` | `<status>` |
| RTO: declaration to validated recovery | `<duration>` | `<duration>` | `<status>` |
| Indexing replay / convergence | `<duration>` | `<duration>` | `<status>` |
| Query API readiness after recovery | `<duration>` | `<duration>` | `<status>` |

Overall result: `<pass / conditional pass / fail>`

Business impact and explicit assumptions: `<text>`

## 3. Backup provenance and integrity

| Field | Evidence |
|---|---|
| Backup ID / manifest schema | `<value>` |
| Source ID and created UTC | `<value>` |
| Artifact SHA-256 | `<value>` |
| Checksum sidecar verification | `<command output reference>` |
| Encryption format / key owner | `age / <owner; no secret>` |
| Database schema revision | `<manifest value>` |
| Retention class / storage immutability | `<value and platform evidence>` |
| Backup success alert / scheduler run | `<reference>` |

## 4. Isolation and safety preconditions

- [ ] Target ID and class were explicitly set.
- [ ] Target network, IAM, and DNS cannot receive production traffic.
- [ ] Database contained zero non-system relations before restore.
- [ ] Exact `RESTORE:<target-id>:<backup-id>` confirmation was recorded.
- [ ] Production guard was not used, or change ticket and independent approval
      are attached.
- [ ] Decrypted temporary files use restricted permissions and cleanup was
      verified.
- [ ] Kafka consumers, publishers, webhooks, email, and other side effects were
      disabled or redirected in the isolated target.
- [ ] Restore-suppression tombstones for previously erased data were available.

## 5. Timeline

| UTC time | Actor | Action / observation | Evidence link |
|---|---|---|---|
| `<time>` | `<actor>` | Declaration | `<link>` |
| `<time>` | `<actor>` | Artifact selected and verified | `<link>` |
| `<time>` | `<actor>` | Restore started | `<link>` |
| `<time>` | `<actor>` | Database validation completed | `<link>` |
| `<time>` | `<actor>` | Application validation completed | `<link>` |
| `<time>` | `<actor>` | Exercise closed and target destroyed | `<link>` |

## 6. Commands and machine evidence

Record sanitized command lines and immutable output references. Minimum:

```text
scripts/ops/restore-control-plane-postgres.sh --artifact <path.age>
scripts/ops/restore-control-plane-postgres.sh --artifact <path.age> \
  --execute --evidence <restore-evidence.json>
```

Attach the generated `imposbro.restore-evidence.v1` JSON. Record tool versions
and platform job/pod IDs. Never paste libpq passwords, connection URIs, age
identity material, or decrypted archive paths.

For the repository's isolated regression exercise, also attach the
`imposbro.dr-drill-evidence.v1` report and its `.sha256` sidecar. Verify the
encrypted artifact SHA-256, pre-encryption/decrypted custom-archive SHA-256
match, source/restored logical fingerprint match, retention before/after
counts, audit-head equality, and container/volume destruction assertion. This
local report is never a substitute for the scheduler, object-lock, IAM, key
custody, region, or traffic evidence required above.

## 7. Validation checklist

### PostgreSQL and control plane

- [ ] `pg_restore --list` succeeded before database contact.
- [ ] Restored Alembic revision equals the manifest revision.
- [ ] All ten required control-plane/outbox/checkpoint/audit-delivery/deletion-ledger tables exist.
- [ ] Audit delivery checkpoint and deletion-ledger/target fingerprints match source.
- [ ] Restored providers reapply every suppression and reject replay at or below the tombstone before traffic.
- [ ] `control_plane_state` singleton invariant is valid.
- [ ] Audit head and hash-chain sample verify.
- [ ] Unpublished control-plane and indexing outbox counts are recorded.
- [ ] Least-privilege application and exporter roles work; backup role cannot
      mutate data.

### Application and dependencies

- [ ] Query API starts with the restored PostgreSQL state.
- [ ] Every Query API replica reports authoritative/applied revision convergence.
- [ ] Redis and Kafka connections use isolated endpoints.
- [ ] Indexing workers become ready and checkpoints prevent duplicate mutation.
- [ ] Representative search, read, ingest, delete, and tombstone journeys pass.
- [ ] Outbox replay converges without unexpected DLQ growth.
- [ ] Typesense target topology and document counts match the recovery scenario.

### Security and deletion

- [ ] Restored secrets came from the target secret manager, not the backup.
- [ ] Previously completed erasure cases were re-applied before serving.
- [ ] Logs and evidence contain no document bodies or credentials.
- [ ] Decrypted data, restore database, volumes, snapshots, and temporary keys
      were destroyed after sign-off.

### Observability and failback

- [ ] Prometheus targets, recording rules, dashboards, and Alertmanager routes
      were validated in the recovery environment.
- [ ] A synthetic alert reached the intended non-production receiver.
- [ ] Failback/fail-forward decision, DNS plan, and rollback point were tested or
      explicitly marked out of scope.

## 8. Deviations, defects, and follow-up

| Severity | Finding | Owner | Due date | Ticket | Retest criterion |
|---|---|---|---|---|---|
| `<level>` | `<finding>` | `<owner>` | `<date>` | `<id>` | `<criterion>` |

Record every manual step that should become automation and every target that
was not exercised. A conditional pass must list the accepting authority and
expiry date.

## 9. Evidence index and sign-off

| Evidence | Immutable location / digest | Retention |
|---|---|---|
| Backup manifest and checksum | `<reference>` | `<period>` |
| Restore evidence JSON | `<reference>` | `<period>` |
| Scheduler/storage/IAM evidence | `<reference>` | `<period>` |
| Logs, metrics, screenshots | `<reference>` | `<period>` |
| Test results and incident timeline | `<reference>` | `<period>` |
| Cleanup/destruction proof | `<reference>` | `<period>` |

Sign-off:

- Service owner: `<name / date / decision>`
- Platform owner: `<name / date / decision>`
- Security/data owner: `<name / date / decision>`
- Product/business owner: `<name / date / decision>`

Next exercise date and scenario: `<date / scenario>`
