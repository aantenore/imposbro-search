# Production Runbook

This runbook describes a production-oriented IMPOSBRO deployment topology,
network boundaries, and disaster-recovery drills. The Helm chart deploys only
the IMPOSBRO application workloads. PostgreSQL, Kafka, Redis, Typesense, the
IdP, secret manager and telemetry backend remain separately operated
dependencies with their own HA, retention and recovery evidence.

## Reference Topology

```text
Browser / operator
  -> authenticated ingress or gateway
  -> admin-ui service
  -> query-api service

query-api service
  -> PostgreSQL (revisioned state, audit, control/indexing outboxes)
  -> Kafka producer
  -> Redis (notification acceleration and distributed quotas)
  -> Typesense data clusters
  -> OIDC JWKS and OTLP/HTTP collector

indexing-service workers
  -> query-api internal admin endpoint
  -> Kafka consumer group
  -> Typesense data clusters
  -> PostgreSQL fenced checkpoints
  -> OTLP/HTTP collector

Prometheus
  -> query-api metrics/health exporter
  -> indexing-service metrics service
  -> PostgreSQL exporter and Alertmanager
```

Keep Admin UI and Query API services as `ClusterIP` behind an authenticated
Ingress, gateway, or service mesh. Enterprise Admin UI uses its own OIDC
session and is forbidden from injecting server-side Query API keys.

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

Prefer Admin UI OIDC login when the UI is directly operator-facing. Static
server-key injection is disabled by default in production. A temporary legacy
deployment must explicitly set `ADMIN_UI_SERVER_CREDENTIAL_MODE` to
`trusted-header-legacy`, enable `ADMIN_UI_ALLOW_LEGACY_TRUSTED_HEADER`, configure
the exact trusted header/value, and prove the authenticated edge removes every
caller-supplied copy before setting its own.

## Network Policy

NetworkPolicy is configurable because every cluster has different
ingress-controller, monitoring, DNS and dependency selectors. The enterprise
profile requires it, including explicit egress and health-probe sources; an
empty or implicit boundary fails Helm rendering.

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

Model egress only after every dependency has stable selectors or CIDRs. Include
DNS plus PostgreSQL, Kafka, Redis, Typesense data nodes, OIDC/JWKS, OTLP and any
external secret/mesh endpoints reached by a workload. A legacy Typesense state
reader may be allowed only during its bounded migration window.

## Production Values Checklist

- Digest-pinned `@sha256` images for Query API, Admin UI, and indexing service.
- Exactly one pre-created Secret or External Secrets Operator source, with no
  inline `config.useSecret` values. Declare `secrets.providedKeys`, explicit
  `secrets.workloadKeys` scopes, and a non-secret `secrets.rolloutVersion`.
- PostgreSQL control-plane/event stores, PostgreSQL fenced worker checkpoints,
  serialized Alembic migration and a verified private trust bundle.
- `ALLOW_UNAUTHENTICATED_ADMIN=false` and `ALLOW_UNAUTHENTICATED_DATA=false`.
- `ADMIN_API_KEY`, `DATA_API_KEY`, `SCOPED_API_KEYS`, or OIDC configured for
  every exposed control/data-plane path.
- HTTPS `ADMIN_UI_PUBLIC_ORIGIN` configured to the exact browser origin; unsafe
  BFF methods will fail closed without matching Origin and Fetch Metadata.
- `ADMIN_UI_SERVER_CREDENTIAL_MODE=disabled` (the default) for production, or
  all documented legacy opt-ins plus edge header-stripping evidence when a
  temporary trusted-header migration is unavoidable.
- HTTPS OIDC issuer, JWKS, authorization, and token endpoints unless a local-only
  Admin UI explicitly enables the loopback-only development escape hatch.
- Mandatory Admin UI OIDC issuer, exact callback under the public origin,
  endpoint-origin allowlist where the provider uses split hosts, and a bounded
  OIDC fetch timeout. See `docs/ADMIN_UI_SECURITY.md`.
- `INTERNAL_STATE_PROTOCOL=https`, `DEFAULT_DATA_CLUSTER_PROTOCOL=https`, and
  `DEFAULT_DATA2_CLUSTER_PROTOCOL=https` for TLS-enabled Typesense endpoints.
  Registered clusters select the same `http|https` value in the Admin UI/API.
  Mount private roots with `tls.trustBundle`; configure PostgreSQL verification,
  Redis `rediss://`, and Kafka `SASL_SSL` with hostname verification. Provider-
  specific mTLS still requires the documented client-certificate adapter.
- OTLP/HTTP tracing over credential-free HTTPS endpoint configuration, with
  exporter headers supplied only through component-scoped Secret references and
  release/build/environment identity set to non-placeholder values.
