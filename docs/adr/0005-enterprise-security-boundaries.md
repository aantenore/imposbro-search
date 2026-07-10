# ADR 0005: Enterprise identity, tenant, secret, and transport boundaries

- **Status:** accepted
- **Date:** 2026-07-10
- **Decision owners:** IMPOSBRO maintainers

## Context

The platform crosses browser, API, database, broker, cache, and search-cluster
trust boundaries. Development API keys and plaintext local endpoints are useful
for a laptop but are not an acceptable implicit production fallback. Tenant
filters, authentication, secret delivery, and TLS must be enforced by the
trusted server even when the Admin UI or ingress is compromised.

## Decision

`DEPLOYMENT_PROFILE=enterprise` is a fail-closed contract rather than a feature
flag. Startup rejects the profile unless it has:

- OIDC signature, issuer, audience, time, and algorithm validation with exactly
  one trusted JWKS or public-key source;
- authenticated admin and data paths, scoped permissions, deny-by-default
  collection tenant policy, and no API-key tenant bypass;
- distributed Redis rate limiting that fails closed;
- PostgreSQL authoritative state/event/checkpoint stores;
- HTTPS Typesense, verified PostgreSQL TLS, Redis TLS, Kafka TLS with hostname
  verification and configured SASL or workload authentication;
- structured redacted logs, strict readiness, audit, and external secret
  injection.

Authorization is evaluated in the Query API. The browser cannot choose a
tenant filter: search and delete filters are composed server-side, ingest must
match the caller's tenant claims, and every durable event carries one explicit
tenant identity. Collection patterns and admin capabilities are server-checked
scopes.

The Admin UI uses OIDC Authorization Code with PKCE, state, nonce, verified ID
tokens, an authenticated-encrypted HttpOnly cookie, bounded absolute/idle
lifetimes, same-origin mutation checks, and POST logout. Production server-key
injection is disabled by default; a legacy trusted-header mode is an explicit
temporary compatibility risk and is forbidden by the enterprise chart.

Kubernetes manifests reference a pre-existing or External Secrets-managed
Secret. Credential rotation changes pod annotations/configuration and never
requires rebuilding an image. Containers run as a fixed non-root user with a
read-only root filesystem, no privilege escalation/capabilities, bounded
resources, disabled service-account token mounting, restrictive network
policies, and digest-pinned images.

## Audit and data exposure

Control-plane mutation, revision, actor, request ID, outcome, and safe metadata
commit transactionally with state or remain in a durable outbox. Audit exports
are ordered, hash-chain verifiable, bounded, and `no-store`. Credentials,
tokens, document bodies, database URLs, and raw provider errors are excluded
from logs, traces, metrics, problems, and broad operator responses.

Backups are encrypted before leaving a mode-0600 temporary directory and are
restored only into an explicitly empty isolated target with checksum, manifest,
schema, and confirmation guards. Deployment owners remain responsible for KMS,
immutable storage, retention, and independent access review.

## Residual deployment responsibilities

The source repository cannot prove ingress header stripping, workload identity,
identity-provider lifecycle, broker ACLs, secret-manager IAM, network egress,
WORM audit storage, or certificate rotation in a specific environment. The
threat model marks those as deployment evidence, and the enterprise release
gate rejects absent drills rather than converting them into source-code claims.

## Alternatives rejected

Client-supplied tenant filters were rejected as an authorization bypass.
Production development defaults were rejected because misconfiguration would
silently expose data. Storing raw secrets in Helm values or ConfigMaps was
rejected because release artifacts and cluster read access would disclose them.
Trusting a spoofable browser header as the primary identity was rejected in
favor of end-to-end OIDC; legacy mode is isolated and opt-in only.

## Verification

Negative authentication, scope, collection, tenant, CSRF, session, TLS, secret
rendering, network-policy, and error-redaction tests are required. The ASVS
mapping records implementation versus deployment status. A live release adds
OIDC/JWKS rotation, secret rotation, TLS/SASL, ingress, and cross-tenant smoke
evidence tied to the exact commit.
