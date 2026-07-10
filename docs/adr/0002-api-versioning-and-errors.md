# ADR 0002: URI-major API versioning and Problem Details

- **Status:** accepted
- **Date:** 2026-07-10
- **Decision owners:** IMPOSBRO maintainers

## Context

The original data-plane and administration endpoints are unversioned and expose
FastAPI's default `{"detail": ...}` failure body. That shape is convenient but
does not provide a stable machine code, request correlation, or a compatibility
boundary for enterprise clients. Removing or silently changing those endpoints
would also break existing integrations and the Admin UI.

## Decision

`/api/v1` is the authoritative first stable contract. The existing routers are
mounted beneath that prefix and remain available at their historical paths as
compatibility aliases. Legacy aliases emit `Deprecation: true` and a `Link` to
the successor URI; no removal date is advertised until a separately approved
sunset plan exists.

Versioned failures use `application/problem+json` with the RFC 9457 core fields
plus stable `code`, `request_id`, and `api_version` extensions. Validation
errors expose normalized locations and codes. Unexpected exceptions are
redacted. Historical aliases preserve the old `detail` body so adoption does
not become a flag day.

Every response carries `X-API-Version` and the configured request-id header.
Operation IDs derive deterministically from route name, method, and path. The
canonical versioned OpenAPI document is available at `/api/v1/openapi.json` and
is stored in `contracts/openapi-v1.json` as a reviewed baseline.

## Compatibility policy

- Additive optional request fields, response fields, endpoints, and enum values
  require a reviewed OpenAPI baseline update and release note.
- Removing or renaming a path, operation, required field, status contract, or
  authentication scope requires a new major URI version.
- Newly required request fields are breaking unless an old behavior remains
  available for the lifetime of v1.
- Bug fixes may tighten rejection of input that was already outside the
  documented schema, but require regression tests and release notes.
- The CI drift gate intentionally rejects every unreviewed schema change. The
  maintainer runs `scripts/ci/openapi-contract.py --write`, reviews the semantic
  diff, and records compatibility impact before merging.
- `contracts/openapi-v1.previous.json` is the immutable last-release baseline.
  `scripts/ci/openapi-compat.py` conservatively rejects removed paths,
  operations, parameters, responses, properties or enum values, newly required
  input, tightened constraints, changed operation IDs, and changed security.
  Promotion of the reviewed current contract to the previous-release baseline
  happens only as an explicit release step, never automatically in a PR.

## Alternatives rejected

Header-only versioning was rejected because caches, proxies, browser tooling,
and operational logs handle explicit paths more reliably. Replacing legacy
errors in place was rejected because it would break clients that read
`detail`. Keeping only unversioned routes was rejected because it provides no
bounded compatibility surface.

## Verification and rollback

Contract tests exercise versioned success, validation, not-found behavior,
legacy compatibility headers, unique operation IDs, exact baseline equality,
and compatibility against the last-release baseline. A deployment can roll
back without a data migration because v1 and legacy aliases share the same
application handlers. Once a v1-only client is released, rollback must target a
build that still mounts `/api/v1`. Source checks do not replace a downstream
consumer smoke tied to the release candidate.
