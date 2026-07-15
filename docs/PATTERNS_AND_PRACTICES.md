# Patterns & Best Practices

This document describes the architectural patterns, conventions, and best practices used in IMPOSBRO Search. It is intended for contributors and maintainers.

---

## 1. Backend (Query API)

### 1.1 Dependency injection

Dependencies (FederationService, StateManager, KafkaService, SyncConfigNotifier) are **set on `app.state`** in the lifespan handler. The `app/deps.py` module exposes dependency functions that read from `request.app.state`, so routers use `Depends(get_federation_service)` etc. without holding a reference to a placeholder.

- **Pattern**: Lifespan sets `app.state.federation_service`, `app.state.kafka_service`, etc.; `deps.py` provides `get_federation_service(request) -> request.app.state.federation_service`.
- **Testing**: Set `TESTING=1` to use a test lifespan that populates `app.state` with mocks; use `with TestClient(app) as client` so the lifespan runs and state is available.

### 1.2 Configuration

- **Settings**: All configuration via `pydantic_settings.BaseSettings` in `app/settings.py`. Required env vars fail fast at import; optional ones have defaults (e.g. `CORS_ORIGINS`, `DEFAULT_DATA2_*`). Typesense bootstrap transports are typed as `http|https` and default to `http` for backward compatibility.
- **Constants**: Version, default ports, and validation patterns live in `app/constants.py` to avoid magic numbers and duplication.

### 1.3 Security

- **API keys**: Never logged in full. The admin endpoint `GET /admin/federation/clusters` returns **masked** API keys (e.g. `****key`); full keys are only sent when registering a cluster (POST body).
- **Typesense transport**: Persist `protocol` with each cluster and pass it unchanged to Query API readiness/search clients and indexing workers. Enterprise requires `https`; the Helm trust bundle supplies a private CA through the standard Python/Node trust paths. Provider-specific client-certificate authentication still needs a deliberate adapter rather than hidden global behavior.
- **Path parameters**: Collection and cluster names are validated with `Path(..., pattern=NAME_PATTERN)` so only alphanumeric, hyphen, and underscore are allowed (Typesense-compatible, reduces injection risk).
- **CORS**: Enabled only when `CORS_ORIGINS` is set; use explicit origins in production (e.g. `https://admin.example.com`).
- **Admin API key**: If `ADMIN_API_KEY` is set, it remains the superuser fallback for `/admin/*`. When unset, admin endpoints are available only if `ALLOW_UNAUTHENTICATED_ADMIN=true` or a `SCOPED_API_KEYS` / OIDC claim grants the specific admin sub-scope.
- **Data-plane API key**: If `DATA_API_KEY` is set, `/ingest/*`, `/documents/*`, and `/search/*` require `X-API-Key: <value>` or `Authorization: Bearer <value>`. When unset, data endpoints are available only if `ALLOW_UNAUTHENTICATED_DATA=true` or `SCOPED_API_KEYS` grants the required endpoint scope.
- **Scoped API keys**: Prefer `SCOPED_API_KEYS` for least-privilege clients. Supported scopes are `admin`, admin subscopes (`admin:read`, `admin:write`, `admin:backup`, `admin:restore`, `admin:internal`), `search` (search/read), `ingest` (document writes/deletes), coarse `data` (search/read+ingest/delete), `*`, and collection patterns such as `search:products_*`, `ingest:orders_*`, or `data:tenant_a_*`. Apply collection-aware dependencies (`require_search_collection_api_key`, `require_ingest_collection_api_key`) instead of broad router-level data-plane auth when adding collection routes.
- **OIDC/JWT bearer auth**: When `OIDC_ENABLED=true`, validate JWT signature, issuer, audience, timestamps, and configured asymmetric algorithms before mapping token claims to internal scopes. Keep API-key checks first for migration, and never persist raw token subjects in audit logs.
- **Tenant policy**: Use `AUTHZ_COLLECTION_POLICIES` for collection-level tenant enforcement. Tenant search filters, document-read checks, ingest validation/injection, and delete filters must happen server-side in `auth.py`, not in the Admin UI or client code.
- **Tenant arrays**: Read authorization may use any-overlap semantics for intentionally shared documents, but writes must require every assigned tenant to be within the actor's authorized tenant set. Reject empty tenant collections.
- **Document identity**: Validate ingest IDs against the same conservative pattern used by GET/DELETE paths; never accept a document that the gateway cannot later address.
- **Audit log**: Enterprise mutations append actor, request, action, resource,
  revision, outcome, and safe change metadata to the transactional PostgreSQL
  audit chain. State and audit commit together; export failures use the durable
  control-plane outbox. Never place credentials or document bodies in audit
  details.

### 1.4 Error handling and HTTP status

