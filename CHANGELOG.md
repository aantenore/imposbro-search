# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and releases use semantic versioning.

## [Unreleased]

### Added

- PostgreSQL control-plane CAS with transactional audit and durable outboxes.
- Versioned `/api/v1` contract, Problem Details, request/trace correlation, and
  compatibility gates against the previous release.
- Sequenced envelope-v2 indexing, PostgreSQL worker fencing, safe tombstones,
  HWM repair, and a guarded single-record DLQ resolver.
- Gated routing rollout lifecycle with dual-write, resumable backfill, exact
  parity, operational cutover evidence, drain, and rollback.
- Enterprise OIDC/tenant policy, transport guardrails, External Secrets Helm
  integration, hardened Kubernetes workloads, SLO alerts, dashboards, runbooks,
  encrypted backup/restore tooling, and structured evidence harnesses.
- Admin rollout console, conflict recovery, Playwright browser workflows, and
  automated WCAG A/AA checks on desktop and mobile.
- Reproducible dependency/image locks, CodeQL, container scanning, SBOM,
  provenance, keyless signing, and immutable multi-component promotion.

### Changed

- PostgreSQL is the only production/enterprise authority for control-plane,
  event, and replicated-worker checkpoint state; legacy Typesense state is a
  read-only migration source.
- Containers use digest-pinned base images and fixed non-root identities.
- The Admin UI no longer fetches build-time fonts from an external network.

### Security

- Enterprise startup denies unauthenticated access, missing tenant coverage,
  plaintext dependency transports, volatile rate limits/checkpoints, and
  unstructured logs.
- Browser mutations are same-origin protected and production server-key
  injection is disabled by default.
- Sensitive external failures, logs, traces, backup evidence, and audit exports
  are redacted and cache-disabled.
- Admin UI dependency resolution now selects the patched `brace-expansion`
  release in every active major line, `js-yaml` 4.3.0, and an explicitly
  verified `sharp` 0.35.3 override for the current Next.js runtime.

### Fixed

- Alembic migrations no longer roll back silently when the PostgreSQL advisory
  lock opens an outer SQLAlchemy transaction.
- Universal Python locks include platform-specific transitive wheels and build
  successfully with `--require-hashes` on ARM64.

[Unreleased]: https://github.com/aantenore/imposbro-search/compare/v1.0.0...HEAD
