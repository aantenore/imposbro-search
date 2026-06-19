# Patterns & Best Practices

This document describes the architectural patterns, conventions, and best practices used in IMPOSBRO Search. It is intended for contributors and maintainers.

---

## 1. Backend (Query API)

### 1.1 Dependency injection

Dependencies (FederationService, StateManager, KafkaService, SyncConfigNotifier) are **set on `app.state`** in the lifespan handler. The `app/deps.py` module exposes dependency functions that read from `request.app.state`, so routers use `Depends(get_federation_service)` etc. without holding a reference to a placeholder.

- **Pattern**: Lifespan sets `app.state.federation_service`, `app.state.kafka_service`, etc.; `deps.py` provides `get_federation_service(request) -> request.app.state.federation_service`.
- **Testing**: Set `TESTING=1` to use a test lifespan that populates `app.state` with mocks; use `with TestClient(app) as client` so the lifespan runs and state is available.

### 1.2 Configuration

- **Settings**: All configuration via `pydantic_settings.BaseSettings` in `app/settings.py`. Required env vars fail fast at import; optional ones have defaults (e.g. `CORS_ORIGINS`, `DEFAULT_DATA2_*`).
- **Constants**: Version, default ports, and validation patterns live in `app/constants.py` to avoid magic numbers and duplication.

### 1.3 Security

- **API keys**: Never logged in full. The admin endpoint `GET /admin/federation/clusters` returns **masked** API keys (e.g. `****key`); full keys are only sent when registering a cluster (POST body).
- **Path parameters**: Collection and cluster names are validated with `Path(..., pattern=NAME_PATTERN)` so only alphanumeric, hyphen, and underscore are allowed (Typesense-compatible, reduces injection risk).
- **CORS**: Enabled only when `CORS_ORIGINS` is set; use explicit origins in production (e.g. `https://admin.example.com`).
- **Admin API key**: If `ADMIN_API_KEY` is set, it remains the superuser fallback for `/admin/*`. When unset, admin endpoints are available only if `ALLOW_UNAUTHENTICATED_ADMIN=true` or a `SCOPED_API_KEYS` / OIDC claim grants the specific admin sub-scope.
- **Data-plane API key**: If `DATA_API_KEY` is set, `/ingest/*` and `/search/*` require `X-API-Key: <value>` or `Authorization: Bearer <value>`. When unset, data endpoints are available only if `ALLOW_UNAUTHENTICATED_DATA=true` or `SCOPED_API_KEYS` grants the required endpoint scope.
- **Scoped API keys**: Prefer `SCOPED_API_KEYS` for least-privilege clients. Supported scopes are `admin`, admin subscopes (`admin:read`, `admin:write`, `admin:backup`, `admin:restore`, `admin:internal`), `search`, `ingest`, coarse `data` (search+ingest), `*`, and collection patterns such as `search:products_*`, `ingest:orders_*`, or `data:tenant_a_*`. Apply collection-aware dependencies (`require_search_collection_api_key`, `require_ingest_collection_api_key`) instead of broad router-level data-plane auth when adding collection routes.
- **OIDC/JWT bearer auth**: When `OIDC_ENABLED=true`, validate JWT signature, issuer, audience, timestamps, and configured asymmetric algorithms before mapping token claims to internal scopes. Keep API-key checks first for migration, and never persist raw token subjects in audit logs.
- **Tenant policy**: Use `AUTHZ_COLLECTION_POLICIES` for collection-level tenant enforcement. Tenant search filters and ingest validation/injection must happen server-side in `auth.py`, not in the Admin UI or client code.
- **Audit log**: Successful admin mutations are recorded in `_imposbro_audit_log` with hashed actor identifiers and safe metadata only.

### 1.4 Error handling and HTTP status

- **400**: Bad request (e.g. missing document `id`, invalid path param).
- **404**: Resource not found (collection, cluster, or routing target).
- **409**: Conflict (e.g. cluster already registered).
- **503**: Service unavailable (e.g. no target cluster for document, dependency down). Used when the operation cannot be fulfilled right now.

Routers catch service-level `ValueError` and map to appropriate status codes; unexpected exceptions are logged and returned as 500.
Search returns `503` when every target cluster fails. If at least one cluster responds, the response remains `200` but includes `partial: true` and `failed_clusters` so clients can display degraded results honestly.

### 1.5 Logging

- Use the module logger: `logger = logging.getLogger(__name__)`.
- Prefer `logger.info("message: %s", value)` over f-strings for structured logging.
- Do not log credentials or full API keys.

