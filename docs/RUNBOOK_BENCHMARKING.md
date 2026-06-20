# Kubernetes Benchmark Runbook

This runbook turns the remaining scale-proof work into a repeatable benchmark.
It measures the deployed Query API path end to end: document ingest, Kafka-backed
indexing convergence, federated search latency, partial responses, and optional
SLO thresholds.

## What It Proves

- The Query API can accept sustained ingest traffic for a realistic collection.
- Indexing workers can drain the Kafka topics and make every benchmark document
  searchable within a known time budget.
- Federated search responds with measurable p50/p95/p99 latency and no hidden
  shard partials unless partial responses are explicitly allowed.
- Benchmark output is both JSON-friendly and Markdown-friendly, so release
  candidates can be compared by machines and published for human review without
  rewriting terminal notes by hand.

## Precheck

1. Deploy IMPOSBRO with production-like Helm values, immutable images, and real
   Kafka/Typesense dependencies.
2. Expose the Query API through an authenticated Ingress or a local port-forward:
   ```bash
   kubectl port-forward svc/imposbro-release-imposbro-search-query-api 8000:8000
   ```
3. Provide credentials through the same variables used by smoke tests:
   ```bash
   export QUERY_API_URL=http://localhost:8000
   export SMOKE_ADMIN_API_KEY=...
   export SMOKE_INGEST_API_KEY=...
   export SMOKE_SEARCH_API_KEY=...
   ```
   `ADMIN_API_KEY`, `DATA_API_KEY`, or compatible `SCOPED_API_KEYS` also work.
4. Confirm the normal gates first:
   ```bash
   make ci
   make smoke-load
   ```

## Baseline Run

Start with a controlled benchmark that creates and deletes a temporary
collection:

```bash
BENCHMARK_DOCUMENTS=10000 \
BENCHMARK_INGEST_CONCURRENCY=32 \
BENCHMARK_INGEST_BATCH_SIZE=100 \
BENCHMARK_SEARCH_REQUESTS=500 \
BENCHMARK_SEARCH_CONCURRENCY=16 \
BENCHMARK_ENVIRONMENT=staging-eu \
BENCHMARK_RELEASE=$(git rev-parse --short HEAD) \
BENCHMARK_CLUSTER_SHAPE="query=3,indexing=5,typesense=2x3,kafka=3" \
BENCHMARK_HELM_VALUES_REF=release-values/staging-eu.yaml \
BENCHMARK_IMAGE_SET="query-api@sha256:...,indexing@sha256:...,admin-ui@sha256:..." \
BENCHMARK_OUTPUT_JSON=artifacts/benchmark-baseline.json \
BENCHMARK_OUTPUT_MARKDOWN=artifacts/benchmark-baseline.md \
make benchmark-k8s
```

For a local repeatable benchmark against Docker Compose, use:

```bash
make benchmark-docker
```

The Docker target starts the stack, runs the same harness with conservative
defaults, writes `artifacts/benchmark-docker.json` and
`artifacts/benchmark-docker.md`, and tears the stack down.
Tune it without editing the Makefile:

```bash
BENCHMARK_DOCKER_DOCUMENTS=2000 \
BENCHMARK_DOCKER_INGEST_CONCURRENCY=32 \
BENCHMARK_INGEST_BATCH_SIZE=50 \
BENCHMARK_DOCKER_SEARCH_REQUESTS=250 \
BENCHMARK_DOCKER_SEARCH_CONCURRENCY=16 \
BENCHMARK_DOCKER_OUTPUT_JSON=artifacts/benchmark-docker-2k.json \
BENCHMARK_DOCKER_OUTPUT_MARKDOWN=artifacts/benchmark-docker-2k.md \
make benchmark-docker
```

The harness prints:

- ingest successes, errors, throughput, and request latency p95;
- seconds until all documents become searchable;
- search successes, errors, p95 latency, and partial response count;
- SLO violations when thresholds are configured.

