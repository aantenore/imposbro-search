"""Contract tests for transactional control-plane store implementations."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import typesense
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text, update

from control_plane import (
    AuditEvent,
    AuditIntegrityError,
    InMemoryControlPlaneStore,
    LegacyStateError,
    PostgresControlPlaneStore,
    StateConflictError,
    StateIntegrityError,
    StoreNotReadyError,
    TypesenseLegacyStateReader,
    UnsupportedStateSchemaError,
)
from control_plane.schema import control_plane_audit, control_plane_state


QUERY_API_ROOT = Path(__file__).resolve().parent.parent


def _audit(action: str = "state_changed") -> AuditEvent:
    return AuditEvent(
        actor="operator:test",
        action=action,
        resource_type="control_plane_state",
        resource_id="current",
        request_id="request-1",
        details={"safe": True},
    )


def _upgrade(engine) -> None:
    config = Config(str(QUERY_API_ROOT / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


@pytest.fixture
def sqlite_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _upgrade(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def sqlite_store(sqlite_engine):
    store = PostgresControlPlaneStore(sqlite_engine, require_postgres=False)
    store.check_ready()
    return store


def test_in_memory_store_allows_only_one_of_100_stale_writers():
    store = InMemoryControlPlaneStore()
    barrier = threading.Barrier(100)

    def write(index: int):
        barrier.wait()
        try:
            record = store.commit(
                expected_revision=0,
                state={"winner": index},
                audit=_audit(),
                event_type="state.changed",
            )
            return "committed", record.revision
        except StateConflictError as exc:
            return "conflict", exc.actual_revision

    with ThreadPoolExecutor(max_workers=100) as executor:
        results = list(executor.map(write, range(100)))

    assert [result for result in results if result[0] == "committed"] == [
        ("committed", 1)
    ]
    assert len([result for result in results if result[0] == "conflict"]) == 99
    assert store.latest_revision() == 1
    assert len(store.list_audit()) == 1
    assert len(store.list_outbox()) == 1
    store.verify_audit_chain()


def test_in_memory_store_rejects_non_json_without_partial_commit():
    store = InMemoryControlPlaneStore()

    with pytest.raises(ValueError, match="finite JSON"):
        store.commit(
            expected_revision=0,
            state={"not_json": {"set"}},
            audit=_audit(),
            event_type="state.changed",
        )

    assert store.load() is None
    assert store.list_audit() == []
    assert store.list_outbox() == []


def test_in_memory_store_rejects_unsupported_state_schema():
    store = InMemoryControlPlaneStore()

    with pytest.raises(UnsupportedStateSchemaError) as error:
        store.commit(
            expected_revision=0,
            state={},
            audit=_audit(),
            event_type="state.changed",
            schema_version=2,
        )

    assert error.value.expected_version == 1
    assert error.value.actual_version == 2


def test_sqlite_contract_commits_state_audit_and_outbox_atomically(sqlite_store):
    record = sqlite_store.commit(
        expected_revision=0,
        state={"routing": {"products": "cluster-a"}},
        audit=_audit(),
        event_type="routing.changed",
        event_payload={"collection": "products"},
    )

    assert record.revision == 1
    assert sqlite_store.load() == record
    assert sqlite_store.latest_revision() == 1
    audit_rows = sqlite_store.list_audit()
    assert audit_rows[0]["revision"] == 1
    assert audit_rows[0]["previous_hash"] == "0" * 64
    outbox = sqlite_store.list_outbox()
    assert len(outbox) == 1
    assert outbox[0].revision == 1
    assert outbox[0].payload == {
        "collection": "products",
        "revision": 1,
        "schema_version": 1,
        "state_digest": record.state_digest,
    }
    sqlite_store.verify_audit_chain()


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_audit_export_is_ascending_cursor_paginated_and_chain_complete(
    store_kind,
    sqlite_store,
):
    store = InMemoryControlPlaneStore() if store_kind == "memory" else sqlite_store
    for revision in range(3):
        store.commit(
            expected_revision=revision,
            state={"value": revision + 1},
            audit=_audit(action=f"change_{revision + 1}"),
            event_type="state.changed",
        )

    first = store.export_audit(limit=2)
    second = store.export_audit(after_sequence=first[-1]["sequence"], limit=2)

    assert [item["sequence"] for item in first] == [1, 2]
    assert [item["sequence"] for item in second] == [3]
    assert first[0]["previous_hash"] == "0" * 64
    assert first[1]["previous_hash"] == first[0]["event_hash"]
    assert second[0]["previous_hash"] == first[1]["event_hash"]
    store.verify_audit_chain()


def test_sqlite_contract_exposes_typed_stale_conflict(sqlite_store):
    sqlite_store.commit(
        expected_revision=0,
        state={"value": 1},
        audit=_audit(),
        event_type="state.changed",
    )

    with pytest.raises(StateConflictError) as error:
        sqlite_store.commit(
            expected_revision=0,
            state={"value": 2},
            audit=_audit(),
            event_type="state.changed",
        )

    assert error.value.expected_revision == 0
    assert error.value.actual_revision == 1
    assert sqlite_store.load().state == {"value": 1}
    assert len(sqlite_store.list_audit()) == 1
    assert len(sqlite_store.list_outbox()) == 1


def test_sqlite_outbox_delivery_updates_are_idempotent(sqlite_store):
    sqlite_store.commit(
        expected_revision=0,
        state={"value": 1},
        audit=_audit(),
        event_type="state.changed",
    )
    event = sqlite_store.list_outbox()[0]

    assert sqlite_store.record_outbox_failure(event.event_id, "redis unavailable")
    failed = sqlite_store.list_outbox()[0]
    assert failed.publish_attempts == 1
    assert failed.last_error == "redis unavailable"
    assert failed.published_at is None

    assert sqlite_store.mark_outbox_published(event.event_id)
    published = sqlite_store.list_outbox()[0]
    assert published.publish_attempts == 2
    assert published.last_error is None
    assert published.published_at is not None
    assert not sqlite_store.mark_outbox_published(event.event_id)
    assert not sqlite_store.record_outbox_failure(event.event_id, "late error")
    assert sqlite_store.list_unpublished_outbox() == []


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_unpublished_outbox_does_not_starve_later_records(store_kind, sqlite_store):
    store = InMemoryControlPlaneStore() if store_kind == "memory" else sqlite_store
    for revision in range(2):
        store.commit(
            expected_revision=revision,
            state={"value": revision + 1},
            audit=_audit(),
            event_type="state.changed",
        )
    records = store.list_unpublished_outbox()
    assert [record.revision for record in records] == [1, 2]

    assert store.mark_outbox_published(records[0].event_id)
    assert [record.revision for record in store.list_unpublished_outbox()] == [2]


def test_sqlite_transaction_rolls_back_state_and_audit_when_outbox_fails(
    sqlite_store,
    monkeypatch,
):
    def fail_outbox(*_args, **_kwargs):
        raise RuntimeError("injected outbox failure")

    monkeypatch.setattr(sqlite_store, "_insert_outbox", fail_outbox)

    with pytest.raises(RuntimeError, match="injected outbox failure"):
        sqlite_store.commit(
            expected_revision=0,
            state={"value": 1},
            audit=_audit(),
            event_type="state.changed",
        )

    assert sqlite_store.load() is None
    assert sqlite_store.list_audit() == []
    assert sqlite_store.list_outbox() == []
    sqlite_store.verify_audit_chain()


def test_sqlite_store_detects_state_and_audit_tampering(sqlite_store, sqlite_engine):
    sqlite_store.commit(
        expected_revision=0,
        state={"value": 1},
        audit=_audit(),
        event_type="state.changed",
    )

    with sqlite_engine.begin() as connection:
        connection.execute(
            update(control_plane_state)
            .where(control_plane_state.c.id == "current")
            .values(state_json={"value": 999})
        )
    with pytest.raises(StateIntegrityError, match="digest mismatch"):
        sqlite_store.load()

    with sqlite_engine.begin() as connection:
        connection.execute(
            update(control_plane_audit)
            .where(control_plane_audit.c.sequence == 1)
            .values(actor="tampered")
        )
    with pytest.raises(AuditIntegrityError, match="event hash mismatch"):
        sqlite_store.verify_audit_chain()


def test_store_check_ready_fails_before_migration():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    store = PostgresControlPlaneStore(engine, require_postgres=False)

    with pytest.raises(StoreNotReadyError, match="missing tables"):
        store.check_ready()

    engine.dispose()


def test_store_check_ready_fails_on_unexpected_migration_revision(sqlite_engine):
    with sqlite_engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = 'unexpected_revision'")
        )
    store = PostgresControlPlaneStore(sqlite_engine, require_postgres=False)

    with pytest.raises(StoreNotReadyError, match="revision mismatch"):
        store.check_ready()


def test_alembic_upgrade_and_downgrade_are_reversible():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    config = Config(str(QUERY_API_ROOT / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    assert "control_plane_state" in inspect(engine).get_table_names()

    config = Config(str(QUERY_API_ROOT / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "base")
    assert "control_plane_state" not in inspect(engine).get_table_names()
    engine.dispose()


class _LegacyDocuments:
    def __init__(self, document=None, error=None):
        self.document = document
        self.error = error
        self.requested_id = None

    def __getitem__(self, document_id):
        self.requested_id = document_id
        return self

    def retrieve(self):
        if self.error:
            raise self.error
        return self.document


class _LegacyCollections:
    def __init__(self, documents):
        self.documents = documents
        self.requested_collection = None

    def __getitem__(self, collection_name):
        self.requested_collection = collection_name
        return type("LegacyCollection", (), {"documents": self.documents})()


class _LegacyClient:
    def __init__(self, document=None, error=None):
        self.documents = _LegacyDocuments(document=document, error=error)
        self.collections = _LegacyCollections(self.documents)


def test_legacy_reader_normalizes_v1_state_without_writing():
    client = _LegacyClient(
        {
            "state_data": json.dumps(
                {
                    "federation_clusters_config": {"cluster-a": {"host": "a"}},
                    "collection_routing_rules": {},
                }
            )
        }
    )
    reader = TypesenseLegacyStateReader(client)

    snapshot = reader.load()

    assert snapshot.state == {
        "federation_clusters_config": {"cluster-a": {"host": "a"}},
        "collection_routing_rules": {},
        "collection_schemas": {},
        "collection_aliases": {},
    }
    assert len(snapshot.state_digest) == 64
    assert client.collections.requested_collection == "_imposbro_state"
    assert client.documents.requested_id == "config_v1"
    assert not hasattr(reader, "save")
    assert not hasattr(reader, "commit")


def test_legacy_reader_returns_none_when_config_document_is_absent():
    client = _LegacyClient(error=typesense.exceptions.ObjectNotFound("missing"))

    assert TypesenseLegacyStateReader(client).load() is None


def test_legacy_reader_rejects_unknown_fields_instead_of_dropping_them():
    client = _LegacyClient(
        {"state_data": json.dumps({"future_unrecognized_state": {"x": 1}})}
    )

    with pytest.raises(LegacyStateError, match="unsupported keys"):
        TypesenseLegacyStateReader(client).load()
