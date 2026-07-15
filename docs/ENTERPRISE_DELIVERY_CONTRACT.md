# Enterprise Delivery Contract

## Objective

Turn IMPOSBRO Search into a self-hostable enterprise Typesense federation and
control-plane platform. The platform must support multiple replicas, concurrent
operators, safe routing migrations, tenant isolation, repeatable delivery, and
operational recovery without relying on undocumented manual database changes.

This document is the acceptance contract. A feature or green test is evidence
only for the requirement it actually exercises. The platform must not be called
enterprise-ready until every MUST requirement below has authoritative evidence.

## Product boundary

- **Primary users:** platform/search engineers, SREs, security administrators,
  and application teams consuming the data-plane API.
- **Primary workflow:** register isolated Typesense clusters, define and safely
  evolve routing policy, ingest asynchronously, search through one API, and
  diagnose or recover the platform from one operator surface.
- **Deployment target:** Kubernetes with external, highly available Kafka,
  Redis, Typesense data clusters, identity provider, secret manager, and a
  durable control-plane state store.
- **Compatibility:** existing data-plane and admin API paths remain supported
  during versioned migrations; stored v1 snapshots remain importable.
- **External evidence:** organizational certifications, a third-party
  penetration test, and a customer production rollout are release evidence,
  not claims that source code alone can manufacture.

## Acceptance threshold

Enterprise readiness requires all MUST requirements to pass, no known P0/P1
defects, no skipped critical-path verification, and a recorded rollback path for
every state or deployment migration. Static manifests are not proof of runtime
HA, recovery, security, or performance behavior.

## Requirements

| ID | Requirement | Priority | Acceptance criteria | Authoritative evidence |
|---|---|---:|---|---|
| ENT-01 | Versioned, stable API contracts | MUST | Data/admin APIs expose a version strategy, stable error shape, request correlation, compatibility policy, and generated OpenAPI contract checks. | Contract tests plus compatibility tests against the previous release. |
| ENT-02 | Transactional control-plane state | MUST | Every mutation is atomic, revisioned, and compare-and-swap protected across replicas; stale writers receive `409`; state and audit metadata cannot silently diverge. Storage is behind an explicit repository contract. | Concurrent-writer integration test against the production state backend, migration/rollback test, and state revision visible through API/metrics. |
| ENT-03 | Safe routing lifecycle | MUST | A routing change follows draft, validation, dual-write, backfill, cutover, drain, completion or rollback states. Illegal transitions fail closed. Search/delete coverage prevents hidden historical documents throughout migration. | State-machine unit tests plus live two-cluster migration, rollback, and failure-recovery smoke. |
| ENT-04 | Durable reconciliation | MUST | Missed notifications and restarted replicas converge monotonically to the latest committed revision; reconciliation is idempotent and observable. | Multi-replica test with dropped notifications, restart, and revision convergence assertions. |
| ENT-05 | Enterprise identity and authorization | MUST | OIDC signature/issuer/audience validation, least-privilege admin/data scopes, collection and tenant isolation, service identity, secure session handling, and deny-by-default production configuration are enforced server-side. | Negative authorization suite, tenant-isolation integration test, and OWASP ASVS 5.0 control mapping. |
| ENT-06 | Secrets and transport security | MUST | No production secret is stored in Git or rendered in non-secret resources; TLS is configurable for every external dependency; secret rotation does not require an image rebuild. | Secret scan, Helm negative tests, TLS integration smoke, and documented rotation drill. |
| ENT-07 | Tamper-evident operator audit | MUST | Sensitive actions record actor, request id, revision, outcome, and safe change metadata with configured retention/export. Required audit failures fail closed or enter a durable outbox. | Mutation/audit transaction tests, retention test, export test, and alert on audit delivery failures. |
| ENT-08 | Data lifecycle and recovery | MUST | Backup, restore, retention, export, delete, schema migration, and rollback ownership are documented and automated where the platform owns the data. RPO/RTO targets are configurable and tested. | Scheduled backup artifact, restore into a clean environment, deletion convergence test, and timed DR report. |
| ENT-09 | Reliability and graceful degradation | MUST | SLOs define availability, latency, ingest convergence, and control-plane correctness. Partial shard failure is explicit; dependency timeouts, retries, backoff, circuit behavior, and readiness semantics are bounded. | SLO dashboards/alerts, outage tests, retry-budget tests, and error-budget runbook. |
| ENT-10 | Observability and support diagnostics | MUST | Structured redacted logs, low-cardinality metrics, distributed traces across API-to-Kafka-to-worker boundaries, build identity, and revision identity allow an operator to diagnose a failed request without source archaeology. | Trace propagation integration test, log redaction test, metric contract test, and dashboard/runbook screenshots or rendered artifacts. |
| ENT-11 | Scalable and reproducible performance | MUST | Published workload profiles define supported cluster sizes and latency/convergence budgets; backpressure and queue lag are bounded; pagination and fan-out budgets are enforced. | Production-shaped benchmark artifact with environment metadata and threshold gate. |
| ENT-12 | Secure Kubernetes delivery | MUST | Non-root immutable containers, restricted security contexts, resource budgets, disruption/placement controls, fail-closed network boundaries, TLS ingress, immutable image digests, and external secret integration are validated. | Helm policy tests, rendered manifests, Kubernetes smoke, and restart/node-drain exercise. |
| ENT-13 | CI/CD and software supply chain | MUST | Every PR runs build, lint, unit, contract, integration, Helm, dependency, secret, SAST, and container checks as applicable. Releases produce signed immutable images, SBOMs, provenance, changelog, and rollback metadata. | Hosted CI run and release attestation linked to the exact commit. |
| ENT-14 | Test depth and failure recovery | MUST | Unit, adapter integration, API contract, browser E2E, accessibility, live dependency, multi-replica, load, DR, and selected chaos tests cover critical workflows. Flaky or skipped critical tests fail the release gate. | Requirement-to-test matrix and green hosted release gate. |
| ENT-15 | Enterprise operator UX | MUST | Admin workflows expose revision/conflict, rollout phase, audit, degraded states, recovery actions, confirmation, and actionable errors; keyboard and WCAG 2.2 AA basics are verified. | Browser E2E, automated accessibility scan, and manual keyboard smoke. |
| ENT-16 | Architecture and operations handover | MUST | ADRs define state ownership, routing migration, consistency, security boundaries, and provider contracts. Runbooks cover deploy, rollback, incident, DR, key rotation, capacity, and support ownership. | Reviewed ADRs, threat model, data lifecycle, quality-gate report, and tabletop record. |

