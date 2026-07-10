# ADR 0004: At-least-once indexing with durable ordering and fencing

- **Status:** accepted
- **Date:** 2026-07-10
- **Decision owners:** IMPOSBRO maintainers

## Context

The Query API accepts document mutations before Typesense applies them. A crash
between database, Kafka, and Typesense calls can otherwise lose a mutation or
apply it more than once. Kafka transactions cannot atomically commit a
Typesense write and PostgreSQL transaction, so an exactly-once claim would be
false.

## Decision

The platform guarantees durable, idempotent **at-least-once** delivery:

- The Query API transactionally allocates a strictly increasing sequence per
  `(tenant, collection, document)` identity, writes one canonical envelope v2,
  and appends it to the PostgreSQL indexing outbox.
- An idempotency-key hash plus request digest returns the original event for an
  identical retry and rejects reuse with different content.
- One logical event contains every routing target. Its Kafka key is the exact
  canonical identity, preserving one partition order for that identity.
- The publisher marks the outbox row only after Kafka acknowledgement. A crash
  before the mark republishes the same event ID, key, sequence, and payload.
- The worker validates the schema, key, tenant-safe delete filter, target set,
  W3C trace context, and monotonic metadata before provider calls.
- In enterprise mode, PostgreSQL checkpoint rows are locked in a deterministic
  order for the whole event. A target applies only a sequence newer than its
  checkpoint; completed targets are skipped on replay and partial progress is
  retained.
- Kafka offsets are acknowledged only after every target has a durable success
  checkpoint. Bounded retry exhaustion publishes a redacted DLQ envelope for
  explicit resolution.

The outbox global position is monotonic but may contain gaps from rolled-back
identity allocation. A high-water read briefly fences in-flight insert commits
so a backfill cursor cannot advance past a lower position that appears later.
Repair locks the identity head and clones the latest durable mutation with a new
sequence and route metadata.

## Consistency model

Accepted writes are durable in PostgreSQL before publication and become
eventually visible in Typesense. Searches are not read-your-writes unless a
caller waits for the operation/event evidence. Ordering is per logical identity,
not global across identities. Multi-target application is not atomic: during a
dependency outage some targets may be ahead, but replay converges monotonically
and routing lifecycle gates prevent unsafe cutover.

Deletes are versioned tombstones using the same identity order. A late older
upsert cannot pass a target checkpoint after the tombstone. Tombstone and
checkpoint retention must cover every Kafka and restore replay horizon.

## Operational recovery

The DLQ resolver selects exactly one source topic/partition/offset, validates
the expected original topic and envelope, and defaults to inspect/dry-run.
Replay preserves event ID, sequence, payload, and key. Commit requires exact
operator confirmation and occurs only after producer acknowledge, flush, and a
fresh group-position check; disposition requires a reason and approver. Bulk
skip, rewind, and blind offset reset are deliberately absent.

## Alternatives rejected

Claiming exactly once was rejected because Typesense and PostgreSQL have no
shared commit protocol. Committing Kafka before provider acknowledgement was
rejected because it loses events. A volatile or Typesense-only enterprise
checkpoint was rejected because it cannot fence multiple worker replicas with
the required transaction semantics. One Kafka event per target was rejected
because it fragments logical idempotency and cutover evidence.

## Verification

Tests cover crash/replay boundaries, duplicate acknowledgement, target-partial
progress, stale sequences, tombstones, concurrent identity allocation, HWM
commit ordering, and DLQ commit sequencing. The live harness proves the path on
PostgreSQL, Kafka, a real worker, and two Typesense targets. These checks prove
the stated at-least-once contract and intentionally do not assert exactly once.
