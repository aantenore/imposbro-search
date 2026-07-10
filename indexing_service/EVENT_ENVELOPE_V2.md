# Indexing Event Envelope v2

This is the producer/consumer contract required by the enterprise indexing
worker. The Query API producer must publish one logical Kafka record per
document mutation, not one record per physical target.

## Payload

```json
{
  "envelope_version": 2,
  "event_id": "01JZZZZZZZZZZZZZZZZZZZZZZZ",
  "identity": {
    "tenant_id": "tenant-a",
    "collection": "products",
    "document_id": "doc-123"
  },
  "document_version": 42,
  "sequence": 105,
  "operation": "upsert",
  "routing_revision": 17,
  "rollout_id": "rollout-2026-07-eu",
  "target_clusters": ["cluster-eu-old", "cluster-eu-new"],
  "occurred_at": "2026-07-10T08:00:00Z",
  "trace": {
    "request_id": "request-123",
    "correlation_id": "operation-456",
    "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
  },
  "document": {"id": "doc-123", "name": "Product"}
}
```

`operation` is `upsert`, `delete`, or `tombstone`. Upsert requires `document`
and its `id` must equal `identity.document_id`. Delete and tombstone reject a
document body and may carry `delete_filter` when tenant-safe filtered deletion
is required. A v2 delete filter is deliberately not a general Typesense filter;
the worker accepts only one of these exact producer-generated shapes:

```text
(id:=<document_id>) && <tenant_field>:=<tenant>
(id:=<document_id>) && <tenant_field>:=[<tenant>,<tenant>]
```

This prevents a malformed internal event from turning one document mutation
into an unconstrained bulk delete. All integer positions are positive signed
64-bit values because the durable PostgreSQL checkpoint schema uses `BIGINT`.
When present, `trace.traceparent` must use the W3C base layout, must not use the
forbidden `ff` version, and must contain non-zero trace and parent identifiers.
It is emitted in apply/error logs but never used as a Prometheus label.

## Kafka key and ordering

The Kafka record key is the UTF-8 byte sequence:

```text
tenant_id + U+001F + collection + U+001F + document_id
```

For the example above it is `tenant-a\x1fproducts\x1fdoc-123`. The worker
rejects a missing or mismatched key. This keeps every mutation for one tenant
document in one Kafka partition.

The producer must preserve these invariants:

- `event_id` remains stable across producer retries.
- `sequence` strictly increases for every logical identity mutation.
- `document_version` also strictly increases; a recreation after a tombstone
  requires a new version and sequence.
- Versions and sequences come from a durable authoritative source. A wall-clock
  fallback across replicas is not a production sequencer, and caller-supplied
  values are safe only when those callers are explicitly trusted to own versioning.
- One event contains the complete target set selected by `routing_revision` and
  the current rollout phase.
- A producer retry republishes the same mutation and Kafka key. The worker
  persists a canonical mutation digest and rejects reuse of an event ID with a
  changed document, target set, version, operation, or routing metadata.
  `trace` and `occurred_at` may differ on a transport retry because they are
  diagnostic metadata and are deliberately excluded from that digest. Target
  order is also non-semantic; the target set is canonicalized for comparison.

Contradictory sequence/version pairs or reuse of an event ID with different
metadata are quarantined instead of being guessed at by the worker.

## Consumer guarantees

The enterprise worker stores one PostgreSQL checkpoint per logical identity and
physical target. Before preflight it opens a transaction, configures a bounded
`lock_timeout`, and acquires transaction-scoped advisory locks for every target
in deterministic checkpoint-key order. Those fences remain held through every
Typesense side effect and checkpoint write. A concurrent stale consumer can
only continue after the current transaction commits; it then observes the newer
checkpoint and becomes a no-op instead of overwriting the document.

The worker preflights every named target before any new write and rejects
contradictory metadata. It advances each successfully completed target
checkpoint. If a later target fails, successful checkpoint writes are committed
as partial progress so the retry resumes only incomplete targets. If the process
crashes before PostgreSQL commit, the checkpoint transaction rolls back and the
same idempotent Typesense upsert/delete can run again. If any target has already
advanced past the event, writes to its other incomplete targets are suppressed.
Older events and pre-tombstone upserts are observable no-ops.

