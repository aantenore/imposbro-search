# Runbook: durable outbox lag

Applies to control-plane and indexing outbox backlog/critical alerts, including
the sampled fast/sustained lag-proxy burn alerts. The two outboxes preserve
different ordering domains; never delete or manually mark an event published to
clear an alert.

## Trigger and impact

- Indexing outbox pending for ten minutes, oldest event over five minutes,
  backlog over 100, or repeated replay failure: accepted upserts/deletes may not
  reach Kafka and search results become stale.
- Control-plane event older than one minute or backlog over ten: replicas and
  workers may use stale routing/configuration. At five minutes or 100 events the
  alert pages.
- Any new worker DLQ publication creates a ticket. The default primary consumer
  group warns above 100 records for ten minutes and pages above 1,000 for five;
  any resolver-group lag above zero for five minutes pages and blocks cutover.

Record which outbox, oldest age, pending count, first affected event/revision,
source instance, deploy revision, and Kafka/control-plane health.

## Immediate safety

1. Do not run `DELETE`, set `published_at`, reset sequence/head rows, replay a
   payload manually, or disable idempotency/checkpoint fencing.
2. Pause configuration writes when control-plane order cannot converge. Reduce
   ingest acceptance if indexing backlog threatens database or Kafka capacity.
3. Preserve the oldest row metadata and `last_error`, but redact `payload_json`
   and identifiers from broad incident channels.
4. Confirm the exporter itself is healthy before interpreting a zero or stale
   series.

## Diagnose

```promql
max(imposbro_control_plane_outbox_pending)
max(imposbro_control_plane_outbox_oldest_unpublished_age_seconds)
max(indexing_event_outbox_pending{job="imposbro_services"})
max(imposbro_indexing_event_outbox_pending_db)
max(imposbro_indexing_event_outbox_oldest_unpublished_age_seconds)
sum(increase(indexing_event_outbox_replay_failures_total[10m]))
imposbro:outbox:lag_burn_rate_5m
imposbro:outbox:lag_burn_rate_1h
imposbro:outbox:lag_burn_rate_6h
sum(increase(indexing_dlq_messages_total{job="indexing_service"}[10m]))
sum(kafka_consumergroup_lag{job="imposbro_kafka",consumergroup="imposbro_federated_indexing_group"})
sum(kafka_consumergroup_lag{job="imposbro_kafka",consumergroup="imposbro_indexing_dlq_resolver"})
```

Using a read-only PostgreSQL session, inspect metadata only:

```sql
SELECT count(*) AS pending,
       min(created_at) AS oldest_created_at,
       max(publish_attempts) AS max_attempts
FROM control_plane_outbox
WHERE published_at IS NULL;

SELECT revision, event_type, created_at, publish_attempts, last_error
FROM control_plane_outbox
WHERE published_at IS NULL
ORDER BY revision
LIMIT 20;

SELECT count(*) AS pending,
       min(created_at) AS oldest_created_at,
       max(publish_attempts) AS max_attempts
FROM indexing_event_outbox
WHERE published_at IS NULL;

SELECT event_id, sequence, created_at, publish_attempts, last_error
FROM indexing_event_outbox
WHERE published_at IS NULL
ORDER BY created_at, event_id
LIMIT 20;
```

Correlate with Kafka broker/producer health, topic authorization, quota/disk,
network/TLS, Query API logs by request/event ID, worker readiness, DLQ growth,
database locks, connection pool saturation, and config revision gap. Compare the
application gauge with the database exporter; disagreement may indicate a stale
health probe or wrong database target.

## Mitigate

- Restore broker/network/authentication availability using the approved
  dependency procedure.
- Roll back a correlated producer/event-schema change while retaining durable
  rows.
- Scale only the supported publisher/worker path after confirming ordering,
  idempotency, and downstream capacity.
- Stop new configuration mutations until the control-plane backlog drains in
  revision order.
- Rate-limit ingest before PostgreSQL disk or connection exhaustion. Maintain
  enough headroom for recovery and WAL.
- For a poison event, preserve it and follow an approved quarantine/replay plan
  with owner, payload schema validation, and idempotency proof. Do not improvise
  SQL updates.

## DLQ quarantine, replay, and offset commit

Routing cutover and completion measure unresolved DLQ work as consumer-group lag
for `imposbro_indexing_dlq_resolver`; any value above zero blocks the transition.
The reference Prometheus rules use the default group names above. If a deployment
overrides either application setting, its rendered monitoring configuration must
replace the group selectors and retain equivalent tests before promotion.
The repository provides `scripts/ops/dlq_resolver.py`, a manual single-record
CLI backed by `kafka-python-ng`. It is not an automatic resolver service and has
no implicit scan, bulk, subscription, or offset-reset mode. Do not reset the
group to the log end or commit merely to make the gate pass.

Kafka connection and TLS/SASL values come only from the environment. Set
`KAFKA_BOOTSTRAP_SERVERS` and an explicit `KAFKA_SECURITY_PROTOCOL`. Enterprise
use should set `SASL_SSL`, SASL mechanism/credentials, a readable
`KAFKA_SSL_CAFILE`, and optionally a matched client certificate/key. Hostname
verification cannot be disabled. Plaintext requires the separate
`DLQ_RESOLVER_ALLOW_INSECURE_KAFKA=true` opt-in and is not an enterprise
steady-state configuration. Never place credentials in CLI arguments or
evidence.

Follow this partition-ordered procedure:

