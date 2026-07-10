"""Live PostgreSQL contract checks for the authoritative control-plane adapter."""

from __future__ import annotations

import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, insert, text
from sqlalchemy.engine import make_url

from control_plane import AuditEvent, PostgresControlPlaneStore, StateConflictError
from indexing_events import (
    IndexingEventDraft,
    LatestRepairDraft,
    PostgresIndexingEventStore,
)
from control_plane.schema import indexing_event_outbox
from control_plane.migrate import run_downgrade, run_upgrade


POSTGRES_URL = os.environ.get("TEST_CONTROL_PLANE_POSTGRES_URL", "").strip()
QUERY_API_ROOT = Path(__file__).resolve().parent.parent


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="TEST_CONTROL_PLANE_POSTGRES_URL requires a disposable PostgreSQL database",
    ),
]


def _audit() -> AuditEvent:
    return AuditEvent(
        actor="postgres-integration",
        action="state_changed",
        resource_type="control_plane_state",
        resource_id="current",
    )


def test_migration_runner_commits_upgrade_and_downgrade_after_connection_close(
    monkeypatch,
):
    schema_name = "imposbro_runner_" + uuid.uuid4().hex
    admin_engine = create_engine(POSTGRES_URL)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    runner_url = make_url(POSTGRES_URL).update_query_dict(
        {"options": f"-csearch_path={schema_name}"}
    )
    monkeypatch.setenv("CONTROL_PLANE_DATABASE_URL", runner_url.render_as_string(False))
    monkeypatch.setenv("CONTROL_PLANE_ALLOW_DESTRUCTIVE_DOWNGRADE", "true")
    verification_engine = create_engine(runner_url)
    try:
        run_upgrade("head")
        with verification_engine.connect() as connection:
            table_names = set(inspect(connection).get_table_names())
            assert "alembic_version" in table_names
            assert "control_plane_state" in table_names
            assert "indexing_event_outbox" in table_names
            assert connection.scalar(text("SELECT count(*) FROM alembic_version")) == 1

        run_downgrade("base")
        with verification_engine.connect() as connection:
            # Alembic retains its empty version table on PostgreSQL. Every
            # application table must be gone, while the migration marker is
            # allowed only when it contains no revision row.
            assert set(inspect(connection).get_table_names()) <= {"alembic_version"}
            if "alembic_version" in inspect(connection).get_table_names():
                assert connection.scalar(
                    text("SELECT count(*) FROM alembic_version")
                ) == 0
    finally:
        verification_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        admin_engine.dispose()


def test_postgres_migration_and_concurrent_cas_contract():
    schema_name = "imposbro_cp_" + uuid.uuid4().hex
    assert re.fullmatch(r"[a-z0-9_]+", schema_name)
    admin_engine = create_engine(POSTGRES_URL)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    engine = create_engine(
        POSTGRES_URL,
        connect_args={"options": f"-csearch_path={schema_name}"},
    )
    try:
        config = Config(str(QUERY_API_ROOT / "alembic.ini"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

        store = PostgresControlPlaneStore(engine)
        store.check_ready()

        def write(value: int):
            try:
                return store.commit(
                    expected_revision=0,
                    state={"value": value},
                    audit=_audit(),
                    event_type="state.changed",
                )
            except StateConflictError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(write, [1, 2]))

        assert len([result for result in results if isinstance(result, StateConflictError)]) == 1
        committed = [
            result for result in results if not isinstance(result, StateConflictError)
        ]
        assert len(committed) == 1
        assert store.latest_revision() == 1
        assert len(store.list_audit()) == 1
        assert len(store.list_outbox()) == 1
        store.verify_audit_chain()

        event_store = PostgresIndexingEventStore(engine)
        event_store.check_ready()

        def prepare_event(value: int):
            return event_store.prepare(
                IndexingEventDraft(
                    tenant_id="tenant-a",
                    collection="products",
                    document_id="doc-1",
                    operation="upsert",
                    target_clusters=("cluster-a", "cluster-b"),
                    routing_revision=1,
                    idempotency_key=f"concurrent-event-{value}",
                    document={"id": "doc-1", "value": value},
                )
            )

        with ThreadPoolExecutor(max_workers=16) as executor:
            events = list(executor.map(prepare_event, range(32)))

        assert sorted(event.sequence for event in events) == list(range(1, 33))
        assert len({event.event_id for event in events}) == 32
        assert event_store.pending_count() == 32
        retry = event_store.prepare(
            IndexingEventDraft(
                tenant_id="tenant-a",
                collection="products",
                document_id="doc-1",
                operation="upsert",
                target_clusters=("cluster-a", "cluster-b"),
                routing_revision=1,
                idempotency_key="concurrent-event-0",
                document={"id": "doc-1", "value": 0},
            )
        )
        assert retry.event_id == events[0].event_id
        assert retry.sequence == events[0].sequence

        high_water_before = event_store.high_water_mark()
        assert high_water_before == 32
        barrier = threading.Barrier(2)

        def prepare_live_after_snapshot():
            barrier.wait()
            return event_store.prepare(
                IndexingEventDraft(
                    tenant_id="tenant-a",
                    collection="products",
                    document_id="doc-1",
                    operation="upsert",
                    target_clusters=("cluster-a", "cluster-b"),
                    routing_revision=2,
                    idempotency_key="live-after-snapshot",
                    document={"id": "doc-1", "value": 999},
                )
            )

        def prepare_repair():
            barrier.wait()
            return event_store.prepare_latest_repair(
                LatestRepairDraft(
                    identity_hash=events[0].identity_hash,
                    idempotency_key="repair-after-snapshot",
                    target_clusters=("cluster-c",),
                    routing_revision=3,
                    rollout_id="repair-rollout",
                )
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            live_future = executor.submit(prepare_live_after_snapshot)
            repair_future = executor.submit(prepare_repair)
            live = live_future.result()
            repair = repair_future.result()

        assert sorted((live.sequence, repair.sequence)) == [33, 34]
        if repair.sequence > live.sequence:
            assert repair.payload["document"] == live.payload["document"]
        assert event_store.high_water_mark() == high_water_before + 2
        assert event_store.list_latest_by_identity(
            after_position=high_water_before,
            through_position=event_store.high_water_mark(),
        )[-1].sequence == 34

        # Identity/sequence values are allocated before commit. The HWM must
        # wait for an older in-flight insert so a cursor can never advance past
        # an event that becomes visible later at a lower global position.
        writer = engine.connect()
        transaction = writer.begin()
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            event_id = "evt:" + uuid.uuid4().hex
            writer.execute(
                insert(indexing_event_outbox).values(
                    event_id=event_id,
                    idempotency_key_hash="a" * 64,
                    request_digest="b" * 64,
                    identity_hash="c" * 64,
                    sequence=1,
                    payload_json={
                        "identity": {
                            "tenant_id": "tenant-barrier",
                            "collection": "products",
                            "document_id": "doc-barrier",
                        }
                    },
                    created_at=datetime.now(timezone.utc),
                    published_at=None,
                    last_error=None,
                    publish_attempts=0,
                )
            )
            barrier_started = threading.Event()

            def read_barrier():
                barrier_started.set()
                return event_store.high_water_mark()

            barrier_future = executor.submit(read_barrier)
            assert barrier_started.wait(timeout=1)
            with pytest.raises(FutureTimeoutError):
                barrier_future.result(timeout=0.2)
            transaction.commit()
            assert barrier_future.result(timeout=5) == high_water_before + 3
        finally:
            if transaction.is_active:
                transaction.rollback()
            writer.close()
            executor.shutdown(wait=True)
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        admin_engine.dispose()
