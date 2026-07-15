# Enterprise monitoring integration

These files are deployment-neutral reference contracts. The existing local
Compose monitoring remains intentionally unchanged; copying this directory does
not deploy exporters, reload Prometheus, configure receivers, or prove paging.

## Components

| Component | Repository artifact | Required live wiring |
|---|---|---|
| Prometheus | `prometheus/prometheus.enterprise.yml` | Mount as active config, mount `prometheus/rules/`, discover every service target, connect Alertmanager |
| Query API metrics | Runtime `/metrics` | Scrape every replica with job `imposbro_services` |
| Worker metrics | Runtime port `9108` | Scrape every worker with job `indexing_service` |
| Kafka consumer lag | Standard `kafka_consumergroup_lag` exporter contract | Run a Kafka exporter as job `imposbro_kafka` with a read-only monitoring principal and expose the configured primary/DLQ resolver groups |
| Per-replica revisions | `json-exporter/imposbro-query-health.yml` | Run a compatible `json_exporter`; probe each replica's `/health` directly, never only a service VIP |
| PostgreSQL outbox age | `postgres-exporter/queries.yml` | Run a compatible `postgres_exporter` with the custom-query path and a TLS/read-only database identity |
| Recording/alert rules | `prometheus/rules/imposbro-enterprise.rules.yml` | Load successfully, retain evaluation history, and route alerts |
| Rule tests | `prometheus/tests/imposbro-enterprise.rules.test.yml` | Run with the Prometheus version pinned by the deployment |
| Alert routing | `alertmanager/alertmanager.enterprise.example.yml` | Replace generic webhooks with supported pager/ticket adapters and secret-file URLs |
| Dashboard | `grafana/provisioning/dashboards/imposbro-enterprise-sre.json` | Provision against the intended Prometheus datasource and verify every panel has data |
| Manual DLQ resolution | `../scripts/ops/dlq_resolver.py` | Install pinned `kafka-python-ng`; inject fail-closed TLS/SASL environment; operate one explicit record and retain redacted evidence |

Exporter flags and image versions belong in environment deployment
configuration. For exporter versions that support these upstream interfaces,
the JSON exporter receives its module file as its config, while the PostgreSQL
exporter receives `queries.yml` through its custom/extended-query option.
Validate the exact flags against the pinned exporter version; do not silently
upgrade across a removed or deprecated custom-query interface.

The checked-in Kafka selectors use the default groups
`imposbro_federated_indexing_group` and `imposbro_indexing_dlq_resolver`.
Deployments that override either application setting must render the same
values into their Prometheus rules/dashboard and rerun the rule tests; a label
mismatch must be treated as missing telemetry, not zero lag.

The PostgreSQL exporter role requires only `CONNECT`, schema `USAGE`, and
`SELECT` on `control_plane_state`, `control_plane_outbox`, and
`indexing_event_outbox`. It must not own schema, write application rows, bypass
row policy, or expose a DSN in source. Inject connection material through the
platform secret mechanism and enforce certificate verification.

## Release validation

Run:

```text
scripts/ops/validate-ops-artifacts.sh
```

When `promtool` and `amtool` are installed, the validator checks PromQL, rule
unit tests, Prometheus configuration, dashboard queries, and Alertmanager
configuration. Generic YAML/JSON and backup guardrail tests run independently.

The manual DLQ resolver's fake-Kafka safety suite is dependency-free and can run
without a broker:

```text
python3 -m unittest discover -s scripts/ops/tests -p 'test_dlq_resolver.py' -v
```

It verifies no commit on acknowledgement or validation failure, exact
`offset + 1` commits, `send().get()` and `flush()` ordering, uncommitted dry-run/
replay defaults, malformed wrapper rejection, disposition approval, TLS/SASL
fail-closed configuration, and evidence redaction. A passing unit suite is not
a live Kafka resolution drill.

Before declaring the environment monitored, also capture:

1. active Prometheus/Alertmanager config digest and successful reload;
2. target-discovery evidence for every Query API/worker replica and the
   PostgreSQL/Kafka exporters;
3. read-only exporter permission test and series freshness;
4. rule evaluation with no errors across a stabilization interval;
5. Grafana panels for availability, latency, traffic/error, outbox, worker, DLQ,
   and configuration convergence;
6. synthetic page and ticket delivery plus resolved notifications;
7. an intentional missing-target/exporter drill that reaches the correlated
   runbook.

The SLO definitions and limits are in `../docs/SLO.md`. Static validation cannot
replace these live checks.
