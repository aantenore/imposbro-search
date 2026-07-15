# ADR 0001: Transactional control-plane store

- **Status:** accepted
- **Date:** 2026-07-10
- **Decision owners:** IMPOSBRO maintainers

## Context

The current control plane stores one JSON document in an internal Typesense
cluster. Every admin mutation upserts the entire document, then writes audit and
publishes a Redis notification as separate side effects. Two API replicas can
therefore read the same state and overwrite each other. A process can also fail
between state, audit, and notification writes. Redis Pub/Sub notifications are
ephemeral, so a disconnected replica has no authoritative catch-up cursor.

Typesense supports create, update, emplace, and upsert document operations, but
its document contract does not expose a fencing token or compare-and-swap write
for this use case. See the official
[Typesense document API](https://typesense.org/docs/27.1/api/documents.html).

## Decision

Use PostgreSQL as the production control-plane source of truth behind a narrow
`ControlPlaneStore` application port.

One database transaction must:

1. update the singleton state row only when `revision = expected_revision`;
2. increment the committed revision;
3. append the operator audit event;
4. append a transactional outbox event for replica convergence.

An update affecting zero rows is a stale write and returns `409 Conflict`.
Workflow records that require exclusive transitions use row-level locking. This
matches PostgreSQL's documented transactional and
[row-locking semantics](https://www.postgresql.org/docs/17/explicit-locking.html).

The supported implementations are intentionally bounded:

- `PostgresControlPlaneStore`: production implementation and only backend that
  can satisfy the enterprise acceptance contract.
- `InMemoryControlPlaneStore`: deterministic unit-test implementation.
- `TypesenseLegacyStateReader`: read-only migration adapter for the existing
  `config_v1` document and v1 backup snapshots.

Redis remains useful for low-latency wake-up notifications, rate limiting, and
cache coordination. It is not the authority for state ordering. Replicas poll
the committed revision/outbox so missed Pub/Sub messages cannot cause permanent
divergence.

## Options rejected

### Typesense plus a Redis distributed lock

Rejected because the lock lease is not a fencing token understood by Typesense.
State, audit, and notification still cannot commit atomically, so lease expiry
or process failure can reintroduce split-brain or missing audit evidence.

### Redis as the state database

Redis transactions or Lua can implement atomic compare-and-swap and Streams can
retain events. This is operationally smaller, but makes durable state history,
schema migration, audit queries, retention, backup, and reporting depend on a
bespoke Redis persistence contract. Redis remains an excellent accelerator but
is the weaker system of record for this control plane.

### Unbounded pluggable databases

Rejected as abstraction theatre. The application owns a small port so domain
logic is testable, but production semantics are defined by PostgreSQL. A new
backend is accepted only when it proves the same atomic state + audit + outbox
contract with the integration suite.

## Migration

1. Deploy the PostgreSQL schema without changing reads.
2. Freeze admin mutations while keeping the data plane available. The legacy
   document has no revision/CAS, so export and cutover cannot otherwise prove
   that a concurrent mutation was retained.
3. Import the legacy Typesense state through the migration adapter with its
   initial committed revision and a migration audit event.
4. Compare canonical state digests and dry-run reconciliation.
5. Switch reads and writes to PostgreSQL through configuration and unfreeze
   admin mutations.
6. Keep the legacy document read-only for one rollback window.
7. A direct snapshot rollback is allowed only while PostgreSQL admin mutations
   remain frozen. After accepting PostgreSQL mutations, downgrade requires a
   validated PostgreSQL-to-v1 export or a forward recovery; it must never
   silently discard post-cutover revisions. Never dual-write two independent
   sources of truth.

## Consequences

- Adds a PostgreSQL dependency and versioned database migrations.
- Enables true optimistic concurrency, transactional audit/outbox, durable
  workflows, standard PITR/backup, and clearer operational ownership.
- Requires production deployments to provision PostgreSQL and run migrations
  before starting a new application revision.
- Existing v1 state and backup artifacts remain importable.

## Verification

- Two writers using the same expected revision race: exactly one commits and
  the other receives a typed conflict.
- State revision, audit row, and outbox row appear together or not at all.
- A replica missing Redis notifications converges by polling committed revision.
- Legacy import is idempotent and produces the same canonical state digest.
- Upgrade and downgrade/restore procedures run against a disposable PostgreSQL
  instance in hosted integration CI.