- **400**: Bad request (e.g. missing document `id`, invalid path param).
- **404**: Resource not found (collection, cluster, or routing target).
- **409**: Conflict (for example a stale `If-Match` revision or an illegal rollout transition).
- **503**: Service unavailable (e.g. no target cluster for document, dependency down). Used when the operation cannot be fulfilled right now.

The canonical `/api/v1` surface returns RFC 9457 problem details with a stable
error code and request ID. Legacy aliases remain compatibility shims. Routers
map domain errors at the HTTP boundary; unexpected failures are redacted and
returned as generic 500 problems.
Search returns `503` when every target cluster fails. If at least one cluster responds, the response remains `200` but includes `partial: true` and `failed_clusters` so clients can display degraded results honestly.

### 1.5 Logging

- Use the structured logging adapter and event names; keep fields bounded and
  low-cardinality unless they are explicitly log-only correlation fields.
- Do not log credentials, authorization headers, document bodies, OIDC claims,
  or full API keys. Redaction tests are a release gate.
- Query API owns request correlation. It sanitizes `REQUEST_ID_HEADER` (default
  `X-Request-ID`), echoes it on responses, and forwards it to Kafka ingest
  and delete messages as `request_id`. Consumers may log request ids for diagnostics, but
  must not use them as Prometheus labels.

### 1.6 State and consistency

- **State store**: PostgreSQL is the enterprise source of truth behind the
  `ControlPlaneStore` port. One transaction advances the global revision,
  enforces compare-and-swap, persists state/audit, and appends the durable
  notification outbox. The Typesense reader exists only for versioned legacy
  migration.
- **Replica convergence**: Redis Pub/Sub is an acceleration hint, never the
  authority. Every replica polls the durable revision, installs an immutable
  federation snapshot monotonically, ignores its own wake-up, and exposes
  authoritative/applied revisions for health and metrics. A dropped Redis
  notification or process restart must still converge.
- **Schema reconciliation**: `FederationService.collection_schemas` is the desired schema state. Creating a collection records the schema, registering a new cluster backfills those schemas, and `POST /admin/collections/reconcile` idempotently recreates missing schemas after operational recovery.
- **Alias portability**: `FederationService.collection_aliases` is the desired per-cluster alias state. Alias upsert/delete must persist this map, and state import applies aliases after schema reconciliation so disaster recovery can recreate live Typesense aliases.
- **Control-plane backup/restore**: `GET /admin/state/export` masks cluster API keys by default; restore-ready exports require `include_secrets=true` and must be stored as secrets. `POST /admin/state/import` is dry-run by default and only mutates runtime/persisted state with `?apply=true`. Validate cluster protocols during import and normalize legacy snapshots without `protocol` to `http` before persisting.
- **Control-plane mutation safety**: Mutate desired state in PostgreSQL first,
  with an expected revision supplied through `If-Match`. Only install the
  committed immutable snapshot. A provider failure leaves an observable
  reconciliation operation; never compensate by silently overwriting newer
  desired state.
- **Durable producer**: The Query API allocates a monotonic per-identity
  sequence and appends one canonical v2 envelope to the PostgreSQL indexing
  outbox in the same transaction. The envelope contains every selected target,
  request/actor/tenant/schema/routing metadata, and an idempotency key. A
  dispatcher publishes it to Kafka; the worker executes the recorded decision
  and fences its PostgreSQL checkpoint before committing the consumer offset.
- **Document lifecycle delete**: Data-plane delete requests should publish `action=delete` events for every cluster that may contain the collection. Keep legacy messages backward compatible by treating missing `action` as upsert, and treat missing documents as idempotent delete no-ops in the worker.
- **Document read/export**: Read-by-id should use search/read authorization, check every candidate cluster for the collection, and enforce tenant policy server-side before returning a document. Cross-tenant matches should return not found rather than document data.
- **Global search merge**: Gateway-side merge must use the same comparator for sorting and deduplication. Complex shard-local sorts should be rejected until the gateway can merge them exactly. Vector-only results should merge on `_vector_distance:asc` when `text_match` is absent.
- **Search pagination**: Typesense returns at most 250 hits per page, so fetch larger global windows as stable, bounded shard pages. Fetch one extra candidate beyond the requested `offset + limit` / `page * per_page` window so `has_more` and `next_offset` are based on an actual extra merged hit rather than a full page guess. Require `offset`/`limit` together and do not emit a cursor that exceeds the gateway cap.
- **Search projections and counts**: Force `id` and simple sort keys into internal shard projections, merge/deduplicate, then hide fields the caller excluded. Exact unique totals are unavailable from bounded shard windows when replication is observed; label the deduplicated window as a lower bound and keep raw cluster totals explicit.
- **Advanced search payloads**: Keep `GET /search/{collection}` for simple queries and use `POST /search/{collection}` for semantic/vector/hybrid params so long `vector_query` payloads are not forced into URLs.
- **Runtime smoke**: Legacy focused Compose smokes remain useful during development. Enterprise acceptance uses `run-enterprise-e2e.sh` for exact-set PostgreSQL/Kafka/Redis/two-cluster/TLS/trace behavior, `run-live-postgres-dr.sh` for encrypted clean restore and retention, and `run-kind-enterprise-smoke.sh` for disruption/load. Run the three serially on a shared Docker daemon; missing or interrupted scenarios must fail closed.
- **Kubernetes autoscaling**: Use HPA for request-serving workloads when CPU/memory is an adequate signal. Prefer KEDA Kafka lag scaling for indexing workers. Never enable both HPA and KEDA for the same indexing Deployment; the chart intentionally fails this configuration.
- **PodDisruptionBudget**: Keep PDB per-workload. It is optional for local/single-replica profiles and mandatory with at least two replicas in enterprise. Use `minAvailable` for small sets or `maxUnavailable` for larger pools and prove the eviction API behavior.
- **Topology spread**: Keep topology spread constraints per workload and align `labelSelector` with the component label. Enterprise requires `DoNotSchedule`, `maxSkew=1`, hard anti-affinity and proven capacity/node-drain behavior; softer placement belongs only to an explicitly less strict profile.

