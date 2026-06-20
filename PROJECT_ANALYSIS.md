# IMPOSBRO Search – Analysis and Improvements

## Current Product Verdict (2026-06-19)

IMPOSBRO Search is useful, but only with a clear wedge: **a Typesense-focused federation/control plane** for teams that want Typesense ergonomics while distributing data across multiple physical clusters by tenant, region, compliance boundary, or scaling domain.

The market already has strong alternatives:

- **Elasticsearch / OpenSearch cross-cluster search** provide native cross-cluster querying for organizations already on that ecosystem.
- **Algolia federated or multi-index search** is strong for UX-oriented multi-source search, especially when results can remain grouped by source/index.
- **Typesense multi_search/federated search** covers searching multiple collections in one Typesense cluster/request, and Typesense HA covers node-level availability.

The product is therefore not a generic “federated search” novelty. Its defensible value is narrower and real: document-level routing/fan-out, async indexing, and an admin surface around **multiple Typesense clusters** where native Typesense federation is not enough. Recommended positioning: open-source Typesense federation gateway/control plane, not an Elasticsearch/OpenSearch replacement.

### Market Signals Checked

- [Typesense federated / multi-search](https://typesense.org/docs/30.2/api/federated-multi-search.html) searches multiple collections in one request, mostly inside the Typesense API surface.
- [Typesense search sorting](https://typesense.org/docs/30.2/api/search.html) supports `_text_match` and up to three sort fields, which makes exact gateway-side merge semantics important when results come from multiple physical clusters.
- [Algolia multi-index search](https://www.algolia.com/doc/guides/building-search-ui/ui-and-ux-patterns/multi-index-search/react) is mature for UX-level federated experiences across indices.
- [Meilisearch multi-search / federated search](https://meilisearch.com/docs/capabilities/multi_search/overview) now supports merged and re-ranked multi-index results.
- [OpenSearch cross-cluster search](https://docs.opensearch.org/latest/search-plugins/cross-cluster-search/) and [Amazon OpenSearch Service cross-cluster search](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/cross-cluster-search.html) cover native cross-cluster querying for the OpenSearch ecosystem.

Implication: the product should not compete as “another search engine.” It should compete as a configurable operational control plane for teams that already want Typesense but need physical cluster routing, async ingestion, auditability, and safer admin workflows.

### Market Validation Refresh (2026-06-19)

The market does validate the problem, but it also narrows the room for differentiation:

- [Typesense Cloud pricing](https://cloud.typesense.org/pricing) emphasizes dedicated clusters with no record/search-operation limits, so the economic wedge is strongest for teams that like Typesense but need to manage multiple physical clusters themselves.
- [Typesense federated / multi-search](https://typesense.org/docs/30.2/api/federated-multi-search.html) already supports multiple searches in one request and union search across collections; IMPOSBRO must therefore differentiate on multi-cluster routing, ingestion, operations, and governance, not on basic multi-index search.
- [Typesense HA docs](https://typesense.org/docs/guide/high-availability.html) require explicit peering addresses in the nodes file; stable Docker/Kubernetes networking and readiness are not incidental details, they are core product reliability.
- [Meilisearch federated search](https://www.meilisearch.com/docs/capabilities/multi_search/getting_started/federated_search) can merge multiple indexes into a single result list, making “merged search UX” a commodity expectation rather than a defensible feature by itself.
- [OpenSearch cross-cluster search](https://docs.opensearch.org/latest/search-plugins/cross-cluster-search/) and [Elastic cross-cluster search](https://www.elastic.co/docs/explore-analyze/cross-cluster-search) directly address querying multiple clusters for their ecosystems; IMPOSBRO should not try to out-platform them.
- [OpenSearch hybrid search](https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/index/) and [Elastic AI/search positioning](https://www.elastic.co/enterprise-search) show that semantic, vector, and hybrid retrieval are now baseline market expectations.
- [Algolia pricing](https://www.algolia.com/pricing) and [Coveo pricing](https://www.coveo.com/en/pricing) position AI relevance, enterprise controls, analytics, and managed support as paid value. IMPOSBRO’s opportunity is open, configurable control for teams that prefer owning infrastructure and avoiding per-request/vendor-platform economics.

**Go / no-go:** Go for an open-source, self-hostable Typesense federation gateway/control plane. No-go for a generic hosted search engine, a generic federated search UI, or an Algolia/Elastic/Coveo replacement.

**Best ICP:** platform/search teams with multi-tenant, regional, compliance, or scaling reasons to keep separate Typesense clusters while exposing one API, one admin workflow, and one ingestion path.

**Sharp product promise:** “One configurable control plane for routing, ingesting, searching, observing, and operating many Typesense clusters.”

### Build-vs-buy

- **Build** when Typesense is a strategic choice and data must be split by tenant/region/cluster while still exposing one API and admin workflow.
- **Buy/use native** when the team is already on Elastic/OpenSearch and needs broad cross-cluster query/analytics.
- **Hybrid** when Algolia/Typesense native federated search handles UX multi-index search, while IMPOSBRO owns operational routing and ingestion across physical Typesense clusters.

## Why This Project Matters

- **Real federated architecture**: Not a thin wrapper around Typesense—document-level sharding, field-based routing, scatter-gather search, and correct deep pagination across multiple clusters.
- **Smart Producer**: The Query API decides the target cluster and puts it in the Kafka message; the indexing service executes without recomputing routing. No logic duplication and consistent behaviour.
- **HA state**: Configuration (clusters, routing) lives in an internal Typesense cluster (Raft), with sync across instances via Redis Pub/Sub. Suited for multi-instance deployment.
- **Modern, separated stack**: FastAPI, Next.js App Router, Kafka, Redis, Prometheus/Grafana, Helm. Good base for extensions (auth, more clusters, custom metrics).
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
7. **Search relevance and honesty**: federated merge preserves Typesense `_text_match:desc` semantics, supports simple global `sort_by`, deduplicates using the same comparator, exposes partial failures, and returns `503` when every target cluster fails.
8. **Helm deployment**: chart metadata is now `Chart.yaml`; ConfigMap/Secret env vars match the app settings; Admin UI service defaults to `ClusterIP`; workloads expose configurable probes, resources, service account, pod scheduling, and security contexts.
9. **Dependency/security**: Admin UI upgraded to Next.js 16.2.9 and ESLint 9; production audit gate has no high/critical findings.
10. **Delivery gates**: root `npm test` runs API and indexing tests; Admin UI has explicit lint/build/audit commands. A GitHub Actions workflow is recommended next, but was not committed because the current OAuth token cannot create workflow files without the `workflow` scope.
11. **Runtime hardening**: Python and Admin UI Docker images run as numeric non-root users, aligning with Kubernetes `runAsNonRoot` policies.
12. **Data-plane auth and auditability**: `/ingest/*`, `/documents/*`, and `/search/*` can require `DATA_API_KEY`, and successful admin mutations are persisted to a safe audit log.
13. **Schema reconciliation**: desired collection schemas are persisted in control-plane state; new clusters are backfilled automatically, and operators can trigger an idempotent `/admin/collections/reconcile`.
14. **Worker observability and DLQ safety**: indexing service exposes configurable Prometheus metrics for cluster config fetches, loaded clusters, successful indexing, retries, and DLQ publications; the Kafka subscription pattern excludes the DLQ topic so poison messages are not recursively consumed.
15. **Admin search workspace**: Admin UI now includes a `/workspace` view for create-schema, ingest, delete, and search workflows against the configured Query API proxy.
16. **Typesense readiness hardening**: `/ready` now verifies every declared node in every data cluster and reports `data_cluster_nodes`; Compose pins Typesense node IPs so Raft peer state survives `docker compose down/up` with volumes.
17. **Runtime validation**: Docker smoke covered create collection, Kafka ingest, federated search with both clusters responding, and a down/up restart with persisted volumes.
18. **Hybrid/vector search path**: `/search/{collection}` now supports a JSON body for semantic/vector/hybrid Typesense params, including `vector_query`, embedding retry controls, and global merge by `_vector_distance`.
19. **Scoped API keys**: `SCOPED_API_KEYS` can grant least-privilege `admin`, `search`, `ingest`, `data`, `*`, or collection-pattern data-plane access such as `search:products_*` / `ingest:orders_*` while preserving legacy `ADMIN_API_KEY`/`DATA_API_KEY` behavior.
20. **Repeatable runtime smoke**: `make smoke-docker` builds/starts the stack, creates a vector collection, ingests through Kafka, verifies federated vector ordering, queues an async document delete, verifies the deleted document disappears from search, checks the Admin UI proxy, then tears the stack down.
21. **Partial outage smoke**: `make smoke-docker-outage` stops the secondary data cluster and verifies `/ready` degrades while federated search still returns healthy-cluster hits with `partial: true` and explicit `failed_clusters`.
22. **Control-plane backup/restore**: Admin API can export state snapshots with masked secrets by default, create restore-ready exports with explicit secret opt-in, dry-run imports, and apply audited state restores for clusters, routing, schemas, and aliases.
23. **Kafka/indexing load smoke**: `make smoke-docker-load` concurrently ingests configurable document batches through Kafka, waits for indexing convergence, and verifies sorted federated search results.
24. **Admin operations workflow**: Admin UI now exposes backup/restore as an operator workflow with masked export, restore-ready export confirmation, JSON download, file upload, dry-run validation, stale-validation protection, and explicit apply confirmation.
25. **Disaster-recovery smoke**: `make smoke-docker-state` validates masked export, restore-ready export, dry-run import, applied restore, routing/schema/alias state recovery, and collection schema reconciliation against a live stack.
26. **Config-sync self-notification safety**: Query API instances tag Redis config notifications with a source id and ignore their own messages, preventing immediate stale reloads after state mutations while preserving multi-replica convergence.
27. **Operations audit visibility**: Admin UI now surfaces recent sanitized admin audit events on the Operations page so backup/restore and other control-plane mutations are visible without leaving the console.
28. **Schema reconciliation workflow**: Admin UI Collections now exposes `POST /admin/collections/reconcile` with a per-cluster created/existing report for restore or cluster-recovery drills.
29. **Collection aliases workflow**: Admin UI Collections now supports listing, creating/updating, and deleting per-cluster aliases for zero-downtime reindexing.
30. **Collection aliases smoke**: `make smoke-docker-alias` creates versioned collections, upserts aliases on every data cluster, verifies federated search follows the alias, switches the alias, verifies search follows the new target, and cleans up.
31. **Local release gate**: `make ci` runs the API/worker tests, Admin UI tests, lint, production build, Docker Compose config validation, and Helm lint/render with CI values.
32. **Kubernetes benchmark harness**: `make benchmark-k8s` runs configurable sustained ingest/search against a deployed Query API, waits for indexing visibility, records p50/p95/p99 latencies, emits JSON artifacts, and can enforce environment-specific SLO thresholds.
33. **Kubernetes network boundary**: Helm now renders opt-in NetworkPolicies for Query API, Admin UI, and indexing metrics, with configurable ingress/gateway, Prometheus, and egress rules so production teams can enforce cluster-specific traffic boundaries.
34. **Production alerting**: Helm now renders opt-in ServiceMonitor and PrometheusRule resources for Prometheus Operator, covering Query API error rate/latency and indexing DLQ/retry/no-cluster conditions with configurable thresholds.
35. **Disruption safety**: Helm now renders opt-in PodDisruptionBudgets for Query API, Admin UI, and indexing workers so production teams can preserve replica availability during voluntary node drains or cluster maintenance.
36. **Replica placement**: Helm now supports per-workload topology spread constraints so replicated Query API, Admin UI, and indexing pods can be distributed across nodes or zones.
37. **Ingress exposure**: Helm now renders opt-in Ingress resources for Query API and Admin UI with configurable class, annotations, TLS, hosts, paths, and service routing while keeping services `ClusterIP` by default.
38. **Release hardening**: Admin mutations roll runtime state back when persistence fails, cluster registration probes all declared nodes, search pagination fetches one extra hit for `next_offset`, fan-out search exposes deduplicated counts, legacy worker messages resolve `default` to a real cluster, Compose binds dev ports to localhost, Helm fails on placeholder/mutable images or missing service/secret values, and local Docker smoke targets cover runtime paths.
39. **Admin UI completeness**: Routing preserves fan-out `clusters[]`, Workspace exposes ingest/delete, offset pagination, and advanced search tuning params, Collections can set `default_sorting_field`, Dashboard/Clusters show per-cluster health, and Operations audit logs can be filtered.
40. **Enterprise identity**: Query API accepts OIDC/JWT bearer tokens with issuer/audience/signature checks, configurable claim-to-scope mapping, hashed OIDC audit actors, and optional collection tenant policies that inject server-side search/delete filters and validate or inject ingest tenant fields.
41. **Alias state portability**: Collection alias bindings are persisted in control-plane state, included in backup/restore snapshots, restored through import apply, and covered by DR smoke.
42. **Horizontal scaling proof**: Added a scale Compose overlay, local Query API proxy, `make smoke-docker-scale`, Kafka lag budget check, rolling-restart ingest smoke, and an operator runbook for scale up/down, lag triage, rollback, and incidents.
43. **Kubernetes autoscaling controls**: Helm chart now supports optional `autoscaling/v2` HPA for Query API/Admin UI/workers and optional KEDA Kafka `ScaledObject` for indexing workers, rendered in CI values.
44. **Collection-level data-plane RBAC**: API keys and OIDC claims can now grant search/ingest/delete/data access to collection glob patterns, with server-side enforcement in the Query API.
45. **Release gate reproducibility**: `make helm` now runs a Python chart validation suite that checks rendered resource counts, Query API/Admin UI Ingress permutations, and negative fail-fast guardrails; Compose Make targets use `.env` when present and `.env.example` as a clean-checkout fallback.
46. **Local benchmark evidence**: `make benchmark-docker` starts the Docker stack, runs the deployment-agnostic ingest/search benchmark with conservative defaults, writes JSON and Markdown artifacts under `artifacts/`, and tears the stack down for repeatable local scale evidence.
47. **Data-plane abuse control**: Query API can now enforce optional fixed-window search and write-side data mutation rate limits by authenticated actor and collection, using Redis counters for multi-replica deployments and memory counters for local/test runs.
48. **Rate-limit observability**: Query API now exports low-cardinality Prometheus metrics for allowed/blocked rate-limit decisions and backend failures; Helm renders alert rules and Grafana shows rate-limit blocks/errors.
49. **Data lifecycle delete**: Query API now exposes `DELETE /documents/{collection}/{document_id}` as an async data-plane mutation. It fans out delete events to every candidate data cluster, propagates request ids, enforces ingest/data scoped authorization, uses tenant-safe filtered deletes for OIDC tenant policies, and the worker treats missing documents as idempotent no-ops with `indexing_documents_deleted_total` metrics.
50. **Data lifecycle read/export**: Query API now exposes `GET /documents/{collection}/{document_id}` as a read-side data-plane operation. It checks every candidate data cluster, enforces search/data scoped authorization, returns tenant-mismatched OIDC documents as 404, exposes low-cardinality `documents_read_total`, and is available from the Admin UI Workspace.

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

1. **Health/readiness**: `/health` now exposes Redis, Kafka, and per-data-cluster readiness; `/ready` returns HTTP 503 until all required dependencies and data clusters are ready.
2. **Helm**: Added indexing-service deployment (values + template) and production-oriented defaults for probes, resources, non-root security context, service account token mounting, and scheduling overrides.
3. **Observability**: Prometheus now scrapes Query API and indexing worker metrics in Compose, Typesense scrape targets use the declared service names, and Grafana includes panels for worker indexing, retry, and DLQ signals.
4. **Tests**: Pytest suite for Query API (root, health, ingest without `id`, valid ingest), with test lifespan (`TESTING=1`) and minimal env in `conftest`.
5. **Documentation**: `.env.example` commented; `PROJECT_ANALYSIS.md` (this file).
6. **Root tooling**: `Makefile` and `package.json` at repo root with `test` / `npm run test` to run Query API tests from root; extended `.gitignore` (pytest cache, coverage, OS); added `LICENSE` (MIT).
7. **Extra tests**: `test_search.py` (404 collection not found, 422 invalid collection name), `test_admin.py` (400 delete default cluster, GET clusters with default).
8. **README**: Fixed variable name from `TYPESENSE_NODES` to `INTERNAL_STATE_NODES` in the HA scaling section.
9. **Patterns and best practices** (see also `docs/PATTERNS_AND_PRACTICES.md`):
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
- ~~**Collection aliases**~~: **Done.** Admin API and Admin UI support `PUT /admin/aliases/{alias_name}`, `GET /admin/aliases`, `DELETE /admin/aliases/{alias_name}` (per cluster) for zero-downtime reindexing.
- ~~**Alias state portability**~~: **Done.** Alias bindings are first-class control-plane state and are included in export/import snapshots.
- ~~**Cursor-based pagination**~~: **Done.** Search accepts optional `offset` and `limit` for cursor-style deep pagination; response includes `next_offset` when applicable.
- ~~**Admin UI dashboard**~~: **Done.** Dashboard fetches `/admin/stats` and `/health` every 15s; shows status, clusters, collections, Redis/Kafka.
- ~~**Hybrid/vector search gateway**~~: **Done.** JSON search endpoint supports long vector/hybrid params and cross-cluster merge can order vector-only results by `_vector_distance`.
- ~~**Support diagnostics**~~: **Done.** Query API echoes a sanitized request-correlation header and propagates it through Kafka data-plane messages so indexing logs can be tied back to the originating HTTP request without adding high-cardinality metric labels.
- ~~**Data lifecycle delete**~~: **Done.** Document delete is available as an asynchronous data-plane write and covered by unit plus Docker smoke tests.
- ~~**Document read/export by ID**~~: **Done.** Tenant-safe data-plane read/export is available by API and Admin UI, and covered by unit plus Docker smoke tests.

### Low priority

- ~~**Grafana**~~: **Done.** Overview dashboard extended with documents by collection, Query API error rate, indexing throughput, retry, and DLQ panels.

### Remaining Product Risks

- **Enterprise authorization depth**: OIDC, tenant policies, collection-scoped data-plane RBAC, tenant-safe document read/delete, Admin UI login/session flows, and fine-grained admin operation scopes now cover API identity, tenant isolation, collection access, browser operator login, and least-privilege operator roles.
- **Traffic abuse protection**: Optional actor-scoped data-plane rate limits now protect search and write-side data mutation paths with Prometheus/Grafana visibility; production operators still need environment-specific budgets and gateway/WAF policy for public exposure.
- **Operational scale proof**: local Docker now covers multi-instance rolling smoke, lag budget, JSON/Markdown benchmark artifacts, Helm autoscaling manifests, and a repeatable Kubernetes benchmark harness; the next credibility step is publishing results from a production-sized Kubernetes run.
- **CI/CD gate**: local `make ci` is green, but hosted GitHub Actions workflow creation still depends on a token with `workflow` scope.
- **Documentation depth**: horizontal scaling, production topology, NetworkPolicy, benchmark execution, and disaster-recovery drills now have operator runbooks. A published production-sized benchmark result is still needed before claiming broad enterprise scale proof.

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
