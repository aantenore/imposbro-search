#!/usr/bin/env python3
"""Bounded TLS OTLP/HTTP capture sink for black-box E2E trace assertions."""

from __future__ import annotations

import json
import os
import re
import ssl
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)


TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_CAPTURED_SPANS = 20_000


class CaptureStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spans: deque[dict] = deque(maxlen=MAX_CAPTURED_SPANS)

    def append_request(self, request: ExportTraceServiceRequest) -> None:
        captured = []
        for resource_spans in request.resource_spans:
            service_name = "unknown"
            for attribute in resource_spans.resource.attributes:
                if attribute.key == "service.name":
                    service_name = attribute.value.string_value or "unknown"
                    break
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    captured.append(
                        {
                            "trace_id": bytes(span.trace_id).hex(),
                            "span_id": bytes(span.span_id).hex(),
                            "parent_span_id": bytes(span.parent_span_id).hex(),
                            "name": span.name[:256],
                            "service_name": service_name[:128],
                            "kind": int(span.kind),
                        }
                    )
        with self._lock:
            self._spans.extend(captured)

    def by_trace_id(self, trace_id: str) -> list[dict]:
        with self._lock:
            return [dict(span) for span in self._spans if span["trace_id"] == trace_id]


STORE = CaptureStore()


class Handler(BaseHTTPRequestHandler):
    server_version = "IMPOSBRO-OTLP-E2E/1"

    def _send_json(self, status: int, payload: object) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, {"ok": True})
            return
        if parsed.path == "/jwks":
            try:
                with open(
                    os.environ.get("E2E_OIDC_JWKS_FILE", "/tls/oidc-jwks.json"),
                    encoding="utf-8",
                ) as handle:
                    jwks = json.load(handle)
            except (OSError, json.JSONDecodeError):
                self._send_json(500, {"error": "jwks_unavailable"})
                return
            self._send_json(200, jwks)
            return
        if parsed.path != "/spans":
            self._send_json(404, {"error": "not_found"})
            return
        trace_id = parse_qs(parsed.query).get("trace_id", [""])[0].lower()
        if not TRACE_ID.fullmatch(trace_id):
            self._send_json(400, {"error": "invalid_trace_id"})
            return
        spans = STORE.by_trace_id(trace_id)
        self._send_json(200, {"trace_id": trace_id, "spans": spans})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if urlparse(self.path).path != "/v1/traces":
            self._send_json(404, {"error": "not_found"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "invalid_content_length"})
            return
        if content_length < 1 or content_length > MAX_REQUEST_BYTES:
            self._send_json(413, {"error": "request_size_rejected"})
            return
        request = ExportTraceServiceRequest()
        try:
            request.ParseFromString(self.rfile.read(content_length))
        except Exception:
            self._send_json(400, {"error": "invalid_otlp_protobuf"})
            return
        STORE.append_request(request)
        encoded = ExportTraceServiceResponse().SerializeToString()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    host = os.environ.get("E2E_OTLP_HOST", "0.0.0.0")
    port = int(os.environ.get("E2E_OTLP_PORT", "4318"))
    certificate = os.environ.get("E2E_OTLP_CERT_FILE", "/tls/server.crt")
    private_key = os.environ.get("E2E_OTLP_KEY_FILE", "/tls/server.key")
    server = ThreadingHTTPServer((host, port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, private_key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever(poll_interval=0.25)


if __name__ == "__main__":
    main()
