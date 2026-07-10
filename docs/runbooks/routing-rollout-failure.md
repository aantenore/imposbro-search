# Runbook: routing cutover failure and rollback

Use this procedure when a routing rollout enters `failed`, a candidate target
degrades after `cutover`, parity or convergence becomes uncertain, or rollback
must begin before `completed`. The lifecycle and coverage guarantees are
defined in [ADR 0003](../adr/0003-safe-routing-migrations.md). This runbook does
not authorize direct routing-state, database, Kafka-offset, or provider-data
edits.

## Trigger and impact

Possible impact includes missing/stale search results on the candidate,
asymmetric deletes, rising outbox/consumer lag, DLQ growth, or replicas applying
different routing revisions. A rollout in `completed` is terminal and requires
a new corrective rollout; do not try to reopen it.

Declare at least SEV-2 for a failed cutover or sustained degraded state. Escalate
to SEV-1 when tenant placement, delete correctness, audit integrity, or broad
availability is untrusted. Page the service, search-data, streaming, and
platform owners; add security/data ownership for cross-tenant or erasure risk.

The reference alerts page after a rollout remains `failed` for one minute or
`rolling_back` has no persisted update for 15 minutes (plus a five-minute
confirmation). A cutover/drain that remains active beyond its recorded rollback
deadline creates a ticket after five minutes. These thresholds are deployment
policy and may be tightened, but must not be disabled without an accepted risk.

```promql
max(imposbro_routing_rollouts{phase="failed"})
max(imposbro_routing_rollout_max_phase_age_seconds{phase="rolling_back"})
max(imposbro_routing_rollout_max_deadline_overrun_seconds{phase=~"cutover|drain"})
```

## Immediate safety

1. Stop completion and new routing/schema changes for the collection. Freeze
   unrelated releases when a correlated deployment cannot be excluded.
2. Preserve rollout ID, collection, global control-plane revision/ETag, rollout
   version/phase, active and candidate targets, cutover/rollback deadline,
   parity/barrier/checkpoint, Kafka lag, unresolved DLQ, outbox state, target
   health, request IDs, and first failure time.
3. Do not edit routing JSON, submit operator-computed parity/checkpoint values,
   purge either target, reset offsets, or mark outbox/DLQ work complete. The
   persisted lifecycle intentionally keeps broad write/delete coverage during
   failure and rollback.
4. Bound or pause ingest before durable queues or provider capacity exhausts.
   Existing authenticated reads may continue only while their explicit partial
   and tenant-isolation behavior remains acceptable.

## Diagnose and choose the response

- Confirm every Query API replica reports the same authoritative/applied
  revision and rollout version. If not, follow
  [configuration convergence](config-convergence.md) before another transition.
- Compare candidate/source Typesense health and document parity at the durable
  source barrier. Do not trust counts alone.
- Inspect indexing outbox, primary consumer lag, resolver-group DLQ lag, worker
  readiness/checkpoints, recent schema/secret/certificate changes, and provider
  capacity. Follow [outbox and DLQ recovery](outbox-lag.md) without clearing the
  rollout gate manually.
- If the candidate issue is bounded and reversible before data correctness is
  lost, repair it and recompute machine-owned parity/lag evidence before
  continuing. Otherwise begin rollback. A rollback is preferred when the root
  cause or recovery window is uncertain.

## Execute the persisted rollback

1. Refetch `GET /api/v1/admin/routing-rollouts/{rollout_id}` and the current
   global revision. Do not reuse values captured before diagnosis.
2. Through the Admin UI or canonical transition API, submit `rolling_back`
   with both current controls: `If-Match: "<global-revision>"` and body
   `{"target_phase":"rolling_back","expected_version":<rollout-version>}`.
3. On `409`, refetch and re-evaluate the new authoritative state. Never retry a
   stale CAS blindly. On `422`, preserve the gate error. On `503`, restore the
   dependency/evidence path; do not bypass it.
4. Verify the persisted phase and audit event on every replica. During
   `rolling_back`, search/write/delete coverage remains the active/candidate
   union so neither historical copy is silently hidden.
5. Exercise authenticated tenant-scoped search, ingest, delete, outbox drain,
   worker checkpoint, and configuration convergence against the active policy.
   Confirm no new DLQ item, lag growth, partial response, or cross-tenant result
   appears during the stabilization interval.
6. Refetch both revisions again and transition legally from `rolling_back` to
   `rolled_back`. Verify reads and writes now resolve only through the recorded
   active policy. Keep the candidate data intact until incident/evidence and
   retention owners approve reconciliation.

If persistence or provider calls fail after desired state is recorded, leave
that state for normal reconciliation and keep the incident open. Do not perform
an unversioned in-memory or direct-database compensation.

## Validate and close

- all serving replicas expose the same revision, digest, rollout version, and
  terminal `rolled_back` phase;
- authenticated search/ingest/delete journeys pass on the active policy;
- source target parity is understood, lag/outbox drains, resolver-group DLQ lag
  is zero, checkpoints are monotonic, and no older event can resurrect a
  tombstone;
- the audit trail contains the failure, `rolling_back`, and `rolled_back`
  decisions with actor/request/revision evidence;
- alerts, capacity, and redundancy return to their approved baseline; and
- a new rollout/defect record owns the forward remediation. Never reuse the
  terminal workflow.

Attach sanitized API responses, revisions, target/lag/checkpoint summaries,
dashboard interval, incident timeline, approvals, and validation evidence. Do
not attach credentials, document payloads, or raw tenant identifiers. Follow
[the release rollback contract](../RELEASE_ROLLBACK.md) for a correlated image
rollback and [the ownership contract](../OPERATIONS_OWNERSHIP.md) for handover.
