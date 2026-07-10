"""Fenced, event-scoped checkpoints for versioned indexing events."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ContextManager, Dict, Iterator, Literal, Mapping, Optional, Protocol

import typesense
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Engine,
    Index,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from event_envelope import (
    EVENT_ID_PATTERN,
    OPERATIONS,
    IndexingEventV2,
    NAME_PATTERN,
    indexing_event_digest,
)


DEFAULT_CHECKPOINT_COLLECTION = "_imposbro_indexing_checkpoints"
DEFAULT_LOCK_TIMEOUT_MS = 5000
EXPECTED_ALEMBIC_REVISION = "0003_audit_delivery_deletion"
POSTGRES_CHECKPOINT_TABLE = "indexing_checkpoints"
READINESS_ID_NAMESPACE = "imposbro-indexing-checkpoint-readiness-v1"
PRODUCTION_PROFILES = {"production", "enterprise"}
DEPLOYMENT_PROFILES = {"development", "test", *PRODUCTION_PROFILES}
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}

checkpoint_metadata = MetaData()
postgres_checkpoints = Table(
    POSTGRES_CHECKPOINT_TABLE,
    checkpoint_metadata,
    Column("checkpoint_key", String(64), primary_key=True),
    Column("identity_key", Text, nullable=False),
    Column("tenant_id", String(256), nullable=False),
    Column("collection", String(128), nullable=False),
    Column("document_id", String(256), nullable=False),
    Column("target_cluster", String(128), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("document_version", BigInteger, nullable=False),
    Column("operation", String(16), nullable=False),
    Column("event_id", String(128), nullable=False),
    Column("event_digest", String(64), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("applied_at_ms", BigInteger, nullable=False),
    Column("tombstone", Boolean, nullable=False),
    Column("routing_revision", BigInteger, nullable=False),
    Column("rollout_id", String(128), nullable=False, default=""),
    UniqueConstraint(
        "identity_key",
        "target_cluster",
        name="uq_indexing_checkpoint_identity_target",
    ),
    CheckConstraint(
        "length(checkpoint_key) = 64",
        name="ck_indexing_checkpoint_key_hash",
    ),
    CheckConstraint("sequence >= 1", name="ck_indexing_checkpoint_sequence"),
    CheckConstraint(
        "document_version >= 1",
        name="ck_indexing_checkpoint_document_version",
    ),
    CheckConstraint(
        "routing_revision >= 1",
        name="ck_indexing_checkpoint_routing_revision",
    ),
    CheckConstraint(
        "applied_at_ms >= 0",
        name="ck_indexing_checkpoint_applied_at",
    ),
    CheckConstraint(
        "operation IN ('upsert', 'delete', 'tombstone')",
        name="ck_indexing_checkpoint_operation",
    ),
    CheckConstraint(
        "length(event_digest) = 64",
        name="ck_indexing_checkpoint_event_digest",
    ),
    CheckConstraint(
        "((operation = 'upsert' AND tombstone = false) OR "
        "(operation IN ('delete', 'tombstone') AND tombstone = true))",
        name="ck_indexing_checkpoint_tombstone",
    ),
)
Index(
    "ix_indexing_checkpoints_tenant_collection",
    postgres_checkpoints.c.tenant_id,
    postgres_checkpoints.c.collection,
)

CHECKPOINT_SCHEMA_FIELDS = {
    "identity_key": "string",
    "tenant_id": "string",
    "collection": "string",
    "document_id": "string",
    "target_cluster": "string",
    "sequence": "int64",
    "document_version": "int64",
    "operation": "string",
    "event_id": "string",
    "event_digest": "string",
    "occurred_at": "string",
    "applied_at_ms": "int64",
    "tombstone": "bool",
    "routing_revision": "int64",
    "rollout_id": "string",
}


class CheckpointStoreError(RuntimeError):
    """Raised when idempotency state cannot be safely read or advanced."""


class CheckpointLockTimeoutError(CheckpointStoreError):
    """Raised when an event cannot obtain every fence before the deadline."""


class EventOrderingError(RuntimeError):
    """Raised on contradictory version/sequence metadata."""


@dataclass(frozen=True)
class TargetCheckpoint:
    identity_key: str
    tenant_id: str
    collection: str
    document_id: str
    target_cluster: str
    sequence: int
    document_version: int
    operation: str
    event_id: str
    event_digest: str
    occurred_at: str
    applied_at_ms: int
    tombstone: bool
    routing_revision: int
    rollout_id: str = ""


CheckpointDecision = Literal["apply", "duplicate", "stale"]


class EventCheckpointSession(Protocol):
    backend_name: str

    def load(
        self,
        event: IndexingEventV2,
        target_cluster: str,
    ) -> Optional[TargetCheckpoint]:
        """Load one target while every event target fence remains held."""

    def save(self, checkpoint: TargetCheckpoint) -> None:
        """Stage or persist a target checkpoint inside the fenced scope."""


class CheckpointStore(Protocol):
    backend_name: str

    def ensure_ready(self) -> None:
        """Fail closed unless schema, migration, permissions, and backend are ready."""

    def event_scope(
        self,
        event: IndexingEventV2,
    ) -> ContextManager[EventCheckpointSession]:
        """Fence all event identity/target pairs until processing completes."""

    def close(self) -> None:
        """Release backend resources owned by the adapter."""


@dataclass
class _LockEntry:
    lock: threading.RLock
    users: int = 0


class _DeterministicLockRegistry:
    """Reference-counted in-process fences acquired in canonical key order."""

    def __init__(self, timeout_ms: int):
        self.timeout_ms = timeout_ms
        self._guard = threading.Lock()
        self._entries: Dict[str, _LockEntry] = {}

    @contextmanager
    def hold(self, keys) -> Iterator[None]:
        ordered_keys = sorted(set(keys))
        with self._guard:
            entries = []
            for key in ordered_keys:
                entry = self._entries.get(key)
                if entry is None:
                    entry = _LockEntry(threading.RLock())
                    self._entries[key] = entry
                entry.users += 1
                entries.append((key, entry))

        acquired = []
        deadline = time.monotonic() + (self.timeout_ms / 1000)
        try:
            for key, entry in entries:
                remaining = max(0.0, deadline - time.monotonic())
                if not entry.lock.acquire(timeout=remaining):
                    raise CheckpointLockTimeoutError(
                        "Timed out acquiring checkpoint fence"
                    )
                acquired.append((key, entry))
            yield
        finally:
            for _key, entry in reversed(acquired):
                entry.lock.release()
            with self._guard:
                for key, entry in entries:
                    entry.users -= 1
                    if entry.users == 0:
                        self._entries.pop(key, None)


class _InMemoryEventSession:
    backend_name = "memory"

    def __init__(self, store: "InMemoryCheckpointStore", event: IndexingEventV2):
        self._store = store
        self._event = event
        self._allowed = set(event.target_clusters)
        self._staged: Dict[str, TargetCheckpoint] = {}

    def load(
        self,
        event: IndexingEventV2,
        target_cluster: str,
    ) -> Optional[TargetCheckpoint]:
        self._validate(event, target_cluster)
        key = checkpoint_id(event, target_cluster)
        if key in self._staged:
            return self._staged[key]
        return self._store._load_key(key)

    def save(self, checkpoint: TargetCheckpoint) -> None:
        if checkpoint.target_cluster not in self._allowed:
            raise CheckpointStoreError("Checkpoint target was not fenced")
        key = checkpoint_document_id(checkpoint)
        self._store._validate_advance(key, checkpoint)
        self._staged[key] = checkpoint

    def commit_progress(self) -> None:
        self._store._commit_staged(self._staged)

    def _validate(self, event: IndexingEventV2, target_cluster: str) -> None:
        if event.identity != self._event.identity or target_cluster not in self._allowed:
            raise CheckpointStoreError("Checkpoint identity/target was not fenced")


class InMemoryCheckpointStore:
    """Transactional in-process adapter for tests and explicit local development."""

    backend_name = "memory"

    def __init__(self, *, lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS):
        self.lock_timeout_ms = validate_lock_timeout(lock_timeout_ms)
        self._records_lock = threading.RLock()
        self._records: Dict[str, TargetCheckpoint] = {}
        self._fences = _DeterministicLockRegistry(self.lock_timeout_ms)

    def ensure_ready(self) -> None:
        return None

    @contextmanager
    def event_scope(self, event: IndexingEventV2) -> Iterator[_InMemoryEventSession]:
        with self._fences.hold(event_checkpoint_keys(event)):
            session = _InMemoryEventSession(self, event)
            try:
                yield session
            finally:
                # Preserve per-target progress even when a later target fails.
                session.commit_progress()

    def load(
        self,
        event: IndexingEventV2,
        target_cluster: str,
    ) -> Optional[TargetCheckpoint]:
        return self._load_key(checkpoint_id(event, target_cluster))

    def save(self, checkpoint: TargetCheckpoint) -> None:
        key = checkpoint_document_id(checkpoint)
        self._validate_advance(key, checkpoint)
        self._commit_staged({key: checkpoint})

    def close(self) -> None:
        return None

    def _load_key(self, key: str) -> Optional[TargetCheckpoint]:
        with self._records_lock:
            return self._records.get(key)

    def _validate_advance(self, key: str, checkpoint: TargetCheckpoint) -> None:
        with self._records_lock:
            current = self._records.get(key)
        if current is None:
            return
        if (
            checkpoint.event_id == current.event_id
            and checkpoint.event_digest == current.event_digest
            and checkpoint.sequence == current.sequence
            and checkpoint.document_version == current.document_version
        ):
            return
        if (
            checkpoint.sequence <= current.sequence
            or checkpoint.document_version <= current.document_version
        ):
            raise CheckpointStoreError("Checkpoint advancement would regress state")

    def _commit_staged(self, staged: Mapping[str, TargetCheckpoint]) -> None:
        with self._records_lock:
            for key, checkpoint in staged.items():
                current = self._records.get(key)
                if current is not None and (
                    checkpoint.sequence < current.sequence
                    or checkpoint.document_version < current.document_version
                ):
                    raise CheckpointStoreError(
                        "Concurrent checkpoint commit would regress state"
                    )
                self._records[key] = checkpoint


class TypesenseCheckpointStore:
    """Development-only durable adapter; cross-process fencing is unavailable."""

    backend_name = "typesense"

    def __init__(
        self,
        clients: Mapping[str, typesense.Client],
        *,
        collection_name: str = DEFAULT_CHECKPOINT_COLLECTION,
        lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    ):
        if not NAME_PATTERN.fullmatch(collection_name):
            raise CheckpointStoreError("Invalid checkpoint collection name")
        self._clients = clients
        self.collection_name = collection_name
        self.lock_timeout_ms = validate_lock_timeout(lock_timeout_ms)
        self._ready_client_ids: Dict[str, int] = {}
        self._lock = threading.RLock()
        self._fences = _DeterministicLockRegistry(self.lock_timeout_ms)

    def ensure_ready(self) -> None:
        if not self._clients:
            raise CheckpointStoreError("No target clusters available for checkpoints")
        for target_cluster, client in self._clients.items():
            if (
                not isinstance(target_cluster, str)
                or not NAME_PATTERN.fullmatch(target_cluster)
            ):
                raise CheckpointStoreError("Invalid target cluster name")
            self._ensure_cluster(target_cluster, client)

    @contextmanager
    def event_scope(self, event: IndexingEventV2) -> Iterator["TypesenseCheckpointStore"]:
        with self._fences.hold(event_checkpoint_keys(event)):
            yield self

    def load(
        self,
        event: IndexingEventV2,
        target_cluster: str,
    ) -> Optional[TargetCheckpoint]:
        client = self._client(target_cluster)
        checkpoint_key = checkpoint_id(event, target_cluster)
        try:
            document = (
                client.collections[self.collection_name]
                .documents[checkpoint_key]
                .retrieve()
            )
        except typesense.exceptions.ObjectNotFound:
            return None
        except Exception as exc:
            raise CheckpointStoreError(
                f"Failed to load checkpoint for target '{target_cluster}'"
            ) from exc
        return checkpoint_from_document(document, event, target_cluster)

    def save(self, checkpoint: TargetCheckpoint) -> None:
        client = self._client(checkpoint.target_cluster)
        document = checkpoint_document(checkpoint)
        try:
            client.collections[self.collection_name].documents.upsert(document)
        except Exception as exc:
            raise CheckpointStoreError(
                f"Failed to persist checkpoint for target '{checkpoint.target_cluster}'"
            ) from exc

    def close(self) -> None:
        return None

    def _client(self, target_cluster: str):
        client = self._clients.get(target_cluster)
        if client is None:
            raise CheckpointStoreError(
                f"No checkpoint client for target '{target_cluster}'"
            )
        self._ensure_cluster(target_cluster, client)
        return client

    def _ensure_cluster(self, target_cluster: str, client) -> None:
        with self._lock:
            if self._ready_client_ids.get(target_cluster) == id(client):
                return
            try:
                existing = client.collections[self.collection_name].retrieve()
                self._validate_existing_schema(target_cluster, existing)
            except typesense.exceptions.ObjectNotFound:
                try:
                    client.collections.create(
                        checkpoint_collection_schema(self.collection_name)
                    )
                except typesense.exceptions.ObjectAlreadyExists:
                    existing = client.collections[self.collection_name].retrieve()
                    self._validate_existing_schema(target_cluster, existing)
                except Exception as exc:
                    raise CheckpointStoreError(
                        f"Failed to create checkpoint collection on '{target_cluster}'"
                    ) from exc
            except CheckpointStoreError:
                raise
            except Exception as exc:
                raise CheckpointStoreError(
                    f"Failed to verify checkpoint collection on '{target_cluster}'"
                ) from exc
            self._probe_cluster_writable(target_cluster, client)
            self._ready_client_ids[target_cluster] = id(client)

    def _validate_existing_schema(
        self,
        target_cluster: str,
        schema: Mapping,
    ) -> None:
        if (
            schema.get("name") != self.collection_name
            or schema.get("default_sorting_field") != "applied_at_ms"
        ):
            raise CheckpointStoreError(
                f"Checkpoint schema mismatch on '{target_cluster}': collection metadata"
            )
        actual = {
            str(field.get("name")): str(field.get("type"))
            for field in schema.get("fields", [])
            if isinstance(field, dict)
        }
        mismatches = [
            name
            for name, expected_type in CHECKPOINT_SCHEMA_FIELDS.items()
            if actual.get(name) != expected_type
        ]
        if mismatches:
            raise CheckpointStoreError(
                f"Checkpoint schema mismatch on '{target_cluster}': "
                + ", ".join(sorted(mismatches))
            )

    def _probe_cluster_writable(self, target_cluster: str, client) -> None:
        document = readiness_probe_document(target_cluster)
        try:
            client.collections[self.collection_name].documents.upsert(document)
            persisted = (
                client.collections[self.collection_name]
                .documents[document["id"]]
                .retrieve()
            )
        except Exception as exc:
            raise CheckpointStoreError(
                f"Checkpoint collection is not writable on '{target_cluster}'"
            ) from exc
        if (
            persisted.get("event_digest") != document["event_digest"]
            or persisted.get("target_cluster") != target_cluster
        ):
            raise CheckpointStoreError(
                f"Checkpoint write verification failed on '{target_cluster}'"
            )


class _PostgresEventSession:
    backend_name = "postgres"

    def __init__(
        self,
        connection: Connection,
        event: IndexingEventV2,
        locked_keys: Mapping[str, str],
    ):
        self._connection = connection
        self._event = event
        self._locked_keys = dict(locked_keys)

    def load(
        self,
        event: IndexingEventV2,
        target_cluster: str,
    ) -> Optional[TargetCheckpoint]:
        checkpoint_key = self._validate(event, target_cluster)
        try:
            row = self._connection.execute(
                select(postgres_checkpoints)
                .where(postgres_checkpoints.c.checkpoint_key == checkpoint_key)
                .with_for_update()
            ).mappings().one_or_none()
        except Exception as exc:
            raise CheckpointStoreError(
                f"Failed to load PostgreSQL checkpoint for '{target_cluster}'"
            ) from exc
        if row is None:
            return None
        return checkpoint_from_document(row, event, target_cluster)

    def save(self, checkpoint: TargetCheckpoint) -> None:
        expected_key = self._locked_keys.get(checkpoint.target_cluster)
        actual_key = checkpoint_document_id(checkpoint)
        if expected_key != actual_key:
            raise CheckpointStoreError("Checkpoint save was not covered by an event fence")
        values = postgres_checkpoint_values(checkpoint)
        statement = postgres_insert(postgres_checkpoints).values(**values)
        excluded = statement.excluded
        update_values = {
            column.name: getattr(excluded, column.name)
            for column in postgres_checkpoints.columns
            if column.name != "checkpoint_key"
        }
        statement = statement.on_conflict_do_update(
            index_elements=[postgres_checkpoints.c.checkpoint_key],
            set_=update_values,
            where=and_(
                postgres_checkpoints.c.identity_key == excluded.identity_key,
                postgres_checkpoints.c.target_cluster == excluded.target_cluster,
                postgres_checkpoints.c.sequence < excluded.sequence,
                postgres_checkpoints.c.document_version < excluded.document_version,
            ),
        ).returning(postgres_checkpoints.c.checkpoint_key)
        try:
            persisted_key = self._connection.execute(statement).scalar_one_or_none()
        except Exception as exc:
            raise CheckpointStoreError(
                f"Failed to persist PostgreSQL checkpoint for '{checkpoint.target_cluster}'"
            ) from exc
        # psycopg/SQLAlchemy rowcount is not reliable for INSERT ... ON
        # CONFLICT DO UPDATE. RETURNING is authoritative: a false CAS predicate
        # returns no row, while a committed insert/update returns the fenced key.
        if persisted_key != actual_key:
            raise CheckpointStoreError(
                "PostgreSQL checkpoint compare-and-set rejected a non-monotonic write"
            )

    def _validate(self, event: IndexingEventV2, target_cluster: str) -> str:
        if event.identity != self._event.identity:
            raise CheckpointStoreError("Checkpoint identity was not fenced")
        checkpoint_key = self._locked_keys.get(target_cluster)
        if checkpoint_key is None:
            raise CheckpointStoreError("Checkpoint target was not fenced")
        return checkpoint_key


class PostgresCheckpointStore:
    """Production adapter using transaction-scoped PostgreSQL advisory fences."""

    backend_name = "postgres"

    def __init__(
        self,
        engine: Engine | str,
        *,
        lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
        expected_migration_revision: str = EXPECTED_ALEMBIC_REVISION,
    ):
        self._owns_engine = isinstance(engine, str)
        self.engine = (
            create_engine(normalize_postgres_url(engine), pool_pre_ping=True)
            if isinstance(engine, str)
            else engine
        )
        self.lock_timeout_ms = validate_lock_timeout(lock_timeout_ms)
        self.expected_migration_revision = expected_migration_revision
        self._ready = False
        self._ready_lock = threading.Lock()

    def ensure_ready(self) -> None:
        with self._ready_lock:
            if self._ready:
                return
            try:
                if (
                    self.engine.dialect.name != "postgresql"
                    or self.engine.dialect.driver != "psycopg"
                ):
                    raise CheckpointStoreError(
                        "Production checkpoint store requires PostgreSQL with psycopg"
                    )
                inspector = inspect(self.engine)
                table_names = set(inspector.get_table_names())
                if POSTGRES_CHECKPOINT_TABLE not in table_names:
                    raise CheckpointStoreError(
                        "Checkpoint migration is incomplete; missing indexing_checkpoints"
                    )
                if "alembic_version" not in table_names:
                    raise CheckpointStoreError("Checkpoint Alembic revision is missing")
                expected_columns = {
                    column.name for column in postgres_checkpoints.columns
                }
                actual_column_details = {
                    column["name"]: column
                    for column in inspector.get_columns(POSTGRES_CHECKPOINT_TABLE)
                }
                actual_columns = set(actual_column_details)
                missing_columns = sorted(expected_columns - actual_columns)
                if missing_columns:
                    raise CheckpointStoreError(
                        "Checkpoint table is missing columns: "
                        + ", ".join(missing_columns)
                    )
                nullable_columns = sorted(
                    column.name
                    for column in postgres_checkpoints.columns
                    if not column.nullable
                    and actual_column_details[column.name].get("nullable", True)
                )
                if nullable_columns:
                    raise CheckpointStoreError(
                        "Checkpoint table unexpectedly allows nulls: "
                        + ", ".join(nullable_columns)
                    )
                primary_key = set(
                    inspector.get_pk_constraint(POSTGRES_CHECKPOINT_TABLE).get(
                        "constrained_columns",
                        [],
                    )
                )
                if primary_key != {"checkpoint_key"}:
                    raise CheckpointStoreError("Checkpoint primary key is incompatible")
                unique_constraints = {
                    frozenset(constraint.get("column_names") or ())
                    for constraint in inspector.get_unique_constraints(
                        POSTGRES_CHECKPOINT_TABLE
                    )
                }
                if frozenset({"identity_key", "target_cluster"}) not in unique_constraints:
                    raise CheckpointStoreError(
                        "Checkpoint identity/target uniqueness constraint is missing"
                    )
                with self.engine.connect() as connection:
                    revision = connection.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
                    if revision != self.expected_migration_revision:
                        raise CheckpointStoreError(
                            "Checkpoint migration revision mismatch: "
                            f"expected {self.expected_migration_revision}, actual {revision}"
                        )
                    connection.rollback()
                    self._read_write_probe(connection)
            except CheckpointStoreError:
                raise
            except Exception as exc:
                raise CheckpointStoreError(
                    "PostgreSQL checkpoint connection or schema check failed"
                ) from exc
            self._ready = True

    @contextmanager
    def event_scope(self, event: IndexingEventV2) -> Iterator[_PostgresEventSession]:
        locked_keys = {
            target: checkpoint_id(event, target)
            for target in event.target_clusters
        }
        connection = self.engine.connect()
        transaction = connection.begin()
        try:
            deadline = time.monotonic() + (self.lock_timeout_ms / 1000)
            for checkpoint_key in sorted(locked_keys.values()):
                remaining_ms = max(
                    1,
                    int((deadline - time.monotonic()) * 1000),
                )
                self._set_lock_timeout(connection, remaining_ms)
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_key)"),
                    {"lock_key": advisory_lock_key(checkpoint_key)},
                )
            self._set_lock_timeout(connection)
        except Exception as exc:
            if transaction.is_active:
                transaction.rollback()
            connection.close()
            raise translate_postgres_error(
                exc,
                "Failed to acquire PostgreSQL checkpoint fences",
            ) from exc

        session = _PostgresEventSession(connection, event, locked_keys)
        try:
            yield session
        except BaseException as processing_error:
            # Successful targets already saved in this transaction remain useful
            # progress even if a later Typesense target fails.
            try:
                transaction.commit()
            except Exception as commit_error:
                if transaction.is_active:
                    transaction.rollback()
                raise translate_postgres_error(
                    commit_error,
                    "Failed to commit partial checkpoint progress",
                ) from processing_error
            raise
        else:
            try:
                transaction.commit()
            except Exception as exc:
                if transaction.is_active:
                    transaction.rollback()
                raise translate_postgres_error(
                    exc,
                    "Failed to commit PostgreSQL checkpoints",
                ) from exc
        finally:
            connection.close()

    def close(self) -> None:
        if self._owns_engine:
            self.engine.dispose()

    def _set_lock_timeout(
        self,
        connection: Connection,
        timeout_ms: Optional[int] = None,
    ) -> None:
        effective_timeout = timeout_ms or self.lock_timeout_ms
        connection.exec_driver_sql(
            f"SET LOCAL lock_timeout = '{effective_timeout}ms'"
        )

    def _read_write_probe(self, connection: Connection) -> None:
        transaction = connection.begin()
        checkpoint = readiness_probe_checkpoint("_postgres_readiness")
        try:
            self._set_lock_timeout(connection)
            checkpoint_key = checkpoint_document_id(checkpoint)
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": advisory_lock_key(checkpoint_key)},
            )
            values = postgres_checkpoint_values(checkpoint)
            statement = postgres_insert(postgres_checkpoints).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=[postgres_checkpoints.c.checkpoint_key],
                set_={
                    "event_digest": statement.excluded.event_digest,
                    "applied_at_ms": statement.excluded.applied_at_ms,
                },
            )
            connection.execute(statement)
            persisted = connection.execute(
                select(postgres_checkpoints.c.event_digest).where(
                    postgres_checkpoints.c.checkpoint_key == checkpoint_key
                )
            ).scalar_one()
            if persisted != checkpoint.event_digest:
                raise CheckpointStoreError(
                    "PostgreSQL checkpoint write verification failed"
                )
        finally:
            transaction.rollback()


def classify_event(
    event: IndexingEventV2,
    checkpoint: Optional[TargetCheckpoint],
) -> CheckpointDecision:
    if checkpoint is None:
        return "apply"

    same_position = (
        event.sequence == checkpoint.sequence
        and event.document_version == checkpoint.document_version
    )
    if event.event_id == checkpoint.event_id:
        if (
            same_position
            and event.operation == checkpoint.operation
            and indexing_event_digest(event) == checkpoint.event_digest
        ):
            return "duplicate"
        raise EventOrderingError("event_id was reused with different event contents")

    if same_position:
        raise EventOrderingError(
            "different event_id values share the same sequence and document_version"
        )

    sequence_is_older = event.sequence < checkpoint.sequence
    version_is_older = event.document_version < checkpoint.document_version
    if sequence_is_older and version_is_older:
        return "stale"
    if (
        event.sequence <= checkpoint.sequence
        or event.document_version <= checkpoint.document_version
    ):
        raise EventOrderingError(
            "sequence and document_version must both increase for a new event"
        )
    return "apply"


def checkpoint_from_event(
    event: IndexingEventV2,
    target_cluster: str,
    *,
    applied_at: Optional[datetime] = None,
) -> TargetCheckpoint:
    applied_at = applied_at or datetime.now(timezone.utc)
    return TargetCheckpoint(
        identity_key=event.identity.logical_key,
        tenant_id=event.identity.tenant_id,
        collection=event.identity.collection,
        document_id=event.identity.document_id,
        target_cluster=target_cluster,
        sequence=event.sequence,
        document_version=event.document_version,
        operation=event.operation,
        event_id=event.event_id,
        event_digest=indexing_event_digest(event),
        occurred_at=event.occurred_at.isoformat(),
        applied_at_ms=int(applied_at.timestamp() * 1000),
        tombstone=event.operation in {"delete", "tombstone"},
        routing_revision=event.routing_revision,
        rollout_id=event.rollout_id or "",
    )


def checkpoint_id(event: IndexingEventV2, target_cluster: str) -> str:
    material = f"{event.identity.logical_key}\x1f{target_cluster}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def checkpoint_document_id(checkpoint: TargetCheckpoint) -> str:
    material = f"{checkpoint.identity_key}\x1f{checkpoint.target_cluster}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def event_checkpoint_keys(event: IndexingEventV2) -> tuple[str, ...]:
    return tuple(
        sorted(checkpoint_id(event, target) for target in event.target_clusters)
    )


def advisory_lock_key(checkpoint_key: str) -> int:
    """Map a SHA-256 checkpoint ID to PostgreSQL's signed 64-bit lock space."""
    unsigned = int(checkpoint_key[:16], 16)
    return unsigned - (1 << 64) if unsigned >= (1 << 63) else unsigned


