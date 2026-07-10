# ADR 0006: Narrow provider ports without lowest-common-denominator semantics

- **Status:** accepted
- **Date:** 2026-07-10
- **Decision owners:** IMPOSBRO maintainers

## Context

IMPOSBRO coordinates PostgreSQL, Kafka, Redis, Typesense, OIDC, and Kubernetes.
Embedding those SDKs in routing or authorization policy makes the system hard
to test and replace. A universal storage or search interface, however, would
hide exactly the ordering, locking, search, and partial-failure behavior the
platform must reason about.

## Decision

Domain state and transition policy are dependency-free immutable values.
Application services depend on narrow capability-oriented ports; provider
adapters preserve provider semantics and expose typed failures and evidence.

Accepted boundaries are:

- `ControlPlaneStore`: atomic revision CAS plus audit and outbox. PostgreSQL is
  the only enterprise adapter; memory is for tests and Typesense is a read-only
  legacy source.
- `IndexingEventStore`: per-identity sequence allocation, idempotent prepare,
  ordered outbox, high-water reads, and latest-event repair. PostgreSQL is the
  production adapter.
- `CheckpointStore`: event-scoped per-target fencing and partial progress.
  PostgreSQL is mandatory for replicated workers.
- Kafka producer/operational probes: durable acknowledgement, consumer-group
  lag and DLQ position. They do not pretend to be a generic queue.
- Typesense federation/migration adapters: provider-native collection schema,
  alias, document import/export, filter, ranking, health, and partial-result
  semantics remain explicit.
- Redis notification/rate-limit adapters: acceleration and distributed quota,
  never authoritative revision storage.
- Identity and secret providers: standard OIDC claims and environment/file-
  mounted secret material behind validation, without an IdP-specific domain
  model.

Configuration supplies endpoints, credentials, timeouts, budgets, policies,
and feature modes. No environment topology or tenant name belongs in domain
code. A new adapter is accepted only when it passes the existing semantic
contract and failure-recovery tests; satisfying method signatures is not
enough.

## Failure translation

Adapters return stable application errors without leaking provider text. The
API maps those errors to versioned Problem Details and request IDs. Metrics use
bounded provider/operation labels, while logs and traces may carry safe event
or revision correlation. Retry ownership is explicit so nested SDK, HTTP, and
worker retry loops cannot multiply without a budget.

## Alternatives rejected

Direct SDK access from routers was rejected because it couples protocol,
policy, and persistence. A generic CRUD repository for every provider was
rejected because PostgreSQL CAS, Kafka acknowledgement, and Typesense search
are not interchangeable CRUD operations. Supporting arbitrary enterprise
databases by configuration alone was rejected until an adapter proves the same
transaction, ordering, backup, and operational contract.

## Consequences and verification

Provider upgrades can be tested at adapter boundaries and dependency versions
are locked. Domain unit tests stay deterministic, while production adapters
have live dependency suites. This creates more explicit interfaces and tests,
but prevents a nominally agnostic abstraction from concealing unsafe behavior.
