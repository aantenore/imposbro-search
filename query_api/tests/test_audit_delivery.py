from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, update

from control_plane import (
    AuditDeliveryAck,
    AuditDeliveryCoordinator,
    AuditDeliveryIntegrityError,
    AuditDeliveryRepository,
    AuditEvent,
    AuditSinkError,
    PostgresControlPlaneStore,
    HTTPAuditSink,
)
from control_plane.schema import control_plane_audit
from control_plane.audit_delivery_worker import authorization, integer, required


QUERY_API_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def audit_state():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    config = Config(str(QUERY_API_ROOT / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    store = PostgresControlPlaneStore(engine, require_postgres=False)
    for revision in range(3):
        store.commit(
            expected_revision=revision,
            state={"revision": revision + 1},
            audit=AuditEvent(
                actor="operator:test",
                action="state_changed",
                resource_type="control_plane_state",
                resource_id="current",
                request_id=f"request-{revision + 1}",
            ),
            event_type="state.changed",
        )
    yield engine, store, AuditDeliveryRepository(engine)
    engine.dispose()


class AcceptingSink:
    def __init__(self):
        self.sequences = []

    def deliver(self, batch):
        self.sequences.append([record["sequence"] for record in batch.records])
        return AuditDeliveryAck(
            destination_id=batch.destination_id,
            last_sequence=batch.last_sequence,
            last_event_hash=batch.last_event_hash,
        )


class FailingSink:
    def deliver(self, _batch):
        raise AuditSinkError("sink_timeout")


def test_delivery_is_paged_idempotent_and_advances_exact_chain_boundary(audit_state):
    _engine, _store, repository = audit_state
    sink = AcceptingSink()
    coordinator = AuditDeliveryCoordinator(repository, sink)
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)

    first = coordinator.deliver_once("security-archive", limit=2, now=now)
    second = coordinator.deliver_once("security-archive", limit=2, now=now)
    empty = coordinator.deliver_once("security-archive", limit=2, now=now)

    assert sink.sequences == [[1, 2], [3]]
    assert first.last_sequence == 2
    assert second.last_sequence == empty.last_sequence == 3
    assert repository.stats().max_backlog == 0
    assert repository.advance(
        AuditDeliveryAck("security-archive", 3, second.last_event_hash), now=now
    )


def test_failure_persists_only_safe_code_and_defers_retry(audit_state):
    engine, _store, repository = audit_state
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    coordinator = AuditDeliveryCoordinator(
        repository, FailingSink(), base_retry_seconds=7, max_retry_seconds=30
    )

    checkpoint = coordinator.deliver_once("siem-primary", now=now)
    deferred = repository.next_batch("siem-primary", now=now + timedelta(seconds=1))

    assert checkpoint.failure_attempts == 1
    assert checkpoint.last_error_code == "sink_timeout"
    assert checkpoint.next_retry_at == now + timedelta(seconds=7)
    assert deferred.records == ()
    assert repository.stats().failed_destinations == 1
    with engine.connect() as connection:
        serialized = str(connection.exec_driver_sql(
            "SELECT * FROM audit_delivery_checkpoints"
        ).all())
    assert "password" not in serialized.lower()


def test_tampered_page_and_ack_mismatch_fail_closed(audit_state):
    engine, _store, repository = audit_state
    repository.ensure_destination("archive")
    with engine.begin() as connection:
        connection.execute(
            update(control_plane_audit)
            .where(control_plane_audit.c.sequence == 2)
            .values(details_json={"tampered": True})
        )
    with pytest.raises(AuditDeliveryIntegrityError, match="event hash"):
        repository.next_batch("archive")

    with pytest.raises(AuditDeliveryIntegrityError, match="malformed"):
        repository.advance(AuditDeliveryAck("archive", 1, "bad"))


@pytest.mark.parametrize("destination", ["../sink", "UPPER", "", "x" * 65])
def test_destination_identifier_is_bounded(destination, audit_state):
    _engine, _store, repository = audit_state
    with pytest.raises(ValueError):
        repository.ensure_destination(destination)


def test_generic_https_sink_posts_exact_boundary_and_parses_ack(audit_state):
    _engine, _store, repository = audit_state
    repository.ensure_destination("archive")
    batch = repository.next_batch("archive", limit=1)
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return (
                '{"destination_id":"archive","last_sequence":1,'
                f'"last_event_hash":"{batch.last_event_hash}"}}'
            ).encode()

    def opener(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    ack = HTTPAuditSink(
        "https://audit.example.test/v1/events",
        authorization="Bearer file-backed-value",
        opener=opener,
    ).deliver(batch)

    assert ack.last_sequence == 1
    assert captured["timeout"] == 5.0
    assert captured["request"].get_header("Authorization") == "Bearer file-backed-value"
    assert b'"events"' in captured["request"].data


def test_packaged_worker_configuration_is_bounded_and_file_backed(
    monkeypatch, tmp_path
):
    secret_file = tmp_path / "authorization"
    secret_file.write_text("Bearer mounted-secret\n", encoding="utf-8")
    monkeypatch.setenv("AUDIT_DELIVERY_AUTHORIZATION_FILE", str(secret_file))
    monkeypatch.setenv("AUDIT_DELIVERY_BATCH_SIZE", "250")
    monkeypatch.setenv("AUDIT_DELIVERY_DESTINATION_ID", "security-archive")

    assert authorization() == "Bearer mounted-secret"
    assert integer("AUDIT_DELIVERY_BATCH_SIZE", 500, 1, 5000) == 250
    assert required("AUDIT_DELIVERY_DESTINATION_ID") == "security-archive"
    monkeypatch.setenv("AUDIT_DELIVERY_BATCH_SIZE", "5001")
    with pytest.raises(SystemExit, match="between"):
        integer("AUDIT_DELIVERY_BATCH_SIZE", 500, 1, 5000)
