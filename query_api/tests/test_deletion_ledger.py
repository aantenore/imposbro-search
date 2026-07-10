from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, select

from control_plane import (
    DeletionLedgerRepository,
    DeletionReconciler,
    StaleTombstoneError,
    SuppressionReceipt,
)
from control_plane.schema import deletion_ledger, indexing_event_outbox
from indexing_events import IndexingEventDraft, PostgresIndexingEventStore


QUERY_API_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def deletion_state():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    config = Config(str(QUERY_API_ROOT / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    yield engine, PostgresIndexingEventStore(engine, require_postgres=False), DeletionLedgerRepository(engine)
    engine.dispose()


def delete_draft(key: str, *, routing_revision: int = 3, targets=("cluster-a", "cluster-b")):
    return IndexingEventDraft(
        tenant_id="tenant-a",
        collection="products",
        document_id="doc-1",
        operation="delete",
        target_clusters=tuple(targets),
        routing_revision=routing_revision,
        idempotency_key=key,
        delete_filter="id:=doc-1",
    )


def test_delete_event_and_suppression_commit_atomically(deletion_state):
    _engine, event_store, ledger = deletion_state
    event = event_store.prepare(delete_draft("delete-1"))

    records = ledger.list_restore_suppressions()
    assert len(records) == 1
    assert records[0].identity_hash == event.identity_hash
    assert records[0].tombstone_sequence == event.sequence == 1
    assert records[0].retention_until is None
    assert ledger.pending_targets(event.identity_hash) == ("cluster-a", "cluster-b")
    assert ledger.should_suppress(event.identity_hash, 1)
    assert not ledger.should_suppress(event.identity_hash, 2)


def test_target_reconciliation_requires_current_monotonic_checkpoint(deletion_state):
    _engine, event_store, ledger = deletion_state
    event = event_store.prepare(delete_draft("delete-1"))
    with pytest.raises(StaleTombstoneError):
        ledger.record_target(
            event.identity_hash,
            "cluster-a",
            tombstone_sequence=1,
            checkpoint_sequence=0,
            status="absent",
            receipt="not-found",
        )
    first = ledger.record_target(
        event.identity_hash,
        "cluster-a",
        tombstone_sequence=1,
        checkpoint_sequence=1,
        status="applied",
        receipt="provider-ack-a",
    )
    assert first.converged_at is None
    second = ledger.record_target(
        event.identity_hash,
        "cluster-b",
        tombstone_sequence=1,
        checkpoint_sequence=1,
        status="absent",
        receipt="provider-ack-b",
    )
    assert second.converged_at is not None
    assert ledger.pending_targets(event.identity_hash) == ()


def test_newer_tombstone_resets_targets_and_old_restore_remains_suppressed(deletion_state):
    _engine, event_store, ledger = deletion_state
    first = event_store.prepare(delete_draft("delete-1"))
    second = event_store.prepare(
        delete_draft("delete-2", routing_revision=4, targets=("cluster-c",))
    )

    assert second.sequence == first.sequence + 1
    assert ledger.pending_targets(first.identity_hash) == ("cluster-c",)
    assert ledger.should_suppress(first.identity_hash, first.sequence)
    assert ledger.should_suppress(first.identity_hash, second.sequence)
    assert not ledger.should_suppress(first.identity_hash, second.sequence + 1)


def test_restore_coordinator_deletes_every_target_before_replay_can_resume(deletion_state):
    _engine, event_store, ledger = deletion_state
    tombstone = event_store.prepare(delete_draft("delete-restore"))

    class RestoredTarget:
        def __init__(self):
            self.calls = []

        def suppress(self, record, target_id):
            self.calls.append((record.identity_hash, target_id))
            return SuppressionReceipt(
                identity_hash=record.identity_hash,
                target_id=target_id,
                tombstone_sequence=record.tombstone_sequence,
                checkpoint_sequence=record.tombstone_sequence,
                status="absent" if target_id == "cluster-b" else "applied",
                receipt_id=f"restore-delete:{target_id}",
            )

    target = RestoredTarget()
    reconciler = DeletionReconciler(ledger, target)

    assert not reconciler.replay_allowed(tombstone.identity_hash, tombstone.sequence)
    assert reconciler.reconcile_once() == 2
    assert target.calls == [
        (tombstone.identity_hash, "cluster-a"),
        (tombstone.identity_hash, "cluster-b"),
    ]
    assert ledger.pending_targets(tombstone.identity_hash) == ()
    assert reconciler.reconcile_once() == 0
    # A provider restore can resurrect data after the original receipts. The
    # restore gate therefore re-verifies every target, not only pending rows.
    target.calls.clear()
    assert reconciler.reconcile_restore() == 2
    assert target.calls == [
        (tombstone.identity_hash, "cluster-a"),
        (tombstone.identity_hash, "cluster-b"),
    ]
    assert reconciler.replay_allowed(tombstone.identity_hash, tombstone.sequence + 1)


def test_legal_hold_reference_is_hashed_and_not_exposed(deletion_state):
    engine, event_store, ledger = deletion_state
    event = event_store.prepare(delete_draft("delete-1"))
    ledger.set_legal_hold(
        event.identity_hash,
        enabled=True,
        reference="legal-case-sensitive-reference",
    )
    with engine.connect() as connection:
        row = connection.execute(select(deletion_ledger)).mappings().one()
    assert row["legal_hold"] is True
    assert row["legal_hold_reference_hash"] == hashlib.sha256(
        b"legal-case-sensitive-reference"
    ).hexdigest()
    assert "legal-case-sensitive-reference" not in str(row)


def test_migration_downgrade_refuses_to_drop_nonempty_suppression_ledger(deletion_state):
    engine, event_store, _ledger = deletion_state
    event_store.prepare(delete_draft("delete-1"))
    config = Config(str(QUERY_API_ROOT / "alembic.ini"))
    with pytest.raises(RuntimeError, match="durable evidence"):
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0002_indexing_outbox")


def test_empty_migration_can_downgrade_without_dropping_prior_tables():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    config = Config(str(QUERY_API_ROOT / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0002_indexing_outbox")
    assert "deletion_ledger" not in set(__import__("sqlalchemy").inspect(engine).get_table_names())
    assert "indexing_event_outbox" in set(__import__("sqlalchemy").inspect(engine).get_table_names())
    engine.dispose()


def test_migration_backfills_latest_existing_tombstone():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    config = Config(str(QUERY_API_ROOT / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0002_indexing_outbox")
        connection.execute(
            insert(indexing_event_outbox).values(
                event_id="evt:existing-delete",
                global_position=1,
                idempotency_key_hash="1" * 64,
                request_digest="2" * 64,
                identity_hash="3" * 64,
                sequence=4,
                payload_json={
                    "identity": {
                        "tenant_id": "tenant-a",
                        "collection": "products",
                        "document_id": "doc-existing",
                    },
                    "operation": "delete",
                    "document_version": 4,
                    "routing_revision": 7,
                    "rollout_id": None,
                    "target_clusters": ["cluster-a"],
                },
                created_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
                published_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
                last_error=None,
                publish_attempts=1,
            )
        )
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    record = DeletionLedgerRepository(engine).list_restore_suppressions()[0]
    assert record.identity_hash == "3" * 64
    assert record.tombstone_sequence == 4
    assert DeletionLedgerRepository(engine).pending_targets(record.identity_hash) == (
        "cluster-a",
    )
    engine.dispose()
