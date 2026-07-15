# Runbook: data deletion convergence

Use for an authorized document/subject erasure case or when a tombstone did not
converge. The target lifecycle is defined in `docs/DATA_LIFECYCLE.md`. The
repository does not currently provide a complete erasure ledger or automated
cross-store proof, so this procedure cannot by itself support an enterprise
erasure claim.

## Intake, authorization, and safety

1. Create a controlled case with ID, tenant, lawful basis, requester authority,
   requested identities/selectors, requested UTC, due date, and data-owner.
2. Check legal hold before mutation. A hold requires security/data-owner
   direction; do not silently ignore or override it.
3. Narrow selectors into explicit tenant/collection/document identities. Use the
   service authorization model; never execute an unreviewed broad Typesense,
   Kafka, or SQL delete.
4. Record only necessary identifiers. Do not paste document bodies, API keys,
   passwords, or decrypted backup data into the case.
5. Capture pre-delete locations as counts/hashes and timestamps, not raw content.

## Execute through the ordered path

Submit the normal authenticated versioned delete request with a unique request
ID and idempotency key. For multiple identities, use a bounded, resumable job
whose configuration and approvals are attached to the case. Do not bypass Kafka
or durable sequence/checkpoint fencing with direct Typesense deletes unless an
approved emergency design also creates a later durable tombstone.

The accepted delete/tombstone sequence must be newer than every known upsert for
that identity and target every cluster that can contain the collection. Record
event ID, sequence, routing revision, rollout ID, and targets from sanitized
application evidence.

## Verify active-store convergence

Within the initial 15-minute target:

- authenticated document reads return not found on every candidate cluster;
- direct privileged verification on each Typesense target finds no document;
- indexing outbox has no unpublished matching tombstone;
- worker checkpoint/idempotency decisions show the tombstone was applied or an
  idempotent not-found was accepted on every target;
- no later upsert has been accepted for the identity;
- search queries no longer return the identity, allowing for documented index
  refresh behavior.

Use request/event IDs to inspect logs. Do not add document or tenant IDs as
Prometheus labels. If any target fails, preserve ordering and follow the outbox
lag runbook; never mark an event successful manually.

## Verify replay and retained copies

Within 24 hours, inspect the primary topic, retry paths, and DLQ using
access-controlled tooling. Resolve any older matching upsert so it cannot replay
after the tombstone. Preserve the tombstone/checkpoint at least through the
maximum Kafka and restore replay horizon.

Record disposition for:

- eligible published PostgreSQL outbox rows and identity heads;
- Redis/cache values and logs under their approved retention policy;
- control-plane audit records without breaking the hash chain;
- Kafka primary/DLQ retention and any legal hold;
- encrypted backups that may contain the identity.

Immutable backups are not edited in place. Add the identity/case to the durable
restore-suppression ledger, record the latest possible backup expiry date, and
require any future restore to reapply the tombstone before serving. If such a
ledger is unavailable, the case remains open as a control gap.

## Failure handling

- **Unauthorized or ambiguous scope:** stop without mutation and return to the
  data owner.
- **Newer concurrent upsert:** freeze writes for the identity, establish order,
  and publish a later authorized tombstone.
- **Outbox/Kafka failure:** retain the durable row and use
  `outbox-lag.md`; do not delete or mutate publication state.
- **DLQ poison event:** quarantine under restricted access, validate schema and
  authorization, then replay through the supported idempotent path.
- **Typesense target unavailable:** keep the case open, preserve the tombstone,
  and verify immediately after recovery.
- **Backup/hold conflict:** escalate to security/data owner; document policy and
  expiry rather than claiming immediate physical deletion.

## Close and evidence

Close only when all active targets converge, retry/DLQ resurrection is blocked,
retained copies have an approved expiry/hold disposition, restore suppression is
recorded, and the data owner signs off. Evidence should contain case ID,
authorization/hold decisions, event/sequence/target map, before/after counts or
hashes, query/job timestamps, outbox/checkpoint status, DLQ review, backup expiry
boundary, exceptions, and approver.

If any store cannot be checked, label the result partial and assign an owner and
deadline. Static policy, a successful API response, or absence from one search
result is not proof of cross-store erasure.
