# ADR 0003: Safe routing migrations without hidden documents

- **Status:** accepted
- **Date:** 2026-07-10
- **Decision owners:** IMPOSBRO maintainers

## Context

Changing the cluster selected for a collection is not a metadata-only edit.
Historical documents remain on the former target while new events may reach a
new target. An immediate routing switch can therefore hide documents from
search, leave deletes on only one copy, or make rollback impossible. Operator-
entered counts and checkpoints are not trustworthy release evidence.

Typesense does not provide a transaction spanning clusters, Kafka, and the
control-plane database. The migration must make every intermediate state
explicit and safe under retries, concurrent ingest, worker restart, and partial
cluster failure.

## Decision

Persist each change as a revisioned `RoutingRollout` with the lifecycle:

`draft -> validating -> dual_write -> backfill -> verifying -> cutover -> drain -> completed`

Failure paths end in `cancelled`, `failed`, or
`rolling_back -> rolled_back`. The pure domain state machine owns transition
legality and gates; HTTP handlers and Typesense adapters cannot bypass it.

The migration protocol is:

1. Record active and candidate policies in a side-effect-free draft.
2. Require validation and capacity attestations before dual-write.
3. Capture a PostgreSQL indexing-event high-water mark while writers are
   fenced against commit reordering.
4. Copy source documents in bounded resumable batches, persisting an opaque
   cursor and counts after each successful batch.
5. Repair every identity changed after the snapshot with a newly sequenced
   durable event sent to every candidate target. A stale snapshot copy can
   never overwrite that later sequence at the worker checkpoint.
6. Compute exact logical parity at a stable high-water mark. Operators may run
   the measurement but cannot submit their own digest or checkpoint.
7. At cutover, measure Kafka consumer lag and the resolver DLQ group, require
   zero unresolved DLQ and the configured lag budget, then atomically commit
   the candidate policy as desired state.
8. During drain, reads and deletes retain coverage of every target that can
   still hold historical documents. Completion removes old coverage only after
   the rollback window and gates permit it.

Every transition uses both the global control-plane revision and rollout-local
version. A stale operator receives `409` and no side effect. Collection
deletion first commits a routing tombstone so a partially deleted provider copy
cannot become visible again.

## Failure and rollback semantics

- A failed provider call leaves durable desired state marked for reconciliation;
  it is not compensated by an unversioned in-memory rollback.
- Backfill and repair are idempotent. Repeating a completed chunk or event does
  not create an older visible version.
- Parity failure returns the rollout to backfill or begins rollback; it never
  permits cutover.
- Before completion, rollback restores the recorded active policy and retains
  broad read/delete coverage while candidate copies drain.
- After completion, a new rollout is required. The old workflow is immutable
  audit history rather than a reusable switch.

## Alternatives rejected

An immediate routing-map edit was rejected because it hides historical
documents. A best-effort bulk copy followed by document counts was rejected
because equal counts do not prove equal identities or content and concurrent
writes can race the copy. A globally paused ingestion window was rejected as
the normal design because it turns every migration into an outage; it remains
an explicit incident mitigation, not the protocol.

## Verification

Domain tests cover every legal and illegal transition. Adapter tests inject a
concurrent higher-sequence event during stale copy and prove the repair wins.
The enterprise live harness exercises two real Typesense clusters, Kafka,
PostgreSQL, worker restart, cutover, rollback, and TLS. Release evidence must
retain the machine-owned checkpoint, parity digest, lag, DLQ state, revisions,
and exact source commit.