---

## 2. Admin UI (Next.js)

### 2.1 API client

- **Single entry point**: All backend calls go through `lib/api.js` (`request()`, `api.*`). This ensures consistent error handling and a single place to change base URL or headers.
- **Errors**: Non-2xx and network errors are thrown as `ApiError` (message, status, data). The UI can show `error.message` and optionally `error.status` or `error.data`.
- **Non-JSON responses**: If the proxy or backend returns HTML or plain text (e.g. 502), the client parses safely and exposes a generic message instead of throwing on `response.json()`.

### 2.2 Proxy

- Requests to `/api/*` are handled by the Route Handler `app/api/[[...path]]/route.js`, which forwards to `INTERNAL_QUERY_API_URL`. This allows the Admin UI to run on a different host/port (e.g. Docker) without CORS or exposing the backend URL to the browser.
- Server-side API-key injection defaults to `disabled` in production/enterprise. Prefer a browser OIDC session. The `trusted-header-legacy` mode requires an additional explicit risk opt-in plus an exact ingress-owned header/value, and the BFF strips that header before forwarding. Do not expose a legacy-injection BFF directly.
- Require exact `Origin` and `Sec-Fetch-Site: same-origin` provenance on unsafe browser BFF methods, make logout POST-only, and force no-store on auth/session/proxy responses. `ADMIN_UI_PUBLIC_ORIGIN` is the authoritative HTTPS browser origin in production.
- Keep Ingress opt-in and environment-specific. Do not hardcode ingress classes, authentication annotations, TLS secret names, or public hostnames in templates; model them in values and render representative placeholders in CI.
- When `ADMIN_UI_OIDC_ENABLED=true`, browser login uses OIDC Authorization Code + PKCE through `/api/auth/login` and `/api/auth/callback`. Require a signed `id_token` and mandatory issuer; validate signature, nonce, audience, exact issuer, and timestamps before sealing the session cookie. Keep discovery/token/JWKS HTTPS, issuer-origin-bound (or explicitly allowlisted), redirect-free, and timeout-bounded. Store tokens only in sealed HttpOnly cookies, keep `return_to` relative, and keep Query API `OIDC_ENABLED=true` so it validates the proxied bearer token. The full compatibility contract is in `docs/ADMIN_UI_SECURITY.md`.

### 2.3 Notifications

- The `useNotification` hook provides `showSuccess`, `showError`, `showInfo`, and `clearNotification`. Use it for user feedback after API calls; avoid `alert()`.

---

## 3. Indexing service

### 3.1 Smart Producer

- The consumer **does not** re-compute routing. It validates the canonical v2 envelope and executes its immutable `target_clusters` decision. One envelope can fan out to multiple targets; routing policy remains in the Query API/control plane.

### 3.2 Resilience

- Kafka connections use bounded TLS/SASL-capable clients and graceful shutdown.
  A PostgreSQL advisory lock and compare-and-swap checkpoint fence each
  identity/target application; the source offset is committed only after all
  required target effects/checkpoints or a durable DLQ outcome.
- If a cluster is missing (e.g. removed after message was produced), the consumer refreshes cluster config and retries. Persistent poison messages are published to a dead-letter topic before the source offset is committed.
- The consumer subscription pattern must exclude `<KAFKA_TOPIC_PREFIX>_dlq`. DLQ topics contain wrapper payloads, not normal ingest payloads, and must not be recursively consumed by the indexing worker.

### 3.3 Configuration

- Cluster list is fetched from the Query API's authenticated internal endpoint (`/admin/federation/clusters/internal`). The worker uses each persisted `http|https` protocol when building Typesense clients; legacy entries default to `http`.