### 1.6 State and consistency

- **State store**: Federation config, desired collection schemas, per-cluster collection aliases, and routing rules are persisted in the internal Typesense cluster (`StateManager`). All admin changes are saved and then broadcast via Redis Pub/Sub so other API instances reload config.
- **Config sync source id**: Query API instances publish Redis config-sync notifications with a source id and ignore their own messages. This prevents a writer from immediately reloading a stale state-store read while still allowing other replicas to converge. Override `CONFIG_SYNC_SOURCE_ID` only when a stable process identity is required.
- **Schema reconciliation**: `FederationService.collection_schemas` is the desired schema state. Creating a collection records the schema, registering a new cluster backfills those schemas, and `POST /admin/collections/reconcile` idempotently recreates missing schemas after operational recovery.
- **Alias portability**: `FederationService.collection_aliases` is the desired per-cluster alias state. Alias upsert/delete must persist this map, and state import applies aliases after schema reconciliation so disaster recovery can recreate live Typesense aliases.
- **Control-plane backup/restore**: `GET /admin/state/export` masks cluster API keys by default; restore-ready exports require `include_secrets=true` and must be stored as secrets. `POST /admin/state/import` is dry-run by default and only mutates runtime/persisted state with `?apply=true`.
- **Control-plane mutation safety**: Admin mutations that change runtime federation state must snapshot the in-memory state before mutating and roll it back if `StateManager.save_state()` fails. Do not leave one Query API replica running config that was not persisted.
- **Smart Producer**: The Query API decides the target cluster for each document and puts it in the Kafka message; the indexing service only executes that decision. Routing logic lives in one place.
- **Global search merge**: Gateway-side merge must use the same comparator for sorting and deduplication. Complex shard-local sorts should be rejected until the gateway can merge them exactly. Vector-only results should merge on `_vector_distance:asc` when `text_match` is absent.
- **Search pagination**: Fetch one extra candidate beyond the requested `offset + limit` / `page * per_page` window so `has_more` and `next_offset` are based on an actual extra merged hit rather than a full page guess. When fan-out duplicates are observed, report deduplicated counts rather than inflated raw cluster totals.
- **Advanced search payloads**: Keep `GET /search/{collection}` for simple queries and use `POST /search/{collection}` for semantic/vector/hybrid params so long `vector_query` payloads are not forced into URLs.
- **Runtime smoke**: Use `make smoke-docker` after changes that affect Docker wiring, collection schemas, Kafka ingest, search merge, vector search, or the Admin UI proxy. Use `make smoke-docker-outage` after changes that affect readiness, cluster failure handling, or partial federated results. Use `make smoke-docker-load` after changes that affect Kafka throughput, indexing convergence, or search pagination/sorting under more than a couple of documents. Use `make smoke-docker-state` after changes that affect control-plane export/import, desired schema reconciliation, or disaster-recovery workflows. Use `make smoke-docker-alias` after changes that affect aliases or zero-downtime reindexing workflows. Use `make smoke-docker-scale` after changes that affect horizontal scaling, Docker networking, Kafka consumer behavior, rolling restarts, or lag budgets. Use `make smoke-vector` / `make smoke-outage` / `make smoke-load` / `make smoke-state` / `make smoke-alias` / `make smoke-scale` when the stack is already running.
- **Kubernetes autoscaling**: Use HPA for request-serving workloads when CPU/memory is an adequate signal. Prefer KEDA Kafka lag scaling for indexing workers. Never enable both HPA and KEDA for the same indexing Deployment; the chart intentionally fails this configuration.
- **PodDisruptionBudget**: Keep PDB opt-in and per-workload. Use `minAvailable` for small replica sets and `maxUnavailable` for larger pools; avoid strict PDBs on single-replica workloads unless blocking voluntary disruption is intentional.
- **Topology spread**: Keep topology spread constraints per workload and align `labelSelector` with the component label. Prefer soft `ScheduleAnyway` constraints until capacity and PDB interactions are proven in the target cluster.

---

## 2. Admin UI (Next.js)

### 2.1 API client

- **Single entry point**: All backend calls go through `lib/api.js` (`request()`, `api.*`). This ensures consistent error handling and a single place to change base URL or headers.
- **Errors**: Non-2xx and network errors are thrown as `ApiError` (message, status, data). The UI can show `error.message` and optionally `error.status` or `error.data`.
- **Non-JSON responses**: If the proxy or backend returns HTML or plain text (e.g. 502), the client parses safely and exposes a generic message instead of throwing on `response.json()`.

