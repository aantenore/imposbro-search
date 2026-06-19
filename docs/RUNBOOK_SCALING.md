# Horizontal Scaling Runbook

This runbook is for operating IMPOSBRO Search with multiple Query API and indexing worker replicas.

## Scaling Model

- Query API is stateless except for shared control-plane state in the internal Typesense cluster and Redis config-sync notifications.
- Indexing workers share the Kafka consumer group `imposbro_federated_indexing_group`; Kafka partitions bound useful worker parallelism.
- Typesense state and data clusters are stateful dependencies. Scale them with their own operator/chart/runbook, not by scaling the IMPOSBRO app deployments.
- Admin UI can scale horizontally, but only behind an authenticated ingress/gateway when it proxies privileged credentials.

## Precheck

1. Confirm readiness:
   ```bash
   curl -fsS http://localhost:8000/ready
   ```
2. Confirm release gate:
   ```bash
   make ci
   ```
3. Confirm runtime path before changing replica counts:
   ```bash
   make smoke-docker
   make smoke-docker-load
   ```
4. Confirm Kafka topics for high-volume collections have at least as many partitions as the planned active indexing workers.
5. Confirm `ALLOW_UNAUTHENTICATED_ADMIN=false` and `ALLOW_UNAUTHENTICATED_DATA=false` outside local-only Docker.

## Local Multi-Instance Smoke

Use the scale overlay so host ports are not published by every replica. A small nginx proxy publishes one local Query API endpoint on `127.0.0.1:8000`.

```bash
make compose-config-scale
SMOKE_SCALE_DOCUMENTS=90 SMOKE_SCALE_CONCURRENCY=12 make smoke-docker-scale
```

The smoke:

- starts `query_api` and `indexing_service` with three replicas by default;
- ingests documents through the local Query API proxy while restarting each Query API and indexing container;
- checks Query API readiness after each restart;
- checks Kafka consumer-group lag recovers to `SMOKE_LAG_BUDGET` (default `0`);
- verifies all documents are searchable with no partial shard failure.

Useful overrides:

```bash
SCALE_QUERY_API_REPLICAS=4 SCALE_INDEXING_REPLICAS=4 make smoke-docker-scale
SMOKE_SCALE_DOCUMENTS=250 SMOKE_SCALE_CONCURRENCY=24 SMOKE_TIMEOUT_SECONDS=180 make smoke-docker-scale
```

## Kubernetes Scaling

Scale Query API first when search/admin traffic is the bottleneck:

```bash
kubectl scale deployment imposbro-release-imposbro-search-query-api --replicas=3
kubectl rollout status deployment imposbro-release-imposbro-search-query-api
```

Scale indexing workers when Kafka lag or indexing convergence is the bottleneck:

```bash
kubectl scale deployment imposbro-release-imposbro-search-indexing-service --replicas=5
kubectl rollout status deployment imposbro-release-imposbro-search-indexing-service
```

Keep indexing replicas at or below useful Kafka partition parallelism. Extra replicas can help failover, but they will sit idle if there are fewer assigned partitions.

## Kubernetes Autoscaling

Use HPA for request-serving workloads when CPU or memory is the best available signal:

```yaml
queryApi:
  autoscaling:
    enabled: true
    minReplicas: 2
    maxReplicas: 6
    targetCPUUtilizationPercentage: 70

adminUi:
  autoscaling:
    enabled: true
    minReplicas: 2
    maxReplicas: 4
    targetCPUUtilizationPercentage: 70
```

Use KEDA for indexing workers when Kafka lag is the real bottleneck signal. Install KEDA and its CRDs before enabling this value:

```yaml
indexingService:
  keda:
    enabled: true
    minReplicaCount: 1
    maxReplicaCount: 10
    kafka:
      lagThreshold: "50"
      ensureEvenDistributionOfPartitions: "true"
```

When `indexingService.keda.kafka.topic` is empty, KEDA calculates lag across topics known to the consumer group. This matches the dynamic per-collection topic pattern, but production teams should confirm the consumer group has committed offsets for representative topics before relying on scale-from-zero. Keep `minReplicaCount` above `0` unless every high-volume topic has an explicit trigger.

## Lag Budget

Default acceptance for local smoke is lag returning to `0` within `SMOKE_TIMEOUT_SECONDS`.

Production guidance:

- Alert when lag grows continuously for 5 minutes.
- Alert when DLQ messages increase.
- Track indexing convergence p95 for representative collections.
- Treat retries plus lag growth as a worker or Typesense capacity incident.

For local Docker inspection:

```bash
docker compose exec -T kafka \
  /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server kafka:9092 \
  --describe \
  --group imposbro_federated_indexing_group
```

## Rolling Restart

Preferred order:

1. Query API replicas, one at a time.
2. Indexing worker replicas, one at a time.
3. Admin UI replicas.

After each step:

```bash
curl -fsS http://localhost:8000/ready
make smoke-load
```

If indexing workers restart during a burst, Kafka lag may rise temporarily. It must recover inside the configured lag budget and DLQ must remain unchanged.

## Rollback

1. Revert the app image or Helm values change.
2. Roll Query API first, then indexing workers.
3. Run:
   ```bash
   make smoke-docker-load
   make smoke-docker-state
   ```
4. If control-plane state is suspected, export current state for evidence, validate the last known good backup, then restore with dry-run followed by apply.

## Incident Checklist

- `/ready` degraded: inspect `data_cluster_nodes`, Kafka, and Redis status.
- Search partial: inspect `failed_clusters` in the response and Typesense node health.
- Lag growing: inspect worker logs, retries, DLQ, Kafka topic partitions, and Typesense write errors.
- Admin config diverged: run state export, compare snapshots, and use restore/reconcile workflows.