---

## 4. Delivery

- **Helm fail-fast**: The chart deploys only IMPOSBRO application workloads. PostgreSQL, Kafka, Redis, Typesense, IdP, secret manager and OTLP backend are external. Enterprise rejects placeholder/non-digest images, inline or cross-workload secrets, volatile state/checkpoints, plaintext transports, missing identity/tenant policy, permissive CORS/network/readiness, unsafe UI credential mode, absent migrations/telemetry/PDB/placement/security contexts, and incomplete release identity.
- **Helm chart tests**: Keep `scripts/test-helm-chart.py` aligned with `helm/ci-values.yaml` when adding optional Kubernetes resources. Cover the happy-path rendered resource counts plus at least one negative guardrail for each fail-fast validation.
- **NetworkPolicy**: Keep selectors/CIDRs cluster-specific, but require explicit ingress, egress and health-probe boundaries in enterprise. Do not hardcode controller, Prometheus, DNS or dependency ownership into templates; model and negatively test them through values.
- **Compose exposure**: Docker Compose is a local development stack. Bind published ports to `127.0.0.1` by default and keep unauthenticated bypasses local-only. Make targets should use `.env` when present and `.env.example` as a clean-checkout fallback for config validation.
- **Hosted gates**: Every push/PR runs locked unit, PostgreSQL CAS, OpenAPI compatibility, browser/accessibility, live dependencies/trace/routing, Helm, ops, dependency/secret/SAST and container gates. The scheduled assurance workflow adds Kind load/disruption and encrypted DR, then binds all evidence to one clean commit. Release reuses CI before signing/attesting immutable images.

---

## 5. General

### 5.1 Versioning

- API version is in `query_api/app/constants.py` (`VERSION`). Used in FastAPI app, root and health responses, and logs. Bump in one place for releases.

### 5.2 Health checks

- `GET /`: Minimal liveness (service name, version, status).
- `GET /health`: Detailed dependency health with cluster count, Redis, Kafka, and per-data-cluster readiness. It always returns JSON, including `status`, `ready`, and `readiness_policy`, even when degraded.
- `GET /ready`: Orchestrator probe controlled by `READINESS_POLICY`. Development defaults to `serving`; enterprise is forced to `strict`. Both use a cached, parallel, hard-budget dependency probe and return 503 before core initialization.

### 5.3 Kubernetes deployment

- Helm defaults run workloads with a service account that does not mount an API token unless explicitly enabled.
- Query API probes use `/ready` for startup/readiness and `/` for liveness; Admin UI probes `/`. Enterprise uses strict readiness. A serving profile is a deliberate degraded-availability decision with partial-response SLOs, not an enterprise default.
- Workload resources, replica counts, probes, security context, pod labels/annotations, node selectors, affinity, and tolerations are values-driven.
- Worker HTTP `/live` and `/ready` endpoints report process state, configuration and dependency readiness; Kubernetes probes must use those real endpoints.

### 5.4 Metrics

- Prometheus metrics are exposed via `prometheus_fastapi_instrumentator`. Custom counters (e.g. `documents_ingested_total`) are defined in the router that performs the action.
- Query API rate-limit metrics use only low-cardinality labels: action, collection, result, backend, and fail mode. Never label with actor IDs, IP addresses, API keys, raw queries, document IDs, or arbitrary error messages.
- The indexing worker exposes Prometheus metrics on `INDEXING_METRICS_PORT` when `INDEXING_METRICS_ENABLED=true`. Worker metrics include config fetches, loaded clusters, successful indexes, retries, and DLQ publications.
- Keep metric labels low-cardinality. Use collection, target cluster, source topic, and error type; never label metrics with document IDs, raw queries, API keys, or user-controlled free text.
- Keep ServiceMonitor and PrometheusRule resources opt-in because not every cluster uses Prometheus Operator. When adding metrics that imply operational action, add or update alert rules with environment-tunable thresholds.

### 5.5 Documentation

- README: User-facing setup, configuration, and deployment.
- CONTRIBUTING: How to run tests, code style, PR process.
- PROJECT_ANALYSIS: High-level analysis, improvements done, roadmap.
- This file: Patterns and best practices for maintainers.

---

## 6. Checklist for new changes

- [ ] New config → add to `settings.py` and `.env.example` with a short comment.
- [ ] New path params → validate with `Path(..., pattern=NAME_PATTERN)` if they are names/identifiers.
- [ ] New admin endpoints that return credentials → mask or omit secrets.
- [ ] Logging → no credentials; use `%s` or `logger.info("msg %s", x)` where possible.
- [ ] Errors → use HTTP status codes consistently (4xx client, 5xx server, 503 when dependency unavailable).
- [ ] Tests → add or extend pytest in `query_api/tests` for new API behaviour; use `TESTING=1` and mocks.
