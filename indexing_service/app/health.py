"""Process-level liveness and readiness for the indexing worker."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple
from urllib.parse import urlsplit

import metrics


DEFAULT_HEALTH_PORT = 9109


class WorkerHealthState:
    """Thread-safe readiness state shared by bootstrap, consumer, and HTTP probe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._config_loaded = False
        self._consumer_active = False
        self._publish_metrics()

    def _publish_metrics(self) -> None:
        ready = self._config_loaded and self._consumer_active
        metrics.WORKER_CONFIG_LOADED.set(1 if self._config_loaded else 0)
        metrics.WORKER_CONSUMER_ACTIVE.set(1 if self._consumer_active else 0)
        metrics.WORKER_READY.set(1 if ready else 0)

    def reset(self) -> None:
        with self._lock:
            self._config_loaded = False
            self._consumer_active = False
            self._publish_metrics()

    def set_config_loaded(self, loaded: bool) -> None:
        with self._lock:
            self._config_loaded = bool(loaded)
            self._publish_metrics()

    def set_consumer_active(self, active: bool) -> None:
        with self._lock:
            self._consumer_active = bool(active)
            self._publish_metrics()

    def snapshot(self) -> dict:
        with self._lock:
            config_loaded = self._config_loaded
            consumer_active = self._consumer_active
        ready = config_loaded and consumer_active
        return {
            "status": "ready" if ready else "not_ready",
            "live": True,
            "ready": ready,
            "config_loaded": config_loaded,
            "consumer_active": consumer_active,
        }


WORKER_HEALTH = WorkerHealthState()


def health_response(
    path: str,
    state: WorkerHealthState = WORKER_HEALTH,
) -> Tuple[int, dict]:
    """Return status code and safe JSON payload for a probe path."""
    normalized_path = urlsplit(path).path
    snapshot = state.snapshot()
    if normalized_path == "/live":
        return 200, {"status": "live", "live": True}
    if normalized_path == "/ready":
        return (200 if snapshot["ready"] else 503), snapshot
    return 404, {"status": "not_found"}


class WorkerHealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler; probe responses never expose cluster credentials."""

    health_state = WORKER_HEALTH

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        status, payload = health_response(self.path, self.health_state)
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args) -> None:
        """Keep high-frequency Kubernetes probe requests out of worker logs."""


def _health_port_from_env() -> int:
    raw_value = os.environ.get("INDEXING_HEALTH_PORT", str(DEFAULT_HEALTH_PORT)).strip()
    try:
        port = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("INDEXING_HEALTH_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("INDEXING_HEALTH_PORT must be between 1 and 65535")
    return port


def create_health_server(
    port: int,
    state: WorkerHealthState = WORKER_HEALTH,
) -> ThreadingHTTPServer:
    """Create, but do not start, the worker probe server."""

    class BoundWorkerHealthHandler(WorkerHealthHandler):
        health_state = state

    server = ThreadingHTTPServer(("0.0.0.0", port), BoundWorkerHealthHandler)
    server.daemon_threads = True
    return server


def start_health_server(
    port: Optional[int] = None,
    state: WorkerHealthState = WORKER_HEALTH,
) -> ThreadingHTTPServer:
    """Start the worker probe server on a daemon thread."""
    server = create_health_server(port if port is not None else _health_port_from_env(), state)
    thread = threading.Thread(
        target=server.serve_forever,
        name="indexing-health",
        daemon=True,
    )
    thread.start()
    return server


def stop_health_server(server: Optional[ThreadingHTTPServer]) -> None:
    """Stop and release the worker probe server when the process exits."""
    if server is None:
        return
    server.shutdown()
    server.server_close()
