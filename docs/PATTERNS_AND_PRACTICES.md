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
- **Admin API key**: If `ADMIN_API_KEY` is set, all `/admin/*` requests must include `X-API-Key: <value>` or `Authorization: Bearer <value>`. When unset, admin endpoints are unprotected (suitable for dev or behind a gateway).

### 1.4 Error handling and HTTP status

- **400**: Bad request (e.g. missing document `id`, invalid path param).
- **404**: Resource not found (collection, cluster, or routing target).
- **409**: Conflict (e.g. cluster already registered).
- **503**: Service unavailable (e.g. no target cluster for document, dependency down). Used when the operation cannot be fulfilled right now.

Routers catch service-level `ValueError` and map to appropriate status codes; unexpected exceptions are logged and returned as 500.

### 1.5 Logging

- Use the module logger: `logger = logging.getLogger(__name__)`.
- Prefer `logger.info("message: %s", value)` over f-strings for structured logging.
- Do not log credentials or full API keys.

### 1.6 State and consistency

- **State store**: Federation config and routing rules are persisted in the internal Typesense cluster (`StateManager`). All admin changes are saved and then broadcast via Redis Pub/Sub so other API instances reload config.
- **Smart Producer**: The Query API decides the target cluster for each document and puts it in the Kafka message; the indexing service only executes that decision. Routing logic lives in one place.

---

## 2. Admin UI (Next.js)

### 2.1 API client

- **Single entry point**: All backend calls go through `lib/api.js` (`request()`, `api.*`). This ensures consistent error handling and a single place to change base URL or headers.
- **Errors**: Non-2xx and network errors are thrown as `ApiError` (message, status, data). The UI can show `error.message` and optionally `error.status` or `error.data`.
- **Non-JSON responses**: If the proxy or backend returns HTML or plain text (e.g. 502), the client parses safely and exposes a generic message instead of throwing on `response.json()`.

### 2.2 Proxy

- Requests to `/api/*` are handled by the Route Handler `app/api/[[...path]]/route.js`, which forwards to `INTERNAL_QUERY_API_URL`. This allows the Admin UI to run on a different host/port (e.g. Docker) without CORS or exposing the backend URL to the browser.

### 2.3 Notifications

- The `useNotification` hook provides `showSuccess`, `showError`, `showInfo`, and `clearNotification`. Use it for user feedback after API calls; avoid `alert()`.

---

## 3. Indexing service

### 3.1 Smart Producer

- The consumer **does not** re-compute routing. It reads `target_cluster` from the message and indexes into that cluster. All routing logic stays in the Query API.

### 3.2 Resilience

- Kafka connection and consumer loop use retries and a `shutdown_requested` flag for graceful shutdown (SIGTERM/SIGINT).
- If a cluster is missing (e.g. removed after message was produced), the document is logged as failed and the consumer continues with the next message.

### 3.3 Configuration

- Cluster list is fetched from the Query API at startup (`/admin/federation/clusters`). The virtual `"default"` entry (internal HA cluster) is skipped when building Typesense clients.

---

## 4. General

### 4.1 Versioning

- API version is in `query_api/app/constants.py` (`VERSION`). Used in FastAPI app, root and health responses, and logs. Bump in one place for releases.

### 4.2 Health checks

- `GET /`: Minimal liveness (service name, version, status).
- `GET /health`: Detailed health with cluster count, Redis and Kafka connectivity. Returns `status: degraded` when clusters are zero or Redis is down, so load balancers can optionally treat it as unhealthy.

### 4.3 Metrics

- Prometheus metrics are exposed via `prometheus_fastapi_instrumentator`. Custom counters (e.g. `documents_ingested_total`) are defined in the router that performs the action.

### 4.4 Documentation

- README: User-facing setup, configuration, and deployment.
- CONTRIBUTING: How to run tests, code style, PR process.
- PROJECT_ANALYSIS: High-level analysis, improvements done, roadmap.
- This file: Patterns and best practices for maintainers.

---

## 5. Checklist for new changes

- [ ] New config → add to `settings.py` and `.env.example` with a short comment.
- [ ] New path params → validate with `Path(..., pattern=NAME_PATTERN)` if they are names/identifiers.
- [ ] New admin endpoints that return credentials → mask or omit secrets.
- [ ] Logging → no credentials; use `%s` or `logger.info("msg %s", x)` where possible.
- [ ] Errors → use HTTP status codes consistently (4xx client, 5xx server, 503 when dependency unavailable).
- [ ] Tests → add or extend pytest in `query_api/tests` for new API behaviour; use `TESTING=1` and mocks.
