# Runbook: durable audit delivery

The PostgreSQL audit chain remains authoritative even when an external sink is
unavailable. `audit_delivery_checkpoints` records one monotonic chain boundary
per bounded destination ID, exact last event hash, consecutive failure count,
redacted error code, and next retry time. It never stores sink credentials or
raw exception text.

This is a delivery contract, not a WORM or immutability claim. The security
owner must separately verify the selected sink's retention, access,
immutability, legal hold, alerting, and administrative boundary.

## Configure and run

Use a distinct least-privilege database identity and keep authorization in a
mounted mode-0400/0440 file. The endpoint cannot contain URL credentials and is
HTTPS unless an explicit development-only override is set.

```text
export CONTROL_PLANE_DATABASE_URL=<secret-managed-postgresql-url>
export AUDIT_DELIVERY_DESTINATION_ID=security-archive
export AUDIT_DELIVERY_SINK_URL=https://audit.example.internal/v1/events
export AUDIT_DELIVERY_AUTHORIZATION_FILE=/var/run/secrets/audit/authorization
python3 scripts/ops/audit_delivery_worker.py --once
```

The Query API image contains the same entrypoint as
python -m control_plane.audit_delivery_worker; the repository script is a thin
workspace wrapper. Omit --once for a bounded polling process. Batch, poll,
timeout and retry
budgets are configured by `AUDIT_DELIVERY_BATCH_SIZE`,
`AUDIT_DELIVERY_POLL_SECONDS`, `AUDIT_DELIVERY_TIMEOUT_SECONDS`,
`AUDIT_DELIVERY_BASE_RETRY_SECONDS`, and
`AUDIT_DELIVERY_MAX_RETRY_SECONDS`.

The generic sink receives `imposbro.audit.delivery.v1` JSON with the destination
ID, prior chain boundary, current audit head, and ordered events. It must be
idempotent and return the exact `destination_id`, `last_sequence`, and
`last_event_hash` durably accepted. A mismatched acknowledgement never advances
the checkpoint. Concurrent exporters may redeliver a page, but compare-and-set
advancement and a provider idempotency boundary prevent silent skipping.

## Alerts and diagnosis

- `ImposbroAuditDeliveryUnconfigured`: audit events exist with no destination.
- `ImposbroAuditDeliveryBacklog`: a destination is behind the audit head.
- `ImposbroAuditDeliveryFailure`: a redacted provider failure remains active.

Inspect only aggregate PostgreSQL exporter metrics and the checkpoint's safe
fields. Do not copy event details, database URLs, authorization headers, or sink
responses into incident chat. Check network/TLS, destination availability,
authorization version, next retry time, failure count, audit head sequence, and
the destination sequence gap.

Do not edit `last_sequence`, reset failure attempts, delete audit events, or
register a dummy destination to clear an alert. Repair the provider/configuration
and let the worker resend from the last acknowledged hash boundary.

## Recovery and closure

1. Preserve the audit head and checkpoint rows; verify the source chain before
   replay.
2. Restore sink reachability and authorization without disabling TLS or logging
   secrets.
3. Run one bounded delivery and verify the destination acknowledgement matches
   the page boundary.
4. Continue until `imposbro_audit_delivery_max_backlog` and failed destinations
   are zero for the stabilization interval.
5. Verify a synthetic non-sensitive security event in the destination and test
   page delivery. Retain its source sequence/hash and sink receipt under the
   approved evidence policy.

Escalate persistent failure to the security, observability, service, and sink
owners. Source durability alone does not satisfy external audit delivery.