- Ingress class, TLS secrets, hosts, and authentication annotations configured
  for every browser-facing endpoint.
- `CORS_ORIGINS` set to explicit browser origins.
- `REQUEST_ID_HEADER` aligned with the ingress/gateway tracing convention
  (default `X-Request-ID`).
- `READINESS_POLICY=strict` in enterprise. A separate production profile may
  deliberately choose `serving` for partial search, but must document the
  customer-facing degraded contract and alert on every partial response.
- HPA enabled for Query API/Admin UI when CPU or memory tracks request load.
- KEDA enabled for indexing workers when Kafka lag is the scaling signal.
- PodDisruptionBudget enabled for replicated workloads after choosing
  `minAvailable` or `maxUnavailable` for the environment.
- Topology spread constraints configured for replicated workloads when the
  cluster has enough nodes or zones to make spreading meaningful.
- NetworkPolicy enabled with explicit ingress, egress and probe sources.
- `monitoring.serviceMonitor.enabled=true` and
  `monitoring.prometheusRule.enabled=true` when Prometheus Operator is used.
- `make ci`, exact-set live integration, Kind disruption/load, encrypted clean
  restore, composite assurance and hosted CI evidence bound to the release
  commit.

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

The default rules cover multi-window availability/latency burn, missing
targets/telemetry, control-plane revision divergence, control/indexing outbox
age and backlog, worker readiness, retry/DLQ growth and quota backend failure.
Every alert name maps exactly once to `docs/runbooks/README.md`; validate rules,
dashboard PromQL and Alertmanager syntax with `make ops`.

For public or partner-facing data-plane traffic, monitor:

- `query_api_rate_limit_checks_total{result="blocked"}` for clients exceeding
  search or ingest budgets.
- `query_api_rate_limit_backend_errors_total` for Redis-backed limiter outages.
  Enterprise requires `RATE_LIMIT_FAIL_CLOSED=true`; a less strict production
  profile must explicitly accept the abuse-protection tradeoff.

## Release Verification

Before promoting a production release:

```bash
make ci
E2E_DEPENDENCY_MODE=compose scripts/e2e/run-enterprise-e2e.sh
DR_EVIDENCE_BASENAME="dr-candidate-$(date -u +%Y%m%dT%H%M%SZ).json" \
  ops/dr/run-live-postgres-dr.sh
scripts/e2e/run-kind-enterprise-smoke.sh
```

Against the deployed cluster or a port-forward:

```bash
make benchmark-k8s
kubectl rollout status deployment/imposbro-release-imposbro-search-query-api
kubectl rollout status deployment/imposbro-release-imposbro-search-indexing-service
```

Store live, benchmark, disruption and DR JSON/checksum artifacts with the
release evidence. The scheduled workflow reruns them on clean hosted runners
and emits a composite exact-commit assurance manifest.

`/health` remains the dependency-health source of truth. Enterprise uses strict
readiness. If a deliberate non-enterprise `serving` profile keeps `/ready` at
HTTP 200 while degraded, inspect PostgreSQL revision convergence, outboxes,
Redis, Kafka, Typesense nodes and partial-search metadata before closure.

## Disaster Recovery Drill

Plaintext state export is not an enterprise backup. Follow
`docs/runbooks/postgres-backup-restore.md`: freeze or account for writes, create
a PostgreSQL custom archive encrypted directly to an approved `age` recipient,
verify its manifest/checksum, restore transactionally into an empty isolated
database, verify Alembic revision/state/audit/outbox/checkpoint fingerprints,
then run application and deletion-suppression validation before serving
traffic. Secrets remain under the external secret manager's recovery process.

The isolated real-tool drill is:

```bash
DR_EVIDENCE_BASENAME="dr-drill-$(date -u +%Y%m%dT%H%M%SZ).json" \
  ops/dr/run-live-postgres-dr.sh
```

It measures configurable RPO/RTO, real `age` encryption, clean restore,
non-empty audit-chain preservation and owned-outbox retention. It does not
replace managed-region failover, off-site immutability, key-custody recovery,
Kafka/Typesense recovery or production deletion/restore-suppression evidence.

## Rollback

1. Freeze control-plane mutations and capture revisions/outbox/checkpoint
   positions before any rollback.
2. Roll back digest-pinned application images or Helm values: Query API first,
   then indexing workers, then Admin UI. Never downgrade PostgreSQL schema until
   the migration-specific rollback/restore contract proves it safe.
3. Confirm strict readiness, revision convergence, Kafka lag, DLQ, trace and
   audit delivery.
4. Run the tenant-scoped smoke and the candidate's threshold profile.
5. If durable state is corrupt, preserve current evidence and execute the
   guarded encrypted restore into an empty target. Do not overwrite newer state
   with an unversioned snapshot or reintroduce deleted data from backup.
