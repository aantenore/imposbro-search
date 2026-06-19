# IMPOSBRO Search – Analysis and Improvements

## Current Product Verdict (2026-06-19)

IMPOSBRO Search is useful, but only with a clear wedge: **a Typesense-focused federation/control plane** for teams that want Typesense ergonomics while distributing data across multiple physical clusters by tenant, region, compliance boundary, or scaling domain.

The market already has strong alternatives:

- **Elasticsearch / OpenSearch cross-cluster search** provide native cross-cluster querying for organizations already on that ecosystem.
- **Algolia federated or multi-index search** is strong for UX-oriented multi-source search, especially when results can remain grouped by source/index.
- **Typesense multi_search/federated search** covers searching multiple collections in one Typesense cluster/request, and Typesense HA covers node-level availability.

The product is therefore not a generic “federated search” novelty. Its defensible value is narrower and real: document-level routing/fan-out, async indexing, and an admin surface around **multiple Typesense clusters** where native Typesense federation is not enough. Recommended positioning: open-source Typesense federation gateway/control plane, not an Elasticsearch/OpenSearch replacement.

### Build-vs-buy

- **Build** when Typesense is a strategic choice and data must be split by tenant/region/cluster while still exposing one API and admin workflow.
- **Buy/use native** when the team is already on Elastic/OpenSearch and needs broad cross-cluster query/analytics.
- **Hybrid** when Algolia/Typesense native federated search handles UX multi-index search, while IMPOSBRO owns operational routing and ingestion across physical Typesense clusters.

## Why This Project Matters

- **Real federated architecture**: Not a thin wrapper around Typesense—document-level sharding, field-based routing, scatter-gather search, and correct deep pagination across multiple clusters.
- **Smart Producer**: The Query API decides the target cluster and puts it in the Kafka message; the indexing service executes without recomputing routing. No logic duplication and consistent behaviour.
- **HA state**: Configuration (clusters, routing) lives in an internal Typesense cluster (Raft), with sync across instances via Redis Pub/Sub. Suited for multi-instance deployment.
- **Modern, separated stack**: FastAPI, Next.js 14 App Router, Kafka, Redis, Prometheus/Grafana, Helm. Good base for extensions (auth, more clusters, custom metrics).
- **Operational**: docker-compose for development, Helm for Kubernetes, health checks with Redis/Kafka status, Prometheus metrics.

---

## Fixes and Improvements Applied

### Current hardening pass

1. **Admin auth no longer public-by-default**: `/admin/*` requires `ADMIN_API_KEY` unless `ALLOW_UNAUTHENTICATED_ADMIN=true` is explicitly set for local development.
2. **Safe cluster credentials flow**: public cluster listing keeps API keys masked; indexing service now uses an internal admin-authenticated endpoint with unmasked credentials.
3. **Admin UI proxy hardening**: server-side proxy can inject `ADMIN_API_KEY` / `INTERNAL_QUERY_API_ADMIN_API_KEY` without exposing secrets to browser JavaScript.
4. **Input validation**: cluster names, collection names, aliases, and routing models consistently use the shared Typesense-compatible name pattern.
5. **State consistency**: startup distinguishes empty routing config from missing state, and admin mutations fail if state persistence fails.
6. **Kafka reliability**: indexing consumer now uses manual offset commits after successful upsert and fails missing target-cluster messages instead of silently dropping them.
7. **Search relevance**: federated merge now preserves Typesense `_text_match:desc` semantics.
8. **Helm deployment**: chart metadata is now `Chart.yaml`; ConfigMap/Secret env vars match the app settings; Admin UI service defaults to `ClusterIP`.
9. **Dependency/security**: Admin UI upgraded to Next.js 16.2.9 and ESLint 9; production audit gate has no high/critical findings.
10. **CI**: GitHub Actions runs Python service tests, Admin UI lint/build, and production dependency audit.
11. **Runtime hardening**: Python Docker images use Python 3.11 and non-root users.

### Fixes

1. **README and Helm**: Paths updated from `helm/imposbro-search` to `helm` (Chart and values live under `helm/`).
2. **Settings**: `DEFAULT_DATA2_CLUSTER_*` made optional (default `""`) for single-cluster setups.
3. **Federation**: Virtual cluster `"default"` resolved to a real data cluster (`default-data-cluster` or first available) in `get_client_for_document`, `get_clients_for_search`, and `set_routing_rules`.
4. **Search/ingest**: When no valid cluster is available the API returns 503 instead of publishing to Kafka (avoids useless messages and consumer errors).
5. **Indexing service**: Virtual cluster `"default"` from `/admin/federation/clusters` is skipped when building Typesense clients.
6. **Query API requirements**: Removed `asyncio` (stdlib).
7. **Admin UI**: Proxy `/api/*` to Query API via Route Handler (`app/api/[[...path]]/route.js`) instead of middleware rewrite (unsuitable for external URLs).
8. **SyncConfigNotifier**: Added `close()` and call it on shutdown to close the Redis client.

