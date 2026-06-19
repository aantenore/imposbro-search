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
- Benchmark output is JSON-friendly, so release candidates can be compared over
  time instead of judged from ad hoc terminal output.

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
BENCHMARK_SEARCH_REQUESTS=500 \
BENCHMARK_SEARCH_CONCURRENCY=16 \
BENCHMARK_OUTPUT_JSON=artifacts/benchmark-baseline.json \
make benchmark-k8s
```

The harness prints:

- ingest successes, errors, throughput, and request latency p95;
- seconds until all documents become searchable;
- search successes, errors, p95 latency, and partial response count;
- SLO violations when thresholds are configured.

## Release SLO Example

Use environment-specific SLOs. The example below fails the command when
throughput, convergence, search latency, or partial-response expectations are
not met:

```bash
BENCHMARK_DOCUMENTS=100000 \
BENCHMARK_INGEST_CONCURRENCY=64 \
BENCHMARK_SEARCH_REQUESTS=2000 \
BENCHMARK_SEARCH_CONCURRENCY=32 \
BENCHMARK_MIN_INGEST_DOCS_PER_SECOND=500 \
BENCHMARK_MAX_INDEXING_VISIBLE_SECONDS=300 \
BENCHMARK_MAX_SEARCH_P95_MS=250 \
BENCHMARK_MAX_SEARCH_ERROR_RATE=0 \
BENCHMARK_OUTPUT_JSON=artifacts/benchmark-release.json \
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

Keep benchmark JSON artifacts with release evidence. They are intentionally not
committed by default because they are environment-specific measurements.
