# Production Runbook

This runbook describes a production-oriented IMPOSBRO deployment topology,
network boundaries, and disaster-recovery drills. The Helm chart deploys only
the IMPOSBRO application workloads; Kafka, Redis, and Typesense should be
managed by their own operator, managed service, or dedicated chart.

## Reference Topology

```text
Browser / operator
  -> authenticated ingress or gateway
  -> admin-ui service
  -> query-api service
  -> Kafka bootstrap service
  -> Redis config-sync service
  -> Typesense state and data clusters

indexing-service workers
  -> query-api internal admin endpoint
  -> Kafka consumer group
  -> Typesense data clusters

Prometheus
  -> query-api metrics if instrumented externally
  -> indexing-service metrics service
```

Keep Admin UI and Query API services as `ClusterIP` behind an authenticated
Ingress, gateway, or service mesh. Do not expose the Admin UI directly when it
can inject server-side API keys.

## Ingress

Use chart-managed Ingress when the cluster ingress controller should own TLS
and HTTP routing:

```yaml
queryApi:
  ingress:
    enabled: true
    className: nginx
    hosts:
      - host: api.example.com
        path: /
        pathType: Prefix
    tls:
      - secretName: imposbro-query-api-tls
        hosts:
          - api.example.com

adminUi:
  ingress:
    enabled: true
    className: nginx
    annotations:
      nginx.ingress.kubernetes.io/auth-url: https://auth.example.com/oauth2/auth
      nginx.ingress.kubernetes.io/auth-signin: https://auth.example.com/oauth2/start
    hosts:
      - host: admin.example.com
        path: /
        pathType: Prefix
    tls:
      - secretName: imposbro-admin-ui-tls
        hosts:
          - admin.example.com
```

If the Admin UI proxy injects server-side keys, configure
`ADMIN_UI_PROXY_TRUSTED_HEADER` and make the authenticated ingress/gateway set
that header. Prefer Admin UI OIDC login when the UI is directly operator-facing.

## Network Policy

NetworkPolicy is opt-in because every cluster has different ingress-controller,
monitoring, DNS, Kafka, Redis, and Typesense labels.

Start with ingress-only controls:

```yaml
networkPolicy:
  enabled: true
  ingressController:
    from:
      - namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: ingress-nginx
        podSelector:
          matchLabels:
            app.kubernetes.io/name: ingress-nginx
  prometheus:
    from:
      - namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: monitoring
        podSelector:
          matchLabels:
            app.kubernetes.io/name: prometheus
```

The chart renders policies for:

- Query API HTTP ingress, including same-release Admin UI pods by default.
- Admin UI HTTP ingress from the configured ingress/gateway sources.
- Indexing-service metrics ingress from configured Prometheus sources.

Only add egress rules after every dependency has stable selectors or CIDRs.
Once egress is enabled, include DNS plus Kafka, Redis, Typesense state nodes,
Typesense data nodes, and any OIDC/JWKS endpoints used by bearer-token auth.

## Production Values Checklist

- Immutable images for Query API, Admin UI, and indexing service.
- `config.useSecret: true` with secrets supplied by a secure values file or
  external secret injection.
- `ALLOW_UNAUTHENTICATED_ADMIN=false` and `ALLOW_UNAUTHENTICATED_DATA=false`.
- `ADMIN_API_KEY`, `DATA_API_KEY`, `SCOPED_API_KEYS`, or OIDC configured for
  every exposed control/data-plane path.
- `ADMIN_UI_PROXY_TRUSTED_HEADER` set when the Admin UI proxy injects server
  keys, with the authenticated ingress/gateway setting that header.
- Ingress class, TLS secrets, hosts, and authentication annotations configured
  for every browser-facing endpoint.
- `CORS_ORIGINS` set to explicit browser origins.
- `REQUEST_ID_HEADER` aligned with the ingress/gateway tracing convention
  (default `X-Request-ID`).
- HPA enabled for Query API/Admin UI when CPU or memory tracks request load.
- KEDA enabled for indexing workers when Kafka lag is the scaling signal.
- PodDisruptionBudget enabled for replicated workloads after choosing
  `minAvailable` or `maxUnavailable` for the environment.
- Topology spread constraints configured for replicated workloads when the
  cluster has enough nodes or zones to make spreading meaningful.
- NetworkPolicy enabled after ingress and monitoring selectors are known.
- `monitoring.serviceMonitor.enabled=true` and
  `monitoring.prometheusRule.enabled=true` when Prometheus Operator is used.
- `make ci`, runtime smoke, and benchmark evidence captured for the release.

## Support Diagnostics

Query API echoes the configured request-correlation header on every response
and propagates the same value into Kafka ingest messages as `request_id`.
When investigating a failed ingest, capture the HTTP response
`X-Request-ID` (or the configured equivalent), then search Query API and
indexing-service logs for `request_id=<value>`.

Keep request ids out of Prometheus labels and dashboards. They are intentionally
log-only metadata so high-cardinality traffic cannot destabilize monitoring.

## Alerting

When Prometheus Operator is installed, enable chart-managed scrape and alert
resources:

```yaml
monitoring:
  serviceMonitor:
    enabled: true
    labels:
      release: prometheus
  prometheusRule:
    enabled: true
    labels:
      release: prometheus
```

The default rules cover Query API 5xx rate, Query API p95 latency, rate-limit
blocks, rate-limit backend errors, indexing DLQ messages, indexing retry bursts,
and indexing workers with no loaded data clusters. Tune thresholds per
environment; production alert thresholds should be stricter than local Docker
smoke expectations.

For public or partner-facing data-plane traffic, monitor:

- `query_api_rate_limit_checks_total{result="blocked"}` for clients exceeding
  search or ingest budgets.
- `query_api_rate_limit_backend_errors_total` for Redis-backed limiter outages.
  Decide per environment whether `RATE_LIMIT_FAIL_CLOSED=true` is appropriate.
  Fail-open preserves traffic during Redis incidents; fail-closed preserves
  abuse protection at the cost of returning 503 when the limiter is unavailable.

## Release Verification

Before promoting a production release:

```bash
make ci
make smoke-docker-load
make smoke-docker-state
```

Against the deployed cluster or a port-forward:

```bash
make benchmark-k8s
kubectl rollout status deployment/imposbro-release-imposbro-search-query-api
kubectl rollout status deployment/imposbro-release-imposbro-search-indexing-service
```

Store benchmark JSON artifacts with the release evidence.

## Disaster Recovery Drill

Run this drill before depending on backups:

1. Export a masked state snapshot and archive it for evidence.
2. Export a restore-ready snapshot with explicit secret opt-in to the approved
   secret store, not to local laptops or chat.
3. Create a temporary collection schema and alias.
4. Dry-run import the restore-ready snapshot.
5. Apply the snapshot in a non-production namespace.
6. Run collection reconciliation.
7. Verify aliases point to the expected versioned collections.
8. Ingest a small tenant-scoped document set and confirm search returns every
   document without partial failures.
9. Record recovery time and any manual step that should become automation.

The local approximation is:

```bash
make smoke-docker-state
```

## Rollback

1. Roll back application images or Helm values.
2. Roll Query API first, then indexing workers, then Admin UI.
3. Confirm `/ready` and cluster health.
4. Run `make smoke-load` or the equivalent deployed-cluster benchmark.
5. If control-plane state changed, export current state for evidence, dry-run
   the last known-good snapshot, then apply it only after confirming schema and
   alias changes are intended.
