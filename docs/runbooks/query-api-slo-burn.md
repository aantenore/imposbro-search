# Runbook: Query API error-budget burn

Applies to availability and latency burn-rate alerts and the elevated 5xx
ticket. Objectives and exclusions are defined in `docs/SLO.md`. Repository
rules are static until deployed and live alert delivery is tested.

## Trigger and impact

A page means short and long windows agree that the 30-day budget is being
consumed rapidly. A ticket means a slower trend can still exhaust the budget.
Possible impact includes failed search/ingest/delete requests, excessive
latency, partial federated results that are not captured by the status-only
availability SLI, or lost redundancy.

Declare an incident for any page, visible customer impact, correlated target
down alert, or a rising burn rate after mitigation. Record which SLO fired and
do not average availability and latency into one result.

## Immediate safety

1. Freeze risky deploys and configuration changes; identify the last known-good
   application and control-plane revisions.
2. Do not widen timeouts or retry counts blindly. That can convert dependency
   errors into thread/connection exhaustion and worsen tail latency.
3. Preserve request IDs, route templates, status family, dependency latency,
   and rollout timestamps. Never add tenant/document labels to metrics.
4. Confirm traffic exists. Missing telemetry is not a zero burn rate.

## Diagnose

```promql
imposbro:query_api:request_rate_5m
```

```promql
imposbro:query_api:availability_burn_rate_5m
or imposbro:query_api:availability_burn_rate_1h
or imposbro:query_api:availability_burn_rate_6h
```

```promql
imposbro:query_api:latency_burn_rate_5m
or imposbro:query_api:latency_burn_rate_1h
or imposbro:query_api:latency_burn_rate_6h
```

Break down errors without introducing high-cardinality dimensions:

```promql
sum by (handler, method, status) (
  rate(http_requests_total{job="imposbro_services",handler=~"(/api/v1)?/(search|documents|ingest)/.*"}[5m])
)
```

```promql
histogram_quantile(
  0.99,
  sum by (le, handler) (
    rate(http_request_duration_seconds_bucket{job="imposbro_services",handler=~"(/api/v1)?/(search|documents|ingest)/.*"}[5m])
  )
)
```

Compare instances, methods, route families, dependency health, outbox lag,
worker readiness, Typesense node health, and configuration revision gap. Check
recent application, routing, schema, secret, certificate, and infrastructure
changes. Sample logs by request ID and error class; do not copy payloads.

For low traffic, inspect raw numerator and denominator. A few requests can
produce a large ratio while the page threshold intentionally remains gated.
Use an approved synthetic journey to distinguish idle from outage.

## Mitigate

- Roll back a correlated release/configuration through the normal controller.
- Remove a demonstrably unhealthy replica only when healthy capacity remains.
- Shed optional or abusive load using configured rate limits and platform
  controls; preserve authenticated core journeys.
- For a dependency bottleneck, use its approved failover/capacity procedure and
  bound retries/timeouts.
- If one Typesense shard is unhealthy, preserve correctness and make partial
  behavior explicit; do not hide failures solely to improve the status SLI.
- If ingest causes downstream saturation, reduce acceptance rate before Kafka or
  PostgreSQL fills. Never discard outbox or DLQ data as mitigation.

## Validate and close

- Both short and long burn-rate windows fall below `1x` and continue downward.
- Error ratio, p95/p99, target health, saturation, outbox lag, and worker
  readiness are normal for the same interval.
- Representative authenticated journeys pass and partial-result behavior is
  checked separately.
- Redundancy and capacity are restored; rollback alone is not closure.
- Incident intervals and any proposed SLO exclusion have explicit owner and
  approval. Raw compliance is retained even when an adjusted report is made.

Attach alert labels/fingerprint, exact PromQL interval, request/error counts,
route breakdown, deploy/config revisions, dependency findings, mitigation,
customer impact, and follow-up. Escalate to the service owner for SLO policy,
platform owner for infrastructure, and the relevant dependency owner. At budget
exhaustion follow the release-freeze policy in `docs/SLO.md`.
