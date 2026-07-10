# Runbook: configuration revision not converged

Applies to stale-replica warnings/pages and sampled fast/sustained convergence-
proxy burn alerts. Revision telemetry must probe every Query API replica
directly. A load-balanced `/health` target can hide the stale replica and
invalidate this alert.

## Trigger and impact

One or more Query API replicas reports an applied revision lower than its
authoritative revision for two minutes (ticket) or ten minutes (page). Requests
can receive inconsistent routing, cluster, schema, or policy behavior depending
on which replica serves them. A negative gap is also anomalous even though the
recording rule clamps it to zero; inspect raw revisions.

## Immediate safety

1. Stop new control-plane mutations and pause related rollouts until all serving
   replicas agree.
2. Do not edit revision numbers, PostgreSQL state/outbox rows, Redis messages, or
   local worker state manually.
3. Preserve the authoritative revision, each replica's applied revision/state
   digest, rollout ID, outbox range, and recent admin audit events.
4. If policy/routing inconsistency can cross a tenant or security boundary,
   remove stale replicas from traffic and engage security immediately.

## Diagnose

```promql
imposbro_query_api_control_plane_authoritative_revision
```

```promql
imposbro_query_api_control_plane_applied_revision
```

```promql
imposbro:query_api:config_revision_gap
imposbro:config:stale_burn_rate_5m
imposbro:config:stale_burn_rate_1h
imposbro:config:stale_burn_rate_6h
```

Compare the database view and outbox:

```promql
imposbro_control_plane_authoritative_revision_db
max(imposbro_control_plane_outbox_pending)
max(imposbro_control_plane_outbox_oldest_unpublished_age_seconds)
```

Fetch every replica directly:

```text
GET http://<query-api-pod-or-instance>:8000/health
```

Record `control_plane.backend`, `authoritative_revision`, `applied_revision`,
`state_digest`, `converged`, cache freshness, and dependency errors. Then check:

- Prometheus discovery has exactly the intended instances and does not target a
  service virtual IP for per-replica health;
- PostgreSQL authoritative state and schema revision are healthy;
- control-plane outbox publication is ordered and draining;
- Redis notification connectivity and subscriber logs;
- replica restart/deploy timing, secret/certificate rotation, and network policy;
- configuration validation failures or a rollout paused mid-transition.

## Mitigate

- Repair the failed outbox/Redis/network/authentication path first.
- Remove only verified stale replicas from traffic when healthy capacity can
  carry load; replace them one at a time through the deployment controller.
- Roll back a correlated application/config rollout using the supported
  revision-aware workflow. Never force a revision value or copy database rows.
- If the authoritative revision itself is invalid, use the audited control-plane
  rollback procedure so a new ordered revision is created and propagated.

## Validate and close

- Every intended replica exposes the same authoritative and applied revision
  and expected state digest through direct probes.
- PostgreSQL exporter revision agrees; outbox pending/age returns to zero.
- Redis/config synchronization errors stop and worker configuration is loaded.
- Authenticated representative requests return consistent routing/policy from
  each replica.
- Prometheus discovery and JSON exporter series remain present across at least
  two evaluation intervals and a stabilization period.

Escalate to service/control-plane owner for state validation, database owner for
PostgreSQL, platform owner for discovery/networking, and security for policy
divergence. Attach per-replica sanitized health snapshots, revision/digest map,
outbox and audit ranges, deployment/config changes, mitigation, and consistency
validation.
