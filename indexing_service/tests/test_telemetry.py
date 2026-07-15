"""Kafka-to-Typesense trace continuity and data-minimization tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


_app_dir = Path(__file__).resolve().parent.parent / "app"
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

import consumer  # noqa: E402
from checkpoint_store import InMemoryCheckpointStore  # noqa: E402
from telemetry import (  # noqa: E402
    TelemetryConfig,
    TelemetryConfigurationError,
    configure_tracing,
)


PARENT_TRACEPARENT = (
    "00-0123456789abcdef0123456789abcdef-89abcdef01234567-01"
)
KAFKA_KEY = b"secret-tenant\x1fproducts\x1fsecret-document"


def tracing_config(**overrides) -> TelemetryConfig:
    values = {
        "deployment_profile": "test",
        "sdk_disabled": False,
        "traces_exporter": "otlp",
        "service_name": "imposbro-indexing-service",
        "service_version": "4.0.0",
        "deployment_environment": "test",
        "build_id": "build-42",
        "revision": "abcdef123456",
        "endpoint": "http://collector:4318",
        "schedule_delay_millis": 100,
        "export_timeout_millis": 1000,
    }
    values.update(overrides)
    return TelemetryConfig(**values)


@pytest.fixture
def trace_capture():
    exporter = InMemorySpanExporter()
    runtime = configure_tracing(tracing_config(), span_exporter=exporter)
    try:
        yield runtime, exporter
    finally:
        runtime.shutdown()
        configure_tracing(
            tracing_config(
                sdk_disabled=True,
                traces_exporter="none",
                endpoint="",
            )
        )


class FakeDocuments:
    def __init__(self):
        self.upserted = []

    def upsert(self, document):
        self.upserted.append(dict(document))


class FakeCollections:
    def __init__(self, documents):
        self.documents = documents

    def __getitem__(self, _collection_name):
        return type("Collection", (), {"documents": self.documents})()


class FakeClient:
    def __init__(self):
        self.documents = FakeDocuments()
        self.collections = FakeCollections(self.documents)


def event_payload():
    return {
        "envelope_version": 2,
        "event_id": "secret-event-id",
        "identity": {
            "tenant_id": "secret-tenant",
            "collection": "products",
            "document_id": "secret-document",
        },
        "document_version": 1,
        "sequence": 1,
        "operation": "upsert",
        "routing_revision": 1,
        "target_clusters": ["cluster-a"],
        "occurred_at": "2026-07-10T08:00:00Z",
        "trace": {
            "request_id": "secret-request-id",
            "traceparent": PARENT_TRACEPARENT,
        },
        "document": {
            "id": "secret-document",
            "token": "must-not-be-captured",
        },
    }


def test_worker_extracts_kafka_parent_and_nests_each_typesense_write(trace_capture):
    runtime, exporter = trace_capture
    client = FakeClient()

    consumer.process_message(
        event_payload(),
        {"cluster-a": client},
        checkpoint_store=InMemoryCheckpointStore(),
        message_key=KAFKA_KEY,
        allow_legacy=False,
    )

    assert runtime.force_flush()
    spans = {span.name: span for span in exporter.get_finished_spans()}
    process_span = spans["kafka.process"]
    typesense_span = spans["typesense.document.upsert"]
    assert f"{process_span.context.trace_id:032x}" == (
        "0123456789abcdef0123456789abcdef"
    )
    assert process_span.parent is not None
    assert f"{process_span.parent.span_id:016x}" == "89abcdef01234567"
    assert typesense_span.parent is not None
    assert typesense_span.parent.span_id == process_span.context.span_id
    assert typesense_span.context.trace_id == process_span.context.trace_id
    assert typesense_span.attributes == {
        "db.system.name": "typesense",
        "db.operation.name": "upsert",
        "imposbro.target.cluster": "cluster-a",
    }
    assert process_span.resource.attributes == {
        "service.name": "imposbro-indexing-service",
        "service.version": "4.0.0",
        "deployment.environment.name": "test",
        "imposbro.build.id": "build-42",
        "imposbro.service.revision": "abcdef123456",
    }

    span_dump = repr(
        [(span.name, dict(span.attributes), span.events) for span in spans.values()]
    )
    for forbidden in (
        "secret-event-id",
        "secret-tenant",
        "secret-document",
        "secret-request-id",
        "must-not-be-captured",
    ):
        assert forbidden not in span_dump


def test_enterprise_worker_rejects_disabled_or_plaintext_exporter(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "enterprise")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")

    config = TelemetryConfig.from_env(
        default_service_name="imposbro-indexing-service",
        default_service_version="4.0.0",
    )
    with pytest.raises(TelemetryConfigurationError, match="Enterprise tracing requires"):
        config.validate()