### 2.2 Proxy

- Requests to `/api/*` are handled by the Route Handler `app/api/[[...path]]/route.js`, which forwards to `INTERNAL_QUERY_API_URL`. This allows the Admin UI to run on a different host/port (e.g. Docker) without CORS or exposing the backend URL to the browser.
- In production, when the proxy injects server-side API keys, require a trusted upstream identity header (`ADMIN_UI_PROXY_TRUSTED_HEADER`, optionally matched with `ADMIN_UI_PROXY_TRUSTED_VALUE`) from an authenticated ingress/gateway. Do not expose the Admin UI directly with injectable server-side credentials.
- When `ADMIN_UI_OIDC_ENABLED=true`, browser login uses OIDC Authorization Code + PKCE through `/api/auth/login` and `/api/auth/callback`. Store tokens only in sealed HttpOnly cookies, keep `return_to` relative to prevent open redirects, and keep Query API `OIDC_ENABLED=true` so it validates the proxied bearer token.

### 2.3 Notifications

- The `useNotification` hook provides `showSuccess`, `showError`, `showInfo`, and `clearNotification`. Use it for user feedback after API calls; avoid `alert()`.

---

## 3. Indexing service

### 3.1 Smart Producer

- The consumer **does not** re-compute routing. It reads `target_cluster` from the message and indexes into that cluster. All routing logic stays in the Query API.

### 3.2 Resilience

- Kafka connection and consumer loop use retries and a `shutdown_requested` flag for graceful shutdown (SIGTERM/SIGINT).
- If a cluster is missing (e.g. removed after message was produced), the consumer refreshes cluster config and retries. Persistent poison messages are published to a dead-letter topic before the source offset is committed.
- The consumer subscription pattern must exclude `<KAFKA_TOPIC_PREFIX>_dlq`. DLQ topics contain wrapper payloads, not normal ingest payloads, and must not be recursively consumed by the indexing worker.

### 3.3 Configuration

- Cluster list is fetched from the Query API at startup (`/admin/federation/clusters`). The virtual `"default"` entry (internal HA cluster) is skipped when building Typesense clients.

---

## 4. Delivery

- **Helm fail-fast**: The chart deploys only IMPOSBRO application workloads. Kafka, Redis, and Typesense must be supplied explicitly through values. Keep Helm validation strict: reject placeholder images, mutable `:latest` tags, missing external service endpoints, missing required API keys, and missing trusted Admin UI proxy identity headers when server-side key injection is enabled.
- **NetworkPolicy**: Keep NetworkPolicy opt-in and cluster-specific. Do not hardcode ingress-controller, Prometheus, DNS, Kafka, Redis, Typesense, or OIDC network selectors in templates. Model these boundaries through Helm values and verify rendering with production-like labels.
- **Compose exposure**: Docker Compose is a local development stack. Bind published ports to `127.0.0.1` by default and keep unauthenticated bypasses local-only.
- **Hosted gates**: Pull requests and pushes to `main` should run the same checks as `make ci`: unit/API/UI/lint/build/Compose/Helm. Runtime Docker smoke should run manually or on a schedule because it validates the real Kafka/Typesense path and takes longer than the normal PR gate. Creating GitHub workflow files requires credentials with the `workflow` scope.

---

## 5. General

### 5.1 Versioning

- API version is in `query_api/app/constants.py` (`VERSION`). Used in FastAPI app, root and health responses, and logs. Bump in one place for releases.

### 5.2 Health checks

- `GET /`: Minimal liveness (service name, version, status).
- `GET /health`: Detailed dependency health with cluster count, Redis, Kafka, and per-data-cluster readiness. It returns JSON even when degraded.
- `GET /ready`: Readiness probe for orchestrators. Returns HTTP 503 until all required dependencies and data clusters are ready.

### 5.3 Kubernetes deployment

- Helm defaults run workloads with a service account that does not mount an API token unless explicitly enabled.
- Query API probes use `/ready` for startup/readiness and `/` for liveness; Admin UI probes `/`.
- Workload resources, replica counts, probes, security context, pod labels/annotations, node selectors, affinity, and tolerations are values-driven.
- Worker processes should not get fake HTTP probes. Add `indexingService.livenessProbe` only when a real process-level healthcheck is available.

### 5.4 Metrics

- Prometheus metrics are exposed via `prometheus_fastapi_instrumentator`. Custom counters (e.g. `documents_ingested_total`) are defined in the router that performs the action.
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
