"""Executable lifecycle for the provider-neutral durable audit exporter."""

from __future__ import annotations

import argparse
import os
import signal
import time
from pathlib import Path

from .audit_delivery import AuditDeliveryCoordinator, AuditDeliveryRepository
from .audit_http_sink import HTTPAuditSink


def integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise SystemExit(f"{name} must be between {minimum} and {maximum}")
    return value


def boolean(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, str(default)).strip().lower()
    if raw not in {"true", "false"}:
        raise SystemExit(f"{name} must be true or false")
    return raw == "true"


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def authorization() -> str | None:
    path = os.environ.get("AUDIT_DELIVERY_AUTHORIZATION_FILE", "").strip()
    if not path:
        return None
    source = Path(path)
    if not source.is_file():
        raise SystemExit("AUDIT_DELIVERY_AUTHORIZATION_FILE is not a readable file")
    value = source.read_text(encoding="utf-8").strip()
    if not value or "\n" in value or "\r" in value:
        raise SystemExit("audit authorization file is empty or malformed")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    repository = AuditDeliveryRepository(required("CONTROL_PLANE_DATABASE_URL"))
    repository.check_ready()
    destination_id = required("AUDIT_DELIVERY_DESTINATION_ID")
    coordinator = AuditDeliveryCoordinator(
        repository,
        HTTPAuditSink(
            required("AUDIT_DELIVERY_SINK_URL"),
            authorization=authorization(),
            timeout_seconds=integer("AUDIT_DELIVERY_TIMEOUT_SECONDS", 5, 1, 60),
            allow_insecure_http=boolean("AUDIT_DELIVERY_ALLOW_INSECURE_HTTP"),
        ),
        base_retry_seconds=integer("AUDIT_DELIVERY_BASE_RETRY_SECONDS", 5, 1, 3600),
        max_retry_seconds=integer("AUDIT_DELIVERY_MAX_RETRY_SECONDS", 300, 1, 86400),
    )
    batch_size = integer("AUDIT_DELIVERY_BATCH_SIZE", 500, 1, 5000)
    interval = integer("AUDIT_DELIVERY_POLL_SECONDS", 5, 1, 300)
    repository.ensure_destination(destination_id)
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        before = repository.get_checkpoint(destination_id)
        after = coordinator.deliver_once(destination_id, limit=batch_size)
        if args.once:
            return 2 if after.failure_attempts > before.failure_attempts else 0
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