1. Keep the rollout before cutover. Record DLQ topic end offsets and committed
   offsets for every partition for group `imposbro_indexing_dlq_resolver`, plus
   rollout/config revision and incident ID. A missing group commit means the
   retained partition starts unresolved; never bootstrap it to `latest` as a
   shortcut.
2. Inspect exactly one record without committing. The expected source topic is
   operator-supplied and must equal the wrapper; the evidence path must be new:

   ```text
   python3 scripts/ops/dlq_resolver.py inspect \
     --dlq-topic imposbro_search_sharded_dlq \
     --partition 0 --offset 42 \
     --expected-source-topic imposbro_search_sharded_products \
     --evidence evidence/dlq-0-42-inspect.json
   ```

   Evidence contains topic/partition/offset, offset state and wrapper/message/
   key/error digests, but no payload, raw key, error message, or credential.
   Preserve it under approved access, immutability, and retention controls.
3. Validate the wrapper fields (`source_topic`, `message`, optional base64
   `message_key`, `source_partition`, `source_offset`), envelope version,
   tenant/collection authorization, operation, identity, sequence, target set,
   and current routing revision. Establish the root cause before choosing a
   disposition.
4. For a recoverable event, fix the dependency/configuration and validate the
   envelope, original key, and next-unresolved offset without side effects:

   ```text
   python3 scripts/ops/dlq_resolver.py dry-run \
     --dlq-topic imposbro_search_sharded_dlq \
     --partition 0 --offset 42 \
     --expected-source-topic imposbro_search_sharded_products \
     --evidence evidence/dlq-0-42-dry-run.json
   ```

   Only envelope v2 with a Kafka key exactly matching tenant, collection, and
   document identity is replayable. The inner `message` is unchanged; recovery
   metadata is carried in separate Kafka headers. Never publish the DLQ wrapper
   or invent a later sequence.
5. Replay remains uncommitted unless `--commit` is present. This supports
   observing downstream processing without clearing the DLQ record:

   ```text
   python3 scripts/ops/dlq_resolver.py replay \
     --dlq-topic imposbro_search_sharded_dlq \
     --partition 0 --offset 42 \
     --expected-source-topic imposbro_search_sharded_products \
     --confirm REPLAY:imposbro_search_sharded_dlq:0:42 \
     --evidence evidence/dlq-0-42-replay.json
   ```

   For final idempotent replay and resolution, add `--commit` and use the
   stronger confirmation:

   ```text
   python3 scripts/ops/dlq_resolver.py replay \
     --dlq-topic imposbro_search_sharded_dlq \
     --partition 0 --offset 42 \
     --expected-source-topic imposbro_search_sharded_products \
     --commit \
     --confirm REPLAY-AND-COMMIT:imposbro_search_sharded_dlq:0:42 \
     --evidence evidence/dlq-0-42-replay-commit.json
   ```

   The CLI calls `send().get()`, then `flush()`, and only after both succeed
   commits offset `43` for the fixed resolver group. Ack/flush failure, an exact
   selector mismatch, skipped earlier offset, malformed wrapper, or invalid key
   produces failure evidence and no commit.
6. For a permanently invalid, unauthorized, or obsolete record, do not replay.
   Obtain data/service-owner approval (plus security for authorization
   failures). A non-replay disposition requires a reason code, approver,
   `--commit`, and exact confirmation:

   ```text
   python3 scripts/ops/dlq_resolver.py disposition \
     --dlq-topic imposbro_search_sharded_dlq \
     --partition 0 --offset 42 \
     --expected-source-topic imposbro_search_sharded_products \
     --reason obsolete-after-newer-tombstone --approver data-owner \
     --commit \
     --confirm DISPOSITION-AND-COMMIT:imposbro_search_sharded_dlq:0:42 \
     --evidence evidence/dlq-0-42-disposition.json
   ```

   Kafka commits acknowledge all earlier partition offsets, so the tool refuses
   a commit unless the selector equals the group's next unresolved offset. It
   never resets offsets.
7. After replay, verify the normal worker's durable checkpoint/idempotency
   decision and final Typesense state on every intended target. A stale or
   duplicate decision is acceptable only when the later state already satisfies
   the requested operation. Keep the rollout blocked on validation failure.
8. Re-read committed/end offsets from Kafka. The cutover gate may be retried
   only when aggregate resolver-group lag is exactly zero, no new DLQ record has
   appeared during the stabilization interval, primary consumer lag/outbox
   pending are within policy, parity is revalidated, and all quarantine cases
   have an approved disposition.

If the single-record CLI cannot resolve a record safely, leave the gate closed
and escalate; do not substitute a console-consumer commit or offset reset. A
future automatic service requires separate authentication, authorization,
quarantine storage, downstream proof, concurrency fencing, audit logging,
observability, and integration tests; this CLI makes no automatic-service claim.

## Validate recovery

- Oldest age trends monotonically toward zero and pending count drains.
- Replay failure rate stops; Kafka acknowledgements and worker consumption are
  healthy.
- Control-plane authoritative/applied revisions converge on every replica.
- Indexing checkpoints advance without duplicate mutations or DLQ growth.
- Resolver-group DLQ lag is exactly zero with partition offset evidence, and
  every quarantined record has a replayed or approved non-replay disposition.
- Representative documents reflect ordered upsert/delete behavior.
- Database disk, WAL, locks, and connection usage return to normal.

Keep the incident open if the backlog is merely stable. After recovery, record
the oldest-event recovery time, affected event/revision range, whether any
customer data was stale, database/Kafka evidence, mitigation, and replay
validation. Escalate to streaming, database, service, and data owners as
appropriate; involve security when payload authorization or unexpected access
failure is suspected.