def checkpoint_document(checkpoint: TargetCheckpoint) -> Dict:
    return {
        "id": checkpoint_document_id(checkpoint),
        "identity_key": checkpoint.identity_key,
        "tenant_id": checkpoint.tenant_id,
        "collection": checkpoint.collection,
        "document_id": checkpoint.document_id,
        "target_cluster": checkpoint.target_cluster,
        "sequence": checkpoint.sequence,
        "document_version": checkpoint.document_version,
        "operation": checkpoint.operation,
        "event_id": checkpoint.event_id,
        "event_digest": checkpoint.event_digest,
        "occurred_at": checkpoint.occurred_at,
        "applied_at_ms": checkpoint.applied_at_ms,
        "tombstone": checkpoint.tombstone,
        "routing_revision": checkpoint.routing_revision,
        "rollout_id": checkpoint.rollout_id,
    }


def postgres_checkpoint_values(checkpoint: TargetCheckpoint) -> Dict:
    document = checkpoint_document(checkpoint)
    document["checkpoint_key"] = document.pop("id")
    document["occurred_at"] = datetime.fromisoformat(checkpoint.occurred_at)
    return document


def checkpoint_from_document(
    document: Mapping,
    event: IndexingEventV2,
    target_cluster: str,
) -> TargetCheckpoint:
    if not isinstance(document, Mapping):
        raise CheckpointStoreError(
            f"Invalid checkpoint stored for target '{target_cluster}'"
        )
    try:
        sequence = _stored_int(document, "sequence", minimum=1)
        document_version = _stored_int(document, "document_version", minimum=1)
        applied_at_ms = _stored_int(document, "applied_at_ms", minimum=0)
        routing_revision = _stored_int(document, "routing_revision", minimum=1)
        tombstone = document["tombstone"]
        if not isinstance(tombstone, bool):
            raise TypeError("tombstone must be a bool")
        checkpoint = TargetCheckpoint(
            identity_key=_stored_string(document, "identity_key"),
            tenant_id=_stored_string(document, "tenant_id"),
            collection=_stored_string(document, "collection"),
            document_id=_stored_string(document, "document_id"),
            target_cluster=_stored_string(document, "target_cluster"),
            sequence=sequence,
            document_version=document_version,
            operation=_stored_string(document, "operation"),
            event_id=_stored_string(document, "event_id"),
            event_digest=_stored_string(document, "event_digest"),
            occurred_at=_stored_timestamp(document, "occurred_at"),
            applied_at_ms=applied_at_ms,
            tombstone=tombstone,
            routing_revision=routing_revision,
            rollout_id=_stored_string(document, "rollout_id", allow_empty=True),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointStoreError(
            f"Invalid checkpoint stored for target '{target_cluster}'"
        ) from exc

    expected_identity = event.identity
    if (
        checkpoint.identity_key != expected_identity.logical_key
        or checkpoint.tenant_id != expected_identity.tenant_id
        or checkpoint.collection != expected_identity.collection
        or checkpoint.document_id != expected_identity.document_id
        or checkpoint.target_cluster != target_cluster
    ):
        raise CheckpointStoreError(
            f"Checkpoint identity mismatch for target '{target_cluster}'"
        )
    if checkpoint.operation not in OPERATIONS:
        raise CheckpointStoreError(
            f"Invalid checkpoint operation for target '{target_cluster}'"
        )
    expected_tombstone = checkpoint.operation in {"delete", "tombstone"}
    if checkpoint.tombstone != expected_tombstone:
        raise CheckpointStoreError(
            f"Checkpoint tombstone mismatch for target '{target_cluster}'"
        )
    if not EVENT_ID_PATTERN.fullmatch(checkpoint.event_id):
        raise CheckpointStoreError(
            f"Invalid checkpoint event ID for target '{target_cluster}'"
        )
    if not _is_sha256(checkpoint.event_digest):
        raise CheckpointStoreError(
            f"Invalid checkpoint digest for target '{target_cluster}'"
        )
    if checkpoint.rollout_id and not NAME_PATTERN.fullmatch(checkpoint.rollout_id):
        raise CheckpointStoreError(
            f"Invalid checkpoint rollout ID for target '{target_cluster}'"
        )
    return checkpoint


def _stored_string(
    document: Mapping,
    field_name: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = document[field_name]
    if not isinstance(value, str) or (not value and not allow_empty):
        raise TypeError(f"{field_name} must be a string")
    return value


def _stored_timestamp(document: Mapping, field_name: str) -> str:
    value = document[field_name]
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    else:
        raise TypeError(f"{field_name} must be a timestamp")
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _stored_int(document: Mapping, field_name: str, *, minimum: int) -> int:
    value = document[field_name]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise TypeError(f"{field_name} must be an integer")
    return value


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def readiness_probe_checkpoint(target_cluster: str) -> TargetCheckpoint:
    now = datetime.now(timezone.utc)
    probe_id = hashlib.sha256(
        f"{READINESS_ID_NAMESPACE}\x1f{target_cluster}".encode("utf-8")
    ).hexdigest()
    return TargetCheckpoint(
        identity_key=READINESS_ID_NAMESPACE,
        tenant_id="_system",
        collection="_checkpoint_readiness",
        document_id=probe_id,
        target_cluster=target_cluster,
        sequence=1,
        document_version=1,
        operation="upsert",
        event_id="checkpoint-readiness-v1",
        event_digest=hashlib.sha256(
            READINESS_ID_NAMESPACE.encode("utf-8")
        ).hexdigest(),
        occurred_at=now.isoformat(),
        applied_at_ms=int(now.timestamp() * 1000),
        tombstone=False,
        routing_revision=1,
        rollout_id="",
    )


def readiness_probe_document(target_cluster: str) -> Dict:
    return checkpoint_document(readiness_probe_checkpoint(target_cluster))


def checkpoint_collection_schema(collection_name: str) -> Dict:
    fields = []
    for name, field_type in CHECKPOINT_SCHEMA_FIELDS.items():
        field = {"name": name, "type": field_type}
        if name in {"tenant_id", "collection", "target_cluster", "operation"}:
            field["facet"] = True
        fields.append(field)
    return {
        "name": collection_name,
        "fields": fields,
        "default_sorting_field": "applied_at_ms",
    }


def validate_lock_timeout(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 60000:
        raise CheckpointStoreError(
            "INDEXING_CHECKPOINT_LOCK_TIMEOUT_MS must be between 1 and 60000"
        )
    return value


def _lock_timeout_from_env() -> int:
    raw_value = os.environ.get(
        "INDEXING_CHECKPOINT_LOCK_TIMEOUT_MS",
        str(DEFAULT_LOCK_TIMEOUT_MS),
    ).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise CheckpointStoreError(
            "INDEXING_CHECKPOINT_LOCK_TIMEOUT_MS must be an integer"
        ) from exc
    return validate_lock_timeout(value)


def _strict_bool_env(name: str, default: bool = False) -> bool:
    raw_value = os.environ.get(name, str(default)).strip().lower()
    if raw_value in TRUE_VALUES:
        return True
    if raw_value in FALSE_VALUES:
        return False
    raise CheckpointStoreError(f"{name} must be a boolean")


def normalize_postgres_url(database_url: str) -> str:
    value = str(database_url).strip()
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value[len("postgres://"):]
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value[len("postgresql://"):]
    return value


def _sqlstate(exc: BaseException) -> Optional[str]:
    original = getattr(exc, "orig", exc)
    return getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)


def translate_postgres_error(exc: BaseException, message: str) -> CheckpointStoreError:
    if isinstance(exc, CheckpointStoreError):
        return exc
    if isinstance(exc, DBAPIError) and _sqlstate(exc) == "55P03":
        return CheckpointLockTimeoutError(
            f"PostgreSQL checkpoint lock timed out after the configured deadline"
        )
    return CheckpointStoreError(message)


def build_checkpoint_store_from_env(
    clients: Mapping[str, typesense.Client],
) -> CheckpointStore:
    backend = os.environ.get("INDEXING_CHECKPOINT_BACKEND", "postgres").strip().lower()
    profile = os.environ.get("DEPLOYMENT_PROFILE", "development").strip().lower()
    lock_timeout_ms = _lock_timeout_from_env()
    if profile not in DEPLOYMENT_PROFILES:
        raise CheckpointStoreError(
            "DEPLOYMENT_PROFILE must be development, test, production, or enterprise"
        )

    if backend == "postgres":
        database_url = os.environ.get("CONTROL_PLANE_DATABASE_URL", "").strip()
        if not database_url:
            raise CheckpointStoreError(
                "INDEXING_CHECKPOINT_BACKEND=postgres requires "
                "CONTROL_PLANE_DATABASE_URL"
            )
        store = PostgresCheckpointStore(
            database_url,
            lock_timeout_ms=lock_timeout_ms,
        )
        store.ensure_ready()
        return store

    if profile in PRODUCTION_PROFILES:
        raise CheckpointStoreError(
            f"Deployment profile '{profile}' requires the postgres checkpoint backend"
        )

    if backend == "memory":
        if not _strict_bool_env("INDEXING_ALLOW_VOLATILE_CHECKPOINTS"):
            raise CheckpointStoreError(
                "The memory checkpoint backend is volatile; set "
                "INDEXING_ALLOW_VOLATILE_CHECKPOINTS=true only for development"
            )
        return InMemoryCheckpointStore(lock_timeout_ms=lock_timeout_ms)

    if backend == "typesense":
        if not _strict_bool_env("INDEXING_ALLOW_TYPESENSE_CHECKPOINTS"):
            raise CheckpointStoreError(
                "The Typesense checkpoint backend has no cross-process fencing; set "
                "INDEXING_ALLOW_TYPESENSE_CHECKPOINTS=true only for development"
            )
        collection_name = os.environ.get(
            "INDEXING_CHECKPOINT_COLLECTION",
            DEFAULT_CHECKPOINT_COLLECTION,
        ).strip()
        store = TypesenseCheckpointStore(
            clients,
            collection_name=collection_name,
            lock_timeout_ms=lock_timeout_ms,
        )
        store.ensure_ready()
        return store

    raise CheckpointStoreError(
        "INDEXING_CHECKPOINT_BACKEND must be postgres, memory, or typesense"
    )
