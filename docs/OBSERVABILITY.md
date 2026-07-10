# Distributed tracing contract

IMPOSBRO uses the OpenTelemetry Python SDK and the stable OTLP/HTTP protobuf
exporter. Trace export is optional in development and mandatory in the
`enterprise` deployment profile. A repository render or unit test proves the
configuration contract; it is not evidence that a live collector accepted and
retained spans.

## Trace topology

The supported critical path is:

```text
W3C HTTP parent
  -> Query API server span
    -> Kafka producer span
      -> durable envelope trace.traceparent
        -> indexing consumer span
          -> one Typesense client span per applied target
```

The outbox stores the server context. Each send or replay creates a bounded
producer span and updates only the outbound copy, so immutable outbox payloads
are not rewritten and the worker is parented to the actual publish attempt.
Duplicate or stale events that perform no Typesense operation intentionally do
not emit a misleading datastore span.

## Data minimization

Application spans may contain only fixed operation attributes, a validated
configured target-cluster name, and release resource attributes. They do not
capture request or response bodies, query strings, authorization headers,
documents, tenant IDs, document IDs, event IDs, idempotency keys, request IDs,
exception messages, datastore URLs, or credentials. Errors record only the
exception class in `error.type`.

Resource identity is deliberately low-cardinality:

- `service.name`
- `service.version`
- `deployment.environment.name`
- `imposbro.build.id`
- `imposbro.service.revision`

`OTEL_EXPORTER_OTLP_HEADERS`, when required by the collector, must come from an
environment secret and must never be placed in Helm ConfigMaps or evidence.

## Runtime configuration

Both Python services use standard OpenTelemetry environment names for the SDK,
exporter, batching, and sampling. Helm sets `OTEL_SERVICE_NAME` per workload and
`OTEL_SERVICE_VERSION` from the chart application version. The important knobs
are documented in `.env.example` and `indexing_service/.env.example`.

Enterprise startup and Helm rendering fail unless:

- `OTEL_SDK_DISABLED=false` and `OTEL_TRACES_EXPORTER=otlp`;
- the resolved OTLP trace endpoint is credential-free HTTPS;
- batching, export, and shutdown timeouts are bounded;
- the sampling policy can produce traces;
- build and revision identity are release values, not development placeholders.

The SDK queue is bounded and uses `BatchSpanProcessor`. Shutdown performs a
bounded force-flush before processor shutdown. Export failure does not change
the data-plane result after startup; collectors and alerts must expose exporter
failure separately so observability cannot become a write-path availability
dependency.

## Verification

The automated contract is covered by:

- `query_api/tests/test_telemetry.py` for W3C HTTP extraction, resource identity,
  payload exclusion, and producer-envelope continuity;
- `indexing_service/tests/test_telemetry.py` for Kafka extraction, consumer to
  Typesense nesting, resource identity, and payload exclusion;
- `query_api/tests/test_settings.py` and worker configuration tests for
  enterprise fail-fast behavior;
- `scripts/test-helm-chart.py` for rendered settings, per-service identity, and
  enterprise guardrails.
- the required `otlp_w3c_trace_continuity` live scenario, which accepts OTLP
  protobuf over a run-specific TLS endpoint and proves one parent chain from
  inbound HTTP through durable outbox/Kafka, worker and Typesense spans.

The disposable collector capture proves application propagation and exporter
interoperability, not production telemetry custody. Release-environment
evidence must additionally verify collector authentication, tenant/access
boundaries, retention, alerting on exporter failure, and trace availability in
the organization-owned backend.
