"""PostgreSQL adapter for durable indexing sequence allocation and outbox."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import List, Mapping, Optional

from sqlalchemy import Engine, create_engine, func, inspect, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from control_plane.schema import (
    ALEMBIC_HEAD_REVISION,
    indexing_event_heads,
    indexing_event_outbox,
)
from control_plane.deletion_ledger import (
    TombstoneRegistration,
    register_tombstone_in_transaction,
)

from .contracts import (
    IdempotencyConflictError,
    IndexingEventDraft,
    IndexingEventStoreNotReadyError,
    LatestRepairDraft,
    PreparedIndexingEvent,
    RepairSourceNotFoundError,
    build_event_payload,
    build_latest_repair_draft,
    indexing_idempotency_key_hash,
)


_REQUIRED_COLUMNS = {
    indexing_event_heads.name: {
        column.name for column in indexing_event_heads.columns
    },
    indexing_event_outbox.name: {
        column.name for column in indexing_event_outbox.columns
    },
}


class PostgresIndexingEventStore:
    """Allocate monotonic per-document sequences and persist before Kafka send."""

    def __init__(self, engine: Engine | str, *, require_postgres: bool = True):
        self.engine = create_engine(engine) if isinstance(engine, str) else engine
        self.require_postgres = require_postgres

    def check_ready(self) -> None:
        try:
            if self.require_postgres and self.engine.dialect.name != "postgresql":
                raise IndexingEventStoreNotReadyError(
                    "Enterprise indexing event store requires PostgreSQL"
                )
            inspector = inspect(self.engine)
            tables = set(inspector.get_table_names())
            missing = sorted(set(_REQUIRED_COLUMNS) - tables)
            if missing:
                raise IndexingEventStoreNotReadyError(
                    "Indexing event migration is incomplete; missing tables: "
                    + ", ".join(missing)
                )
            for table_name, required in _REQUIRED_COLUMNS.items():
                column_details = {
                    column["name"]: column
                    for column in inspector.get_columns(table_name)
                }
                actual = set(column_details)
                missing_columns = sorted(required - actual)
                if missing_columns:
                    raise IndexingEventStoreNotReadyError(
                        f"Indexing event table '{table_name}' is missing columns: "
                        + ", ".join(missing_columns)
                    )
                if table_name == indexing_event_outbox.name:
                    self._validate_global_position_schema(
                        inspector,
                        column_details,
                    )
            with self.engine.connect() as connection:
                revision = connection.scalar(
                    text("SELECT version_num FROM alembic_version")
                )
            if revision != ALEMBIC_HEAD_REVISION:
                raise IndexingEventStoreNotReadyError(
                    "Indexing event migration revision mismatch: "
                    f"expected {ALEMBIC_HEAD_REVISION}, actual {revision}"
                )
        except IndexingEventStoreNotReadyError:
            raise
        except Exception as exc:
            raise IndexingEventStoreNotReadyError(
                "Indexing event database connection or schema check failed"
            ) from exc

    def _validate_global_position_schema(self, inspector, column_details) -> None:
        position = column_details["global_position"]
        if position.get("nullable", True):
            raise IndexingEventStoreNotReadyError(
                "Indexing outbox global_position must be non-nullable"
            )
        unique_column_sets = {
            frozenset(item.get("column_names") or ())
            for item in inspector.get_unique_constraints(
                indexing_event_outbox.name
            )
        }
        unique_column_sets.update(
            frozenset(item.get("column_names") or ())
            for item in inspector.get_indexes(indexing_event_outbox.name)
            if item.get("unique")
        )
        if frozenset({"global_position"}) not in unique_column_sets:
            raise IndexingEventStoreNotReadyError(
                "Indexing outbox global_position must be unique"
            )
        if self.engine.dialect.name == "postgresql":
            identity = position.get("identity") or {}
            if identity.get("always") is not True:
                raise IndexingEventStoreNotReadyError(
                    "Indexing outbox global_position must be GENERATED ALWAYS"
                )

    def prepare(self, draft: IndexingEventDraft) -> PreparedIndexingEvent:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            existing = self._existing_for_key(
                connection,
                draft.idempotency_key_hash,
            )
            if existing is not None:
                return self._verify_existing(existing, draft)

            self._ensure_head(connection, draft, now)
            head = connection.execute(
                select(indexing_event_heads)
                .where(
                    indexing_event_heads.c.identity_hash == draft.identity_hash
                )
                .with_for_update()
            ).mappings().one()

            # A concurrent request with this key and identity must serialize on
            # the same head before it can allocate another sequence.
            existing = self._existing_for_key(
                connection,
                draft.idempotency_key_hash,
            )
            if existing is not None:
                return self._verify_existing(existing, draft)

            sequence = int(head["last_sequence"]) + 1
            event_id = "evt:" + uuid.uuid4().hex
            payload = build_event_payload(draft, event_id, sequence, now)
            connection.execute(
                update(indexing_event_heads)
                .where(
                    indexing_event_heads.c.identity_hash == draft.identity_hash
                )
                .values(last_sequence=sequence, updated_at=now)
            )
            return self._insert_outbox_event(
                connection,
                draft=draft,
                event_id=event_id,
                sequence=sequence,
                payload=payload,
                now=now,
            )

    def _ensure_head(self, connection, draft: IndexingEventDraft, now: datetime) -> None:
        values = {
            "identity_hash": draft.identity_hash,
            "tenant_id": draft.tenant_id,
            "collection": draft.collection,
            "document_id": draft.document_id,
            "last_sequence": 0,
            "updated_at": now,
        }
        if connection.dialect.name == "postgresql":
            statement = postgres_insert(indexing_event_heads).values(**values)
            statement = statement.on_conflict_do_nothing(
                index_elements=[indexing_event_heads.c.identity_hash]
            )
        elif connection.dialect.name == "sqlite":
            statement = sqlite_insert(indexing_event_heads).values(**values)
            statement = statement.on_conflict_do_nothing(
                index_elements=[indexing_event_heads.c.identity_hash]
            )
        else:
            statement = insert(indexing_event_heads).values(**values)
        connection.execute(statement)

    def _insert_outbox_event(
        self,
        connection,
        *,
        draft: IndexingEventDraft,
        event_id: str,
        sequence: int,
        payload,
        now: datetime,
    ) -> PreparedIndexingEvent:
        values = {
            "event_id": event_id,
            "idempotency_key_hash": draft.idempotency_key_hash,
            "request_digest": draft.request_digest,
            "identity_hash": draft.identity_hash,
            "sequence": sequence,
            "payload_json": payload,
            "created_at": now,
            "published_at": None,
            "last_error": None,
            "publish_attempts": 0,
        }
        if connection.dialect.name == "sqlite":
            current_position = connection.scalar(
                select(func.max(indexing_event_outbox.c.global_position))
            )
            values["global_position"] = int(current_position or 0) + 1
        connection.execute(insert(indexing_event_outbox).values(**values))
        if draft.operation in {"delete", "tombstone"}:
            encoded = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            register_tombstone_in_transaction(
                connection,
                TombstoneRegistration(
                    identity_hash=draft.identity_hash,
                    tenant_id=draft.tenant_id,
                    collection=draft.collection,
                    document_id=draft.document_id,
                    tombstone_sequence=sequence,
                    document_version=sequence,
                    event_id=event_id,
                    event_digest=hashlib.sha256(encoded).hexdigest(),
                    routing_revision=draft.routing_revision,
                    rollout_id=draft.rollout_id or "",
                    target_ids=tuple(draft.target_clusters),
                    requested_at=now,
                    # Indefinite suppression is the safe default. A reviewed
                    # retention policy may only bound it after every replay and
                    # backup horizon is known.
                    retention_until=None,
                ),
                now=now,
            )
        row = connection.execute(
            select(indexing_event_outbox).where(
                indexing_event_outbox.c.event_id == event_id
            )
        ).mappings().one()
        return _row_to_event(row)

    @staticmethod
    def _existing_for_key(connection, key_hash: str):
        return connection.execute(
            select(indexing_event_outbox).where(
                indexing_event_outbox.c.idempotency_key_hash == key_hash
            )
        ).mappings().one_or_none()

    @staticmethod
    def _verify_existing(
        row: Mapping,
        draft: IndexingEventDraft,
    ) -> PreparedIndexingEvent:
        if str(row["request_digest"]) != draft.request_digest:
            raise IdempotencyConflictError(
                "Idempotency key was already used for another mutation"
            )
        return _row_to_event(row)

    def list_pending(self, *, limit: int = 100) -> List[PreparedIndexingEvent]:
        if limit < 1:
            raise ValueError("limit must be positive")
        statement = (
            select(indexing_event_outbox)
            .where(indexing_event_outbox.c.published_at.is_(None))
            .order_by(indexing_event_outbox.c.global_position.asc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [_row_to_event(row) for row in rows]

    def mark_published(self, event_id: str) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(indexing_event_outbox)
                .where(
                    indexing_event_outbox.c.event_id == event_id,
                    indexing_event_outbox.c.published_at.is_(None),
                )
                .values(
                    published_at=datetime.now(timezone.utc),
                    last_error=None,
                    publish_attempts=indexing_event_outbox.c.publish_attempts + 1,
                )
            )
        return result.rowcount == 1

    def record_failure(self, event_id: str, error: str) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(indexing_event_outbox)
                .where(
                    indexing_event_outbox.c.event_id == event_id,
                    indexing_event_outbox.c.published_at.is_(None),
                )
                .values(
                    last_error=str(error)[:4096],
                    publish_attempts=indexing_event_outbox.c.publish_attempts + 1,
                )
            )
        return result.rowcount == 1

    def pending_count(self) -> int:
        with self.engine.connect() as connection:
            value = connection.scalar(
                select(func.count())
                .select_from(indexing_event_outbox)
                .where(indexing_event_outbox.c.published_at.is_(None))
            )
        return int(value or 0)

    def high_water_mark(self) -> int:
        # PostgreSQL identity values are allocated before commit and transactions
        # may commit out of order. A plain MAX(global_position) can therefore
        # expose position N+1 while an older N is still in flight, making a
        # restart cursor skip N forever. SHARE conflicts with INSERT/UPDATE's
        # ROW EXCLUSIVE table lock: it drains existing writers and briefly holds
        # new writers while the barrier is read. Sequence gaps remain valid.
        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                qualified_table = connection.dialect.identifier_preparer.format_table(
                    indexing_event_outbox
                )
                connection.exec_driver_sql(
                    f"LOCK TABLE {qualified_table} IN SHARE MODE"
                )
            value = connection.scalar(
                select(func.max(indexing_event_outbox.c.global_position))
            )
        return int(value or 0)

    def list_latest_by_identity(
        self,
        *,
        after_position: int,
        through_position: int,
        limit: int = 1000,
    ) -> List[PreparedIndexingEvent]:
        _validate_position_window(after_position, through_position, limit)
        columns = list(indexing_event_outbox.columns)
        ranked = (
            select(
                *columns,
                func.row_number()
                .over(
                    partition_by=indexing_event_outbox.c.identity_hash,
                    order_by=indexing_event_outbox.c.global_position.desc(),
                )
                .label("identity_rank"),
            )
            .where(
                indexing_event_outbox.c.global_position > after_position,
                indexing_event_outbox.c.global_position <= through_position,
            )
            .subquery()
        )
        statement = (
            select(*(ranked.c[column.name] for column in columns))
            .where(ranked.c.identity_rank == 1)
            .order_by(ranked.c.global_position.asc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [_row_to_event(row) for row in rows]

    def prepare_latest_repair(
        self,
        repair: LatestRepairDraft,
    ) -> PreparedIndexingEvent:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            head = connection.execute(
                select(indexing_event_heads)
                .where(
                    indexing_event_heads.c.identity_hash == repair.identity_hash
                )
                .with_for_update()
            ).mappings().one_or_none()
            if head is None:
                raise RepairSourceNotFoundError(
                    "No durable indexing event exists for the repair identity"
                )
            key_hash = indexing_idempotency_key_hash(
                str(head["tenant_id"]),
                str(head["collection"]),
                str(head["document_id"]),
                repair.idempotency_key,
            )
            existing = self._existing_for_key(connection, key_hash)
            if existing is not None:
                return _row_to_event(existing)
            source_row = connection.execute(
                select(indexing_event_outbox)
                .where(
                    indexing_event_outbox.c.identity_hash == repair.identity_hash
                )
                .order_by(indexing_event_outbox.c.sequence.desc())
                .limit(1)
            ).mappings().one_or_none()
            if source_row is None:
                raise RepairSourceNotFoundError(
                    "Indexing head has no corresponding durable event"
                )
            draft = build_latest_repair_draft(repair, _row_to_event(source_row))
            sequence = int(head["last_sequence"]) + 1
            event_id = "evt:" + uuid.uuid4().hex
            payload = build_event_payload(draft, event_id, sequence, now)
            connection.execute(
                update(indexing_event_heads)
                .where(
                    indexing_event_heads.c.identity_hash == repair.identity_hash
                )
                .values(last_sequence=sequence, updated_at=now)
            )
            return self._insert_outbox_event(
                connection,
                draft=draft,
                event_id=event_id,
                sequence=sequence,
                payload=payload,
                now=now,
            )


def _row_to_event(row: Mapping) -> PreparedIndexingEvent:
    created_at = row["created_at"]
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    published_at: Optional[datetime] = row["published_at"]
    if published_at is not None and published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return PreparedIndexingEvent(
        event_id=str(row["event_id"]),
        sequence=int(row["sequence"]),
        payload=dict(row["payload_json"]),
        created_at=created_at,
        global_position=int(row["global_position"]),
        published_at=published_at,
        publish_attempts=int(row["publish_attempts"]),
        last_error=row["last_error"],
    )


def _validate_position_window(
    after_position: int,
    through_position: int,
    limit: int,
) -> None:
    if after_position < 0 or through_position < after_position:
        raise ValueError("Position window must satisfy 0 <= after <= through")
    if limit < 1:
        raise ValueError("limit must be positive")