This provides durable **at-least-once processing with idempotent convergence**.
It does not claim exactly-once delivery: a crash after the Typesense document
write but before the checkpoint can repeat that same idempotent write. Kafka,
Typesense document mutation, and checkpoint mutation are not one transaction.

Startup requires the PostgreSQL dialect with the `psycopg` driver, the exact
Alembic head, all checkpoint columns, the expected primary key, and successful
transactional advisory-lock/read/write access. Any mismatch keeps readiness
false and prevents Kafka consumption. `CONTROL_PLANE_DATABASE_URL` is shared
with the migrated control-plane database; credentials remain Secret-backed.

Two data-model invariants remain producer/control-plane responsibilities:

- A tombstone must target every active or historical rollout cluster that may
  still contain the document. Otherwise no per-target checkpoint can delete a
  copy the producer did not name.
- Typesense addresses a document by `collection + document.id`, while the event
  order key also contains `tenant_id`. Therefore `document.id` must remain
  globally unique inside a physical collection, or the whole platform must
  adopt a tenant-aware physical-ID mapping consistently for ingest, read,
  search, and delete. The worker cannot safely introduce that mapping alone.

`INDEXING_CHECKPOINT_BACKEND=postgres` is mandatory in production and enterprise.
The memory adapter has the same deterministic event-scoped locking semantics but
is volatile and limited to one process. The legacy Typesense checkpoint adapter
has only an in-process fence and is therefore development-only; both alternatives
require explicit opt-in and are rejected by production profiles.

The PostgreSQL transaction is intentionally held open while Typesense is called,
so `INDEXING_CHECKPOINT_LOCK_TIMEOUT_MS`, connection capacity, Kafka retries, and
the maximum fan-out latency must be sized together. Advisory keys use a signed
64-bit projection of the SHA-256 checkpoint key; a theoretical collision only
serializes unrelated documents conservatively. PostgreSQL and Typesense still
do not share one transaction, so the guarantee remains at-least-once rather than
exactly-once.

The producer outbox assigns a separate, gap-tolerant monotonic
`global_position` using a PostgreSQL `GENERATED ALWAYS` identity. Reading the
high-water mark briefly takes a table barrier that drains existing outbox
writers and holds new writers for the duration of the `MAX(global_position)`
read. This prevents a transaction that allocated an older identity value from
committing below an already persisted restart cursor. Repair jobs read the
latest event per identity in a position window and use `prepare_latest_repair`
to clone the latest durable mutation with new routing metadata under the
identity-head lock. No snapshot document body is trusted or backfilled by this
contract.

## Kafka transport security

The consumer and its DLQ producer share `KAFKA_SECURITY_PROTOCOL`,
`KAFKA_SASL_MECHANISM`, `KAFKA_SASL_USERNAME`, `KAFKA_SASL_PASSWORD`,
`KAFKA_SSL_CAFILE`, `KAFKA_SSL_CERTFILE`, `KAFKA_SSL_KEYFILE`, and
`KAFKA_SSL_CHECK_HOSTNAME`. Partial SASL/mTLS settings, unsupported mechanisms,
missing certificate files, and contradictory plaintext/TLS configuration fail
startup. Enterprise deployment should use `SASL_SSL`, certificate validation,
and Secret-backed credentials.

The worker polls one record at a time by default (`KAFKA_MAX_POLL_RECORDS=1`) to
bound rebalance overlap. `KAFKA_MAX_POLL_INTERVAL_MS` must still be greater than
the worst-case fan-out plus checkpoint retry time for the deployment.

## Compatibility and rollout

Legacy payloads lack event ID, tenant identity, sequence, routing revision and
tombstone protection. They are rejected by default. Set
`INDEXING_ALLOW_LEGACY_EVENTS=true` only during a controlled development or
migration window; never use it as the enterprise steady state.

The producer must move to v2 before production workers disable the temporary
compatibility switch. Existing per-target producer messages cannot be made
enterprise-idempotent solely inside the consumer.
