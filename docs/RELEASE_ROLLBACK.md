# Release and rollback metadata contract

Every release is a complete platform set: Query API, indexing service, and
Admin UI images built from one source commit. Partial component promotion is
forbidden. The release workflow records each immutable digest, SPDX and
CycloneDX SBOM hashes, GitHub provenance attestations, keyless signatures,
changelog, and this rollback contract in one attested manifest.

## Preconditions

- The enterprise hosted quality gate is green on the exact source commit.
- Database migrations have a verified backup and isolated restore result.
- API and event schemas are backward compatible with the last release.
- Routing, DR, browser, load, live dependency, Helm, container, and security
  evidence has no skipped critical scenario.
- The operator records environment, current platform release/digests,
  candidate release/digests, schema revision, control-plane revision, active
  routing rollouts, owner, approval, and observation window.

## Deployment order

1. Apply forward-compatible database migrations using the serialized migration
   job. Do not deploy application pods if schema verification fails.
2. Deploy Query API replicas gradually, preserving quorum/capacity and checking
   authoritative/applied revision convergence per replica.
3. Deploy workers gradually. Confirm checkpoint backend, consumer lag, outbox
   drain, DLQ, and event schema before increasing concurrency.
4. Deploy Admin UI and run the canonical operator/browser smoke.
5. Retain the former signed platform digest set throughout the observation and
   database rollback windows.

## Rollback decision matrix

| Condition | Supported response |
|---|---|
| Application defect; schema remains backward compatible | Roll back all component images to the recorded former digests. |
| One unhealthy replica; remaining capacity safe | Remove/replace only that replica through the controller. |
| Routing rollout before completion | Use its persisted `rolling_back` workflow; do not edit routing JSON directly. |
| Migration defect with old code still schema-compatible | Roll back images, preserve database, open a forward-fix migration. |
| Destructive/incompatible schema migration | Freeze mutations/ingest, restore the verified backup into an isolated target, validate, then execute the approved DR/failover plan. |
| Corrupt control-plane revision | Commit an audited corrective revision or restore through the guarded workflow; never decrement/edit revision rows. |

## Mandatory rollback checks

- Select images by digest from the previous attested release manifest, never a
  mutable tag.
- Verify signatures, provenance repository/workflow identity, and SBOM hashes.
- Confirm no active routing migration depends on candidate-only behavior.
- Record Kafka/outbox/checkpoint positions before and after rollback; do not
  purge backlog or reset offsets to make health green.
- Verify every Query API replica reports the same state digest/revision, worker
  readiness, bounded lag, zero unexpected DLQ growth, and representative
  search/ingest/delete/tombstone journeys.
- Preserve incident/evidence links and create a forward remediation. A rollback
  is mitigation, not closure.

Database downgrade is disabled by default and requires the explicit destructive
environment guard, a verified backup, an empty/isolated recovery path where
applicable, independent approval, and the migration-specific rollback test.