When `BENCHMARK_OUTPUT_MARKDOWN` is set, the harness also writes a compact
release-evidence report with ingest, indexing, search, SLO, and request-error
sections. The JSON and Markdown artifacts include publishable run metadata when
`BENCHMARK_ENVIRONMENT`, `BENCHMARK_RELEASE`, `BENCHMARK_CLUSTER_SHAPE`,
`BENCHMARK_HELM_VALUES_REF`, `BENCHMARK_IMAGE_SET`, and optional
`BENCHMARK_EVIDENCE_NOTES` are set. Keep the JSON artifact beside the Markdown
report; the Markdown is for release review and the JSON is for trend comparison.
Set `BENCHMARK_INGEST_BATCH_SIZE=1` to measure single-document request overhead,
or a larger value to exercise `/ingest/{collection}/batch`. Throughput is always
reported as accepted documents per second; ingest latency is request latency, so
larger batch sizes should be compared against runs with the same batch size.

## Release SLO Example

Use environment-specific SLOs. The example below fails the command when
throughput, convergence, search latency, or partial-response expectations are
not met:

```bash
BENCHMARK_DOCUMENTS=100000 \
BENCHMARK_INGEST_CONCURRENCY=64 \
BENCHMARK_INGEST_BATCH_SIZE=100 \
BENCHMARK_SEARCH_REQUESTS=2000 \
BENCHMARK_SEARCH_CONCURRENCY=32 \
BENCHMARK_MIN_INGEST_DOCS_PER_SECOND=500 \
BENCHMARK_MAX_INDEXING_VISIBLE_SECONDS=300 \
BENCHMARK_MAX_SEARCH_P95_MS=250 \
BENCHMARK_MAX_SEARCH_ERROR_RATE=0 \
BENCHMARK_ENVIRONMENT=prod-eu \
BENCHMARK_RELEASE=v1.0.0 \
BENCHMARK_CLUSTER_SHAPE="query=6,indexing=12,typesense=4x3,kafka=3" \
BENCHMARK_HELM_VALUES_REF=release-values/prod-eu.yaml \
BENCHMARK_IMAGE_SET="query-api@sha256:...,indexing@sha256:...,admin-ui@sha256:..." \
BENCHMARK_OUTPUT_JSON=artifacts/benchmark-release.json \
BENCHMARK_OUTPUT_MARKDOWN=artifacts/benchmark-release.md \
make benchmark-k8s
```

For a deliberate degraded-mode exercise, set `BENCHMARK_ALLOW_PARTIAL=true` and
record the reason in the benchmark artifact.

## Existing Collection Mode

When a team wants to reuse a pre-created collection and manage cleanup
separately:

```bash
BENCHMARK_COLLECTION=products_benchmark \
BENCHMARK_USE_EXISTING_COLLECTION=true \
BENCHMARK_KEEP_COLLECTION=true \
make benchmark-k8s
```

The existing collection must contain these fields:

- `title` as `string`
- `body` as `string`
- `tenant` as faceted `string`
- `category` as faceted `string`
- `rank` as `int32`

## Interpreting Results

- **Ingest errors** usually point to Query API auth, Kafka publishing, routing,
  or unavailable target clusters.
- **Slow indexing visibility** points to Kafka lag, worker replica/partition
  mismatch, Typesense write throughput, or retry pressure.
- **Partial search responses** mean at least one target cluster failed during
  scatter-gather. Treat this as a release blocker unless the run is explicitly
  a degraded-mode benchmark.
- **Search p95/p99 regression** should be compared against a previous JSON
  artifact from the same dataset size and cluster shape.

Keep benchmark JSON and Markdown artifacts with release evidence. They are
intentionally not committed by default because they are environment-specific
measurements. Before publishing broad scale claims, confirm that the Markdown
metadata table has no important `n/a` fields for environment, release, cluster
shape, images, and deployment values.
