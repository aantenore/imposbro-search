# Runbook: required enterprise telemetry missing

Applies to a missing Query API target, absent JSON/PostgreSQL exporter series,
or an OTLP trace-export gap. Treat missing telemetry as unknown, not healthy. The repository
configuration is only a reference until mounted and validated in an environment.

## Trigger and impact

- No `imposbro_services` target exists for ten minutes: service discovery or
  Prometheus configuration is incomplete, so availability and latency alerts
  cannot evaluate.
- Revision or outbox-age metrics are absent for fifteen minutes: convergence
  and durable-delivery alerts are blind or only partially evaluated.
- Query API or indexing-service trace resources stop arriving while traffic is
  present: API-to-Kafka-to-worker diagnosis and release attribution are blind.

Open an observability incident when a page is blind, multiple environments are
affected, the gap overlaps a service incident, or the missing interval can no
longer be reconstructed.

## Immediate safety

1. Do not silence service alerts or replace absent data with zero.
2. Freeze monitoring/config changes that correlate with the gap.
3. Use direct health checks and platform logs as temporary coverage; record the
   exact blind interval and affected rules.
4. Do not grant exporter superuser or write access as a quick fix. Preserve
   least privilege and TLS verification.

## Diagnose

```promql
up{job=~"imposbro_services|indexing_service|imposbro_query_health|imposbro_postgres"}
```

```promql
absent(http_requests_total{job="imposbro_services"})
or absent(indexing_worker_ready{job="indexing_service"})
or absent(imposbro_query_api_control_plane_applied_revision)
or absent(imposbro_control_plane_outbox_pending)
or absent(imposbro_indexing_event_outbox_oldest_unpublished_age_seconds)
```

Check in order:

1. Prometheus process health, rule evaluation errors, storage health, and target
   discovery output.
2. The active Prometheus configuration actually mounts
   `monitoring/prometheus/rules/` and contains exporter scrape jobs.
3. Direct Query API `/metrics`, direct per-replica `/health`, worker `:9108`,
   JSON exporter `/probe`, and PostgreSQL exporter `/metrics` responses.
4. JSON exporter module path matches the current health response.
5. PostgreSQL exporter custom queries load and its read-only role can select the
   required tables in the intended database/schema.
6. Relabeling preserves a unique per-replica `instance`; a service virtual IP is
   not sufficient for convergence.
7. Certificate, DNS, network policy, secret rotation, query timeout, cardinality,
   and scrape-limit errors.
8. OTLP/HTTP collector health, exporter TLS trust, configured endpoint,
   authentication header secret injection, queue/export timeouts, sampling, and
   collector backpressure or rejection counters. Never print exporter headers.

Run `scripts/ops/validate-ops-artifacts.sh` to catch repository syntax and
cross-file contract errors. Its success does not validate the live scrape path.

## Mitigate

- Roll back the correlated Prometheus/exporter/deployment configuration.
- Restore target discovery and mounts through the environment's configuration
  controller; avoid an untracked manual edit.
- Repair exporter queries or read-only grants narrowly. The database exporter
  needs only `SELECT` on referenced tables.
- Restore one direct health target per Query API replica and a stable identity
  label.
- If live monitoring cannot recover promptly, establish a time-bounded manual
  watch using direct probes, database read-only queries, and an explicit owner.

## Validate and close

- Every intended target is discovered and `up == 1`.
- All required metrics have fresh timestamps and expected labels/values.
- A synthetic ingest produces one trace ID across Query API server, Kafka
  producer, indexing consumer, and Typesense target spans with the expected
  service/build/revision resources and no payload attributes.
- Recording rules return data; Grafana panels show no silent gaps.
- `promtool` validation and Prometheus reload succeed in the environment.
- A synthetic test alert traverses Alertmanager to the intended non-production
  page/ticket receiver and its resolution is recorded.
- Blind intervals are marked in the SLO report; no unsupported zero-fill occurs.

Escalate to observability/platform owner, then service or database owner for
endpoint/query issues. Attach active config digest, target discovery snapshot,
exporter/rule versions, permissions evidence, blind interval, synthetic delivery
evidence, and follow-up. Redact URLs or labels that contain credentials.
