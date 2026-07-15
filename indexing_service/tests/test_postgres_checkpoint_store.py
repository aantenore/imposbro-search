"""Schema and optional live PostgreSQL checks for fenced checkpoints."""

from __future__ import annotations

import os
import re
import sys
import threading
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


APP_DIR = Path(__file__).resolve().parent.parent / "app"
REPO_ROOT = Path(__file__).resolve().parents[2]
QUERY_API_ROOT = REPO_ROOT / "query_api"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import consumer  # noqa: E402
from checkpoint_store import (  # noqa: E402
    _PostgresEventSession,
    CheckpointStoreError,
    PostgresCheckpointStore,
    advisory_lock_key,
    build_checkpoint_store_from_env,
    checkpoint_from_event,
    checkpoint_id,
    normalize_postgres_url,
)
from event_envelope import parse_indexing_event  # noqa: E402


def _upgrade(engine) -> None:
    config = Config(str(QUERY_API_ROOT / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def _event(*, event_id: str, sequence: int):
    return {
        "envelope_version": 2,
        "event_id": event_id,
        "identity": {
            "tenant_id": "tenant-a",
            "collection": "products",
            "document_id": "doc-1",
        },
        "document_version": sequence,
        "sequence": sequence,
        "operation": "upsert",
        "routing_revision": 1,
        "rollout_id": None,
        "target_clusters": ["cluster-a"],
        "occurred_at": "2026-07-10T08:00:00Z",
        "trace": {},
        "document": {"id": "doc-1", "name": f"version-{sequence}"},
    }


def test_migration_contains_checkpoint_fence_schema_and_global_outbox_position():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _upgrade(engine)
    schema = inspect(engine)

    checkpoint_columns = {
        column["name"] for column in schema.get_columns("indexing_checkpoints")
    }
    outbox_columns = {
        column["name"] for column in schema.get_columns("indexing_event_outbox")
    }

    assert {
        "checkpoint_key",
        "identity_key",
        "target_cluster",
        "sequence",
        "document_version",
        "event_digest",
        "tombstone",
    } <= checkpoint_columns
    assert "global_position" in outbox_columns
    assert any(
        index["name"] == "uq_indexing_event_global_position" and index["unique"]
        for index in schema.get_indexes("indexing_event_outbox")
    )
    engine.dispose()


def test_postgres_adapter_rejects_non_postgres_even_with_migrated_schema():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _upgrade(engine)
    store = PostgresCheckpointStore(engine)

    with pytest.raises(CheckpointStoreError, match="PostgreSQL with psycopg"):
        store.ensure_ready()

    engine.dispose()


@pytest.mark.parametrize("backend", ["memory", "typesense"])
def test_enterprise_profile_rejects_unfenced_development_backends(
    monkeypatch,
    backend,
):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "enterprise")
    monkeypatch.setenv("INDEXING_CHECKPOINT_BACKEND", backend)
    monkeypatch.setenv("INDEXING_ALLOW_VOLATILE_CHECKPOINTS", "true")
    monkeypatch.setenv("INDEXING_ALLOW_TYPESENSE_CHECKPOINTS", "true")

    with pytest.raises(CheckpointStoreError, match="requires the postgres"):
        build_checkpoint_store_from_env({})


def test_advisory_lock_key_is_stable_signed_bigint():
    checkpoint_key = "f" * 64

    assert advisory_lock_key(checkpoint_key) == -1
    assert advisory_lock_key("0" * 64) == 0


@pytest.mark.parametrize("returned_key", ["expected", None])
def test_postgres_save_uses_returning_instead_of_unreliable_rowcount(returned_key):
    event = parse_indexing_event(
        _event(event_id="evt-returning-0001", sequence=1),
        allow_legacy=False,
    )
    expected_key = checkpoint_id(event, "cluster-a")

    class Result:
        rowcount = -1

        def scalar_one_or_none(self):
            return expected_key if returned_key == "expected" else None

    class Connection:
        def execute(self, statement):
            assert "RETURNING indexing_checkpoints.checkpoint_key" in str(statement)
            return Result()

    session = _PostgresEventSession(
        Connection(),
        event,
        {"cluster-a": expected_key},
    )
    checkpoint = checkpoint_from_event(event, "cluster-a")
    if returned_key == "expected":
        session.save(checkpoint)
    else:
        with pytest.raises(CheckpointStoreError, match="compare-and-set rejected"):
            session.save(checkpoint)


POSTGRES_URL = os.environ.get("TEST_CONTROL_PLANE_POSTGRES_URL", "").strip()


@pytest.mark.integration
@pytest.mark.skipif(
    not POSTGRES_URL,
    reason="TEST_CONTROL_PLANE_POSTGRES_URL requires disposable PostgreSQL",
)
def test_live_postgres_fence_prevents_older_thread_overwriting_newer_event():
    database_url = normalize_postgres_url(POSTGRES_URL)
    schema_name = "imposbro_checkpoint_" + uuid.uuid4().hex
    assert re.fullmatch(r"[a-z0-9_]+", schema_name)
    admin_engine = create_engine(database_url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
    engine = create_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema_name}"},
    )
    newer_applied = threading.Event()
    release_newer = threading.Event()
    current_document = {}
    writes = []
    write_lock = threading.Lock()

    class Documents:
        def upsert(self, document):
            with write_lock:
                current_document.clear()
                current_document.update(document)
                writes.append(dict(document))
            if document["name"] == "version-2":
                newer_applied.set()
                if not release_newer.wait(timeout=3):
                    raise RuntimeError("test release timed out")

    documents = Documents()

    class Collections:
        def __getitem__(self, _name):
            return type("Collection", (), {"documents": documents})()

    client = type("Client", (), {"collections": Collections()})()
    store = PostgresCheckpointStore(engine, lock_timeout_ms=2000)
    errors = []

    try:
        _upgrade(engine)
        store.ensure_ready()

        def process(payload):
            try:
                consumer.process_message(
                    payload,
                    {"cluster-a": client},
                    checkpoint_store=store,
                    message_key=b"tenant-a\x1fproducts\x1fdoc-1",
                    allow_legacy=False,
                )
            except Exception as exc:  # pragma: no cover - integration assertion
                errors.append(exc)

        newer = threading.Thread(
            target=process,
            args=(_event(event_id="evt-00000002", sequence=2),),
        )
        older = threading.Thread(
            target=process,
            args=(_event(event_id="evt-00000001", sequence=1),),
        )
        newer.start()
        assert newer_applied.wait(timeout=2)
        older.start()
        release_newer.set()
        newer.join(timeout=4)
        older.join(timeout=4)

        assert not errors
        assert current_document == {"id": "doc-1", "name": "version-2"}
        assert writes == [{"id": "doc-1", "name": "version-2"}]
        event = parse_indexing_event(
            _event(event_id="evt-00000002", sequence=2),
            allow_legacy=False,
        )
        with store.event_scope(event) as session:
            checkpoint = session.load(event, "cluster-a")
        assert checkpoint is not None
        assert checkpoint.sequence == 2
    finally:
        release_newer.set()
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        admin_engine.dispose()
