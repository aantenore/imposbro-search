"""Contract tests for durable indexing sequence allocation and outbox replay."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from indexing_events import (
    IdempotencyConflictError,
    InMemoryIndexingEventStore,
    IndexingEventDraft,
    LatestRepairDraft,
    PostgresIndexingEventStore,
    RepairSourceNotFoundError,
)


QUERY_API_ROOT = Path(__file__).resolve().parent.parent


def draft(
    key: str,
    *,
    name: str = "Product",
    routing_revision: int = 1,
    targets=("cluster-a",),
    document_id: str = "doc-1",
) -> IndexingEventDraft:
    return IndexingEventDraft(
        tenant_id="tenant-a",
        collection="products",
        document_id=document_id,
        operation="upsert",
        target_clusters=tuple(targets),
        routing_revision=routing_revision,
        idempotency_key=key,
        trace={"request_id": "request-1"},
        document={"id": document_id, "name": name},
    )


def test_in_memory_store_allocates_unique_monotonic_sequences_concurrently():
    store = InMemoryIndexingEventStore()

    with ThreadPoolExecutor(max_workers=20) as executor:
        events = list(
            executor.map(
                lambda index: store.prepare(draft(f"key-{index:04}")),
                range(100),
            )
        )

    assert sorted(event.sequence for event in events) == list(range(1, 101))
    assert store.pending_count() == 100
    assert store.high_water_mark() == 100
    assert sorted(event.global_position for event in events) == list(range(1, 101))


def test_idempotent_retry_recovers_original_route_and_trace_but_reuse_conflicts():
    store = InMemoryIndexingEventStore()
    first = store.prepare(draft("retry-key", routing_revision=3))
    retry = store.prepare(
        IndexingEventDraft(
            **{
                **draft("retry-key", routing_revision=4, targets=("cluster-b",)).__dict__,
                "trace": {"request_id": "request-2"},
            }
        )
    )

    assert retry == first
    assert retry.payload["routing_revision"] == 3
    assert retry.payload["target_clusters"] == ["cluster-a"]
    with pytest.raises(IdempotencyConflictError):
        store.prepare(draft("retry-key", name="Different product"))


def test_sql_store_persists_pending_event_and_delivery_state():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    config = Config(str(QUERY_API_ROOT / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    store = PostgresIndexingEventStore(engine, require_postgres=False)
    store.check_ready()

    first = store.prepare(draft("retry-key"))
    second = store.prepare(draft("second-key"))

    assert first.sequence == 1
    assert second.sequence == 2
    assert first.global_position == 1
    assert second.global_position == 2
    assert store.high_water_mark() == 2
    assert [item.event_id for item in store.list_pending()] == [
        first.event_id,
        second.event_id,
    ]
    assert store.record_failure(first.event_id, "broker unavailable") is True
    assert store.list_pending()[0].publish_attempts == 1
    assert store.mark_published(first.event_id) is True
    assert store.mark_published(first.event_id) is False
    assert store.pending_count() == 1
    assert store.prepare(draft("retry-key")).event_id == first.event_id
    engine.dispose()


def test_high_water_window_returns_latest_event_per_identity():
    store = InMemoryIndexingEventStore()
    first = store.prepare(draft("key-0001", name="one"))
    second = store.prepare(draft("key-0002", name="two"))
    other = store.prepare(
        draft("key-0003", name="other", document_id="doc-2")
    )

    assert store.high_water_mark() == other.global_position == 3
    latest = store.list_latest_by_identity(
        after_position=0,
        through_position=store.high_water_mark(),
    )
    assert [event.global_position for event in latest] == [
        second.global_position,
        other.global_position,
    ]
    assert first.global_position not in {
        event.global_position for event in latest
    }
    assert store.list_latest_by_identity(
        after_position=1,
        through_position=2,
    ) == [second]


def test_prepare_latest_repair_clones_latest_mutation_with_new_route():
    store = InMemoryIndexingEventStore()
    source_draft = draft("source-key", name="authoritative")
    source = store.prepare(source_draft)
    repair_request = LatestRepairDraft(
        identity_hash=source_draft.identity_hash,
        idempotency_key="repair-key",
        target_clusters=("cluster-b", "cluster-c"),
        routing_revision=7,
        rollout_id="rollout-7",
        trace={"request_id": "repair-request"},
    )

    repair = store.prepare_latest_repair(repair_request)

    assert repair.sequence == source.sequence + 1
    assert repair.global_position == source.global_position + 1
    assert repair.payload["document"] == source.payload["document"]
    assert repair.payload["target_clusters"] == ["cluster-b", "cluster-c"]
    assert repair.payload["routing_revision"] == 7
    assert repair.payload["rollout_id"] == "rollout-7"
    store.prepare(draft("live-after-repair", name="newer-live"))
    assert store.prepare_latest_repair(repair_request) == repair


def test_prepare_latest_repair_requires_existing_identity():
    store = InMemoryIndexingEventStore()

    with pytest.raises(RepairSourceNotFoundError):
        store.prepare_latest_repair(
            LatestRepairDraft(
                identity_hash="0" * 64,
                idempotency_key="repair-key",
                target_clusters=("cluster-b",),
                routing_revision=2,
            )
        )


def test_live_prepare_and_latest_repair_serialize_without_stale_snapshot_override():
    store = InMemoryIndexingEventStore()
    initial_draft = draft("initial-key", name="initial")
    store.prepare(initial_draft)
    live_draft = draft("live-key", name="live")
    repair_request = LatestRepairDraft(
        identity_hash=initial_draft.identity_hash,
        idempotency_key="repair-key",
        target_clusters=("cluster-b",),
        routing_revision=2,
    )
    barrier = threading.Barrier(2)

    def prepare_live():
        barrier.wait()
        return store.prepare(live_draft)

    def prepare_repair():
        barrier.wait()
        return store.prepare_latest_repair(repair_request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        live_future = executor.submit(prepare_live)
        repair_future = executor.submit(prepare_repair)
        live = live_future.result()
        repair = repair_future.result()

    assert sorted((live.sequence, repair.sequence)) == [2, 3]
    if repair.sequence > live.sequence:
        assert repair.payload["document"] == live.payload["document"]
    else:
        assert live.sequence > repair.sequence


def test_sql_store_supports_repair_and_global_position_window():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    config = Config(str(QUERY_API_ROOT / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    store = PostgresIndexingEventStore(engine, require_postgres=False)
    source_draft = draft("source-key", name="authoritative")
    source = store.prepare(source_draft)

    repair_request = LatestRepairDraft(
        identity_hash=source_draft.identity_hash,
        idempotency_key="repair-key",
        target_clusters=("cluster-b",),
        routing_revision=2,
    )
    repair = store.prepare_latest_repair(repair_request)

    assert repair.sequence == 2
    assert repair.global_position == 2
    assert repair.payload["document"] == source.payload["document"]
    assert store.high_water_mark() == 2
    assert store.list_latest_by_identity(
        after_position=0,
        through_position=2,
    ) == [repair]
    store.prepare(draft("live-after-repair", name="newer-live"))
    assert store.prepare_latest_repair(repair_request) == repair
    engine.dispose()
