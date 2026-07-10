"""Trace extraction and HTTP-to-Kafka continuity contract."""

from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind


_app_dir = Path(__file__).resolve().parent.parent / "app"
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

from services.kafka_producer import KafkaService  # noqa: E402
from telemetry import TelemetryConfig, configure_tracing  # noqa: E402


INBOUND_TRACEPARENT = (
    "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
)


def tracing_config(**overrides) -> TelemetryConfig:
    values = {
        "deployment_profile": "test",
        "sdk_disabled": False,
        "traces_exporter": "otlp",
        "service_name": "imposbro-query-api",
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


class FakeProducer:
    def __init__(self):
        self.sent = []

    def send(self, topic, key, value):
        self.sent.append((topic, key, value))

    def flush(self):
        return None


def test_http_server_span_extracts_w3c_parent_without_request_payload(
    trace_capture,
    client,
):
    runtime, exporter = trace_capture

    response = client.get(
        "/api/v1/openapi.json?secret=must-not-be-captured",
        headers={
            "traceparent": INBOUND_TRACEPARENT,
            "authorization": "Bearer must-not-be-captured",
        },
    )
    assert response.status_code == 200
    assert runtime.force_flush()

    server_spans = [
        span for span in exporter.get_finished_spans() if span.kind == SpanKind.SERVER
    ]
    assert len(server_spans) == 1
    server = server_spans[0]
    assert f"{server.context.trace_id:032x}" == "0123456789abcdef0123456789abcdef"
    assert server.parent is not None
    assert f"{server.parent.span_id:016x}" == "0123456789abcdef"
    _version, response_trace_id, response_parent_id, _flags = response.headers[
        "traceparent"
    ].split("-")
    assert int(response_trace_id, 16) == server.context.trace_id
    assert int(response_parent_id, 16) == server.context.span_id
    assert set(server.attributes) <= {
        "http.request.method",
        "http.response.status_code",
        "http.route",
        "url.scheme",
    }
    serialized_attributes = repr(dict(server.attributes))
    assert "must-not-be-captured" not in serialized_attributes
    assert "authorization" not in serialized_attributes.lower()
    assert server.resource.attributes == {
        "service.name": "imposbro-query-api",
        "service.version": "4.0.0",
        "deployment.environment.name": "test",
        "imposbro.build.id": "build-42",
        "imposbro.service.revision": "abcdef123456",
    }


def test_kafka_envelope_uses_producer_span_as_worker_parent(trace_capture):
    runtime, exporter = trace_capture
    producer = FakeProducer()
    kafka = KafkaService("kafka:9092", "imposbro")
    kafka._producer = producer

    with runtime.span(
        "test.http.request",
        kind=SpanKind.SERVER,
        parent_carrier={"traceparent": INBOUND_TRACEPARENT},
    ):
        kafka.publish_document(
            collection_name="products",
            document={"id": "secret-document", "token": "must-not-be-captured"},
            target_clusters=["cluster-a"],
            tenant_id="secret-tenant",
            document_version=1,
            sequence=1,
            routing_revision=1,
            idempotency_key="secret-idempotency-key",
            request_id="secret-request-id",
            traceparent=INBOUND_TRACEPARENT,
        )

    assert runtime.force_flush()
    spans = {span.name: span for span in exporter.get_finished_spans()}
    server = spans["test.http.request"]
    producer_span = spans["kafka.publish"]
    assert producer_span.parent is not None
    assert producer_span.parent.span_id == server.context.span_id
    assert producer_span.context.trace_id == server.context.trace_id

    envelope = producer.sent[0][2]
    traceparent = envelope["trace"]["traceparent"]
    _version, trace_id, parent_id, _flags = traceparent.split("-")
    assert int(trace_id, 16) == producer_span.context.trace_id
    assert int(parent_id, 16) == producer_span.context.span_id
    assert envelope["trace"]["request_id"] == "secret-request-id"

    span_dump = repr(
        [(span.name, dict(span.attributes)) for span in exporter.get_finished_spans()]
    )
    for forbidden in (
        "secret-document",
        "must-not-be-captured",
        "secret-tenant",
        "secret-idempotency-key",
        "secret-request-id",
    ):
        assert forbidden not in span_dump


def test_enterprise_trace_configuration_is_fail_closed():
    with pytest.raises(Exception, match="Enterprise tracing requires"):
        tracing_config(
            deployment_profile="enterprise",
            sdk_disabled=True,
            traces_exporter="none",
            endpoint="http://collector:4318",
            build_id="development",
            revision="unknown",
        ).validate()


def test_real_otlp_http_export_is_bounded_and_uses_protobuf(monkeypatch):
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib callback name
            length = int(self.headers.get("content-length", "0"))
            captured.append(
                {
                    "path": self.path,
                    "content_type": self.headers.get("content-type"),
                    "body": self.rfile.read(length),
                }
            )
            self.send_response(200)
            self.send_header("content-type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, _format, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS",
        "authorization=Bearer%20collector-secret",
    )
    runtime = configure_tracing(
        tracing_config(endpoint=f"http://127.0.0.1:{server.server_port}")
    )
    try:
        with runtime.span("export.contract", kind=SpanKind.INTERNAL):
            pass
        assert runtime.force_flush()
    finally:
        runtime.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        configure_tracing(
            tracing_config(
                sdk_disabled=True,
                traces_exporter="none",
                endpoint="",
            )
        )

    assert len(captured) == 1
    assert captured[0]["path"] == "/v1/traces"
    assert captured[0]["content_type"] == "application/x-protobuf"
    assert captured[0]["body"]
    assert b"collector-secret" not in captured[0]["body"]