### Improvements

1. **Health**: `/health` now exposes `redis` and `kafka` connectivity; `status` is `degraded` when there are no clusters or Redis is down.
2. **Helm**: Added indexing-service deployment (values + template).
3. **Tests**: Pytest suite for Query API (root, health, ingest without `id`, valid ingest), with test lifespan (`TESTING=1`) and minimal env in `conftest`.
4. **Documentation**: `.env.example` commented; `PROJECT_ANALYSIS.md` (this file).
5. **Root tooling**: `Makefile` and `package.json` at repo root with `test` / `npm run test` to run Query API tests from root; extended `.gitignore` (pytest cache, coverage, OS); added `LICENSE` (MIT).
6. **Extra tests**: `test_search.py` (404 collection not found, 422 invalid collection name), `test_admin.py` (400 delete default cluster, GET clusters with default).
7. **README**: Fixed variable name from `TYPESENSE_NODES` to `INTERNAL_STATE_NODES` in the HA scaling section.
8. **Patterns and best practices** (see also `docs/PATTERNS_AND_PRACTICES.md`):
   - **Constants**: `constants.py` (version, Typesense port, name pattern); no magic numbers.
   - **CORS**: CORS middleware added only when `CORS_ORIGINS` is set; explicit origins for production.
   - **Path validation**: `collection_name` and `cluster_name` validated with regex (`NAME_PATTERN`: alphanumeric, hyphen, underscore).
   - **Security**: API keys masked in `GET /admin/federation/clusters` (only last 4 characters visible).
   - **Federation**: In `load_from_state`, port normalised (int → string) for Typesense compatibility.
   - **Admin UI**: API client handles non-JSON responses (e.g. 502 from proxy) without crashing.

---

## Suggested Roadmap

### High priority

- ~~**Authentication/authorization**~~: **Done.** Optional API key for Admin API (`ADMIN_API_KEY`); use `X-API-Key` or `Authorization: Bearer`. OAuth2/OIDC can be added later.
- ~~**Secrets in production**~~: **Done.** Helm chart supports `config.useSecret: true` and a Secret template for API keys and `REDIS_URL`; deployments can use `envFrom: secretRef`.
- ~~**Integration tests**~~: **Done.** Pytest marker `integration` and `INTEGRATION=1` to run tests against live Kafka/Redis/Typesense; documented in CONTRIBUTING; CI can run unit tests with `-m "not integration"`.

### Medium priority

- ~~**Document fan-out**~~: **Done.** Routing rules support optional `clusters` (list) for replicating a document to multiple clusters; ingest publishes one message per target.
- ~~**Collection aliases**~~: **Done.** Admin API: `PUT /admin/aliases/{alias_name}`, `GET /admin/aliases`, `DELETE /admin/aliases/{alias_name}` (per cluster) for zero-downtime reindexing.
- ~~**Cursor-based pagination**~~: **Done.** Search accepts optional `offset` and `limit` for cursor-style deep pagination; response includes `next_offset` when applicable.
- ~~**Admin UI dashboard**~~: **Done.** Dashboard fetches `/admin/stats` and `/health` every 15s; shows status, clusters, collections, Redis/Kafka.

### Low priority

- ~~**Grafana**~~: **Done.** Overview dashboard extended with "Documents by Collection (1h)" and "Error Rate (5xx)" panels.

---

## Running Tests

```bash
# From repo root
make test          # Unix/macOS
npm run test       # Any OS

# From query_api/
cd query_api
pip install -r requirements-dev.txt
python -m pytest tests -v   # TESTING=1 is set in conftest
```

---

## Developer Documentation

- **[docs/PATTERNS_AND_PRACTICES.md](docs/PATTERNS_AND_PRACTICES.md)**: Architectural patterns, dependency injection, security (API key masking, path validation, CORS), error handling, and checklist for new changes.

## Summary

The project is solid and well-suited for federated and multi-cluster search. The changes above align documentation, configuration, behaviour (default cluster, 503, Admin UI proxy), and quality (tests, health, shutdown). Best practices have been applied for constants, CORS, input validation, and security (masked API keys). The roadmap above outlines next steps for production and advanced features.
