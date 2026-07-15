# Runbook: service or worker unavailable

Applies to `ImposbroQueryApiTargetDown` and
`ImposbroIndexingWorkerNotReady`. This is a static procedure; validate commands,
permissions, and escalation contacts in each environment before relying on it.

## Trigger and likely impact

- Query API `up == 0` for two minutes: one scrape target is unreachable. Impact
  ranges from lost redundancy to total data-plane outage.
- No indexing worker reports ready for five minutes: new upsert/delete events
  may remain in Kafka/outbox and search results can become stale, while existing
  searches may still work.

Start an incident when all replicas are affected, error-budget pages accompany
the alert, backlog grows, or customer journeys fail. Record the alert start UTC,
environment, deployment/config revision, and recent changes.

## Immediate safety

1. Do not restart every replica simultaneously. Preserve at least one healthy
   instance and capture logs/metrics before mutation.
2. Pause rollout or automatic scaling changes if the alert began with a deploy.
3. Do not purge Kafka, outbox, checkpoints, Redis, or Typesense data.
4. If ingest acceptance continues while every worker is down, confirm durable
   outbox/Kafka capacity; rate-limit or pause ingest before storage exhaustion.

## Diagnose

Use the Enterprise SRE dashboard and compare:

```promql
up{job=~"imposbro_services|indexing_service"}
```

```promql
sum by (status) (
  rate(http_requests_total{job="imposbro_services",handler=~"(/api/v1)?/(search|documents|ingest)/.*"}[5m])
)
```

```promql
indexing_worker_ready{job="indexing_service"}
indexing_worker_config_loaded{job="indexing_service"}
indexing_worker_consumer_active{job="indexing_service"}
```

Probe each instance directly, not only the load-balanced service:

```text
GET http://<query-api-instance>:8000/health
GET http://<query-api-instance>:8000/ready
GET http://<query-api-instance>:8000/metrics
```

For the worker, fetch `http://<worker-instance>:9108/metrics`. Determine whether
the failure is discovery/networking, process crash, readiness policy, missing
configuration, Kafka consumer, PostgreSQL, Redis, or Typesense. Correlate the
first failure with deploy, secret rotation, certificate, quota, and dependency
events. Keep credentials and response payloads out of the incident channel.

## Mitigate

- **Bad rollout:** halt it and roll back to the last verified artifact and
  configuration using the platform's normal deployment controller.
- **Single failed replica:** remove only that replica from traffic, capture
  evidence, and replace it. Confirm remaining capacity before action.
- **Dependency outage:** fail over through the approved dependency procedure;
  do not bypass TLS, authentication, durable checkpoints, or ordering guards.
- **Worker configuration not loaded:** repair the authoritative configuration
  or access path; do not mark readiness healthy manually.
- **Kafka consumer inactive:** verify broker reachability, group state, topic
  authorization, and checkpoint backend. Restart one worker only after the
  cause is understood.
- **Capacity exhaustion:** stop nonessential changes, increase approved capacity,
  and protect durable queues. Never delete backlog to make an alert green.

## Validate recovery

For at least two evaluation intervals plus the service stabilization period:

- every intended target reports `up == 1`;
- Query API `/ready` matches the approved readiness policy;
- every worker reports config loaded, consumer active, and ready;
- availability/latency burn rates return below `1x` or continue declining;
- outbox age and pending counts converge instead of merely stabilizing;
- representative authenticated search, read, ingest, and delete journeys pass;
- no new DLQ or replay-failure spike appears.

Close or downgrade the incident only after redundancy is restored. A single
healthy instance is mitigation, not full recovery.

## Escalation and evidence

Escalate to platform networking for scrape/DNS/TLS failures, streaming owner for
Kafka/group failures, database owner for PostgreSQL, and search owner for
Typesense quorum. Engage security for unexpected authorization or secret
failures. Attach sanitized direct-probe results, dependency status, replica
counts, logs by request ID, rollout revision, dashboard interval, mitigation,
and validation. Open a regression test or monitor improvement for every gap.