## Architecture constraints

- Domain policy must not depend directly on FastAPI, Next.js, Redis, Kafka,
  Typesense, or a specific control-plane database client.
- Provider-specific adapters must preserve important provider semantics rather
  than hiding them behind a lowest-common-denominator wrapper.
- All environment-specific endpoints, credentials, limits, timeouts, retention,
  SLOs, and feature rollout behavior must be typed and fail-fast validated.
- Control-plane commits are the source of truth; Pub/Sub is an acceleration
  mechanism and cannot be the only convergence path.
- Every asynchronous mutation is idempotent and carries request, actor, tenant,
  schema, and routing revision metadata needed for replay and diagnosis.

## Verification matrix

| Gate | Local PR gate | Hosted PR gate | Release gate |
|---|:---:|:---:|:---:|
| Unit, lint, type/build | Required | Required | Required |
| API/OpenAPI contract | Required | Required | Required |
| Dependency and secret scan | Required | Required | Required |
| Live service integration | Optional locally | Required | Required |
| Multi-replica consistency | Optional locally | Required | Required |
| Browser E2E/accessibility | Optional locally | Required | Required |
| Helm/policy validation | Required | Required | Required |
| Container scan/SBOM/provenance | Optional locally | Required | Required |
| DR and routing migration smoke | Optional locally | Scheduled | Required |
| Production-shaped benchmark | Optional locally | Scheduled | Required |

## Delivery and rollback policy

- Changes land as reviewable commits on a protected branch through a pull
  request; required checks and review policy are repository settings.
- State format and API compatibility changes require an ADR, forward migration,
  tested rollback or restore plan, and release notes.
- A release is rejected when a critical gate is skipped, when evidence belongs
  to a different commit, or when a known P0/P1 remains open.

## Current status

The July 2026 hardening pass provides a strong production-oriented foundation,
but the contract is not yet satisfied. The first blocking implementation slice
is ENT-02 through ENT-04: transactional revisioned state, safe routing rollout,
and monotonic replica convergence. CI/supply-chain and live enterprise evidence
follow as parallel release-engineering work.
