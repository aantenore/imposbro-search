"""Transactional SQLAlchemy adapter for the PostgreSQL control-plane store."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy import Engine, create_engine, inspect, insert, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from .contracts import (
    AuditEvent,
    AuditIntegrityError,
    ControlPlaneRecord,
    OutboxRecord,
    StateConflictError,
    StateIntegrityError,
    StoreNotReadyError,
    UnsupportedStateSchemaError,
)
from .integrity import (
    ZERO_AUDIT_HASH,
    audit_event_hash,
    build_audit_material,
    canonical_state_digest,
    normalize_json_object,
    utc_datetime,
)
from .schema import (
    ALEMBIC_HEAD_REVISION,
    AUDIT_HEAD_ID,
    CURRENT_STATE_SCHEMA_VERSION,
    STATE_ROW_ID,
    control_plane_audit,
    control_plane_audit_head,
    control_plane_outbox,
    control_plane_state,
)


_REQUIRED_COLUMNS = {
    control_plane_state.name: {column.name for column in control_plane_state.columns},
    control_plane_audit_head.name: {
        column.name for column in control_plane_audit_head.columns
    },
    control_plane_audit.name: {column.name for column in control_plane_audit.columns},
    control_plane_outbox.name: {column.name for column in control_plane_outbox.columns},
}


class PostgresControlPlaneStore:
    """PostgreSQL source of truth with atomic CAS, audit, and outbox writes."""

    def __init__(
        self,
        engine: Engine | str,
        *,
        require_postgres: bool = True,
        expected_migration_revision: str = ALEMBIC_HEAD_REVISION,
        expected_state_schema_version: int = CURRENT_STATE_SCHEMA_VERSION,
    ):
        self.engine = create_engine(engine) if isinstance(engine, str) else engine
        self.require_postgres = require_postgres
        self.expected_migration_revision = expected_migration_revision
        self.expected_state_schema_version = expected_state_schema_version

    def check_ready(self) -> None:
        """Validate backend, migration revision, required columns, and state schema."""
        try:
            dialect = self.engine.dialect.name
            if self.require_postgres and dialect != "postgresql":
                raise StoreNotReadyError(
                    f"Production control-plane store requires PostgreSQL, got '{dialect}'"
                )

            inspector = inspect(self.engine)
            table_names = set(inspector.get_table_names())
            required_tables = set(_REQUIRED_COLUMNS)
            missing_tables = sorted(required_tables - table_names)
            if missing_tables:
                raise StoreNotReadyError(
                    "Control-plane migration is incomplete; missing tables: "
                    + ", ".join(missing_tables)
                )
            if "alembic_version" not in table_names:
                raise StoreNotReadyError("Control-plane Alembic revision is missing")

            for table_name, expected_columns in _REQUIRED_COLUMNS.items():
                actual_columns = {
                    column["name"] for column in inspector.get_columns(table_name)
                }
                missing_columns = sorted(expected_columns - actual_columns)
                if missing_columns:
                    raise StoreNotReadyError(
                        f"Control-plane table '{table_name}' is missing columns: "
                        + ", ".join(missing_columns)
                    )

            with self.engine.connect() as connection:
                migration_revision = connection.scalar(
                    text("SELECT version_num FROM alembic_version")
                )
                if migration_revision != self.expected_migration_revision:
                    raise StoreNotReadyError(
                        "Control-plane migration revision mismatch: "
                        f"expected {self.expected_migration_revision}, "
                        f"actual {migration_revision}"
                    )
                audit_head = connection.execute(
                    select(control_plane_audit_head).where(
                        control_plane_audit_head.c.id == AUDIT_HEAD_ID
                    )
                ).mappings().one_or_none()
                if audit_head is None:
                    raise StoreNotReadyError("Control-plane audit head is missing")
                if len(str(audit_head["event_hash"])) != 64:
                    raise StoreNotReadyError("Control-plane audit head is invalid")

                state_schema_version = connection.scalar(
                    select(control_plane_state.c.schema_version).where(
                        control_plane_state.c.id == STATE_ROW_ID
                    )
                )
                if (
                    state_schema_version is not None
                    and int(state_schema_version)
                    != self.expected_state_schema_version
                ):
                    raise UnsupportedStateSchemaError(
                        self.expected_state_schema_version,
                        int(state_schema_version),
                    )
        except (StoreNotReadyError, UnsupportedStateSchemaError):
            raise
        except Exception as exc:
            raise StoreNotReadyError(
                "Control-plane database connection or schema check failed"
            ) from exc

    def load(self) -> Optional[ControlPlaneRecord]:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(control_plane_state).where(
                    control_plane_state.c.id == STATE_ROW_ID
                )
            ).mappings().one_or_none()
        return self._record_from_row(row) if row is not None else None

    def latest_revision(self) -> Optional[int]:
        with self.engine.connect() as connection:
            revision = connection.scalar(
                select(control_plane_state.c.revision).where(
                    control_plane_state.c.id == STATE_ROW_ID
                )
            )
        return int(revision) if revision is not None else None

    def commit(
        self,
        *,
        expected_revision: int,
        state: Dict[str, Any],
        audit: AuditEvent,
        event_type: str,
        event_payload: Optional[Dict[str, Any]] = None,
        schema_version: int = CURRENT_STATE_SCHEMA_VERSION,
    ) -> ControlPlaneRecord:
        if expected_revision < 0:
            raise ValueError("expected_revision must be zero or greater")
        self._validate_state_schema(schema_version)
        normalized_state = normalize_json_object(state, field_name="state")
        normalized_payload = normalize_json_object(
            event_payload or {},
            field_name="event_payload",
        )
        state_digest = canonical_state_digest(normalized_state)
        now = datetime.now(timezone.utc)
        next_revision = expected_revision + 1

        try:
            with self.engine.begin() as connection:
                self._cas_state(
                    connection,
                    expected_revision=expected_revision,
                    next_revision=next_revision,
                    schema_version=schema_version,
                    state=normalized_state,
                    state_digest=state_digest,
                    timestamp=now,
                )
                self._append_audit_in_transaction(
                    connection,
                    audit,
                    revision=next_revision,
                    timestamp=now,
                )
                self._insert_outbox(
                    connection,
                    revision=next_revision,
                    event_type=event_type,
                    payload={
                        **normalized_payload,
                        "revision": next_revision,
                        "schema_version": schema_version,
                        "state_digest": state_digest,
                    },
                    timestamp=now,
                )
        except StateConflictError:
            raise
        except IntegrityError as exc:
            actual_revision = self.latest_revision() or 0
            if actual_revision != expected_revision:
                raise StateConflictError(expected_revision, actual_revision) from exc
            raise

        return ControlPlaneRecord(
            revision=next_revision,
            schema_version=schema_version,
            state=normalized_state,
            state_digest=state_digest,
            updated_at=now,
        )

    def append_audit(self, audit: AuditEvent, *, revision: Optional[int] = None) -> bool:
        with self.engine.begin() as connection:
            if revision is not None:
                current_revision = connection.scalar(
                    select(control_plane_state.c.revision).where(
                        control_plane_state.c.id == STATE_ROW_ID
                    )
                )
                if current_revision is None or revision > int(current_revision):
                    raise ValueError("audit revision must reference committed state")
            self._append_audit_in_transaction(
                connection,
                audit,
                revision=revision,
                timestamp=datetime.now(timezone.utc),
            )
        return True

    def list_audit(
        self,
        *,
        limit: int = 50,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        statement = select(control_plane_audit)
        if action:
            statement = statement.where(control_plane_audit.c.action == action)
        if resource_type:
            statement = statement.where(
                control_plane_audit.c.resource_type == resource_type
            )
        statement = statement.order_by(control_plane_audit.c.sequence.desc()).limit(limit)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._public_audit_row(row) for row in rows]

    def export_audit(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        statement = (
            select(control_plane_audit)
            .where(control_plane_audit.c.sequence > after_sequence)
            .order_by(control_plane_audit.c.sequence.asc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._public_audit_row(row) for row in rows]

    def list_outbox(
        self,
        *,
        after_revision: int = 0,
        limit: int = 100,
    ) -> List[OutboxRecord]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        statement = (
            select(control_plane_outbox)
            .where(control_plane_outbox.c.revision > after_revision)
            .order_by(control_plane_outbox.c.revision.asc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [
            OutboxRecord(
                event_id=str(row["id"]),
                revision=int(row["revision"]),
                event_type=str(row["event_type"]),
                payload=dict(row["payload_json"]),
                created_at=utc_datetime(row["created_at"]),
                published_at=(
                    utc_datetime(row["published_at"])
                    if row["published_at"] is not None
                    else None
                ),
                last_error=row["last_error"],
                publish_attempts=int(row["publish_attempts"]),
            )
            for row in rows
        ]

    def list_unpublished_outbox(self, *, limit: int = 100) -> List[OutboxRecord]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        statement = (
            select(control_plane_outbox)
            .where(control_plane_outbox.c.published_at.is_(None))
            .order_by(control_plane_outbox.c.revision.asc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [
            OutboxRecord(
                event_id=str(row["id"]),
                revision=int(row["revision"]),
                event_type=str(row["event_type"]),
                payload=dict(row["payload_json"]),
                created_at=utc_datetime(row["created_at"]),
                published_at=None,
                last_error=row["last_error"],
                publish_attempts=int(row["publish_attempts"]),
            )
            for row in rows
        ]

    def verify_audit_chain(self) -> None:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(control_plane_audit).order_by(
                    control_plane_audit.c.sequence.asc()
                )
            ).mappings().all()
            head = connection.execute(
                select(control_plane_audit_head).where(
                    control_plane_audit_head.c.id == AUDIT_HEAD_ID
                )
            ).mappings().one_or_none()

        if head is None:
            raise AuditIntegrityError("Audit head is missing")
        previous_hash = ZERO_AUDIT_HASH
        expected_sequence = 1
        for row in rows:
            sequence = int(row["sequence"])
            if sequence != expected_sequence:
                raise AuditIntegrityError(
                    f"Audit sequence gap at {expected_sequence}"
                )
            if row["previous_hash"] != previous_hash:
                raise AuditIntegrityError(
                    f"Audit previous hash mismatch at sequence {sequence}"
                )
            material = build_audit_material(
                event_id=str(row["id"]),
                sequence=sequence,
                revision=(int(row["revision"]) if row["revision"] is not None else None),
                timestamp=row["timestamp"],
                actor=str(row["actor"]),
                action=str(row["action"]),
                resource_type=str(row["resource_type"]),
                resource_id=str(row["resource_id"]),
                status=str(row["status"]),
                request_id=str(row["request_id"]),
                details=dict(row["details_json"]),
                previous_hash=str(row["previous_hash"]),
            )
            calculated_hash = audit_event_hash(material)
            if row["event_hash"] != calculated_hash:
                raise AuditIntegrityError(
                    f"Audit event hash mismatch at sequence {sequence}"
                )
            previous_hash = calculated_hash
            expected_sequence += 1

        if int(head["sequence"]) != len(rows) or head["event_hash"] != previous_hash:
            raise AuditIntegrityError("Audit head does not match the event chain")

    def mark_outbox_published(
        self,
        event_id: str,
        *,
        published_at: Optional[datetime] = None,
    ) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(control_plane_outbox)
                .where(
                    control_plane_outbox.c.id == event_id,
                    control_plane_outbox.c.published_at.is_(None),
                )
                .values(
                    published_at=published_at or datetime.now(timezone.utc),
                    last_error=None,
                    publish_attempts=control_plane_outbox.c.publish_attempts + 1,
                )
            )
        return result.rowcount == 1

    def record_outbox_failure(self, event_id: str, error: str) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(control_plane_outbox)
                .where(
                    control_plane_outbox.c.id == event_id,
                    control_plane_outbox.c.published_at.is_(None),
                )
                .values(
                    last_error=str(error)[:4096],
                    publish_attempts=control_plane_outbox.c.publish_attempts + 1,
                )
            )
        return result.rowcount == 1

    def _validate_state_schema(self, schema_version: int) -> None:
        if schema_version != self.expected_state_schema_version:
            raise UnsupportedStateSchemaError(
                self.expected_state_schema_version,
                schema_version,
            )

    def _record_from_row(self, row: Mapping[str, Any]) -> ControlPlaneRecord:
        schema_version = int(row["schema_version"])
        self._validate_state_schema(schema_version)
        state = normalize_json_object(row["state_json"], field_name="persisted state")
        calculated_digest = canonical_state_digest(state)
        if calculated_digest != row["state_digest"]:
            raise StateIntegrityError(
                f"Control-plane state digest mismatch at revision {row['revision']}"
            )
        return ControlPlaneRecord(
            revision=int(row["revision"]),
            schema_version=schema_version,
            state=state,
            state_digest=str(row["state_digest"]),
            updated_at=utc_datetime(row["updated_at"]),
        )

    def _cas_state(
        self,
        connection: Connection,
        *,
        expected_revision: int,
        next_revision: int,
        schema_version: int,
        state: Dict[str, Any],
        state_digest: str,
        timestamp: datetime,
    ) -> None:
        actual_revision = connection.scalar(
            select(control_plane_state.c.revision).where(
                control_plane_state.c.id == STATE_ROW_ID
            )
        )
        if actual_revision is None:
            if expected_revision != 0:
                raise StateConflictError(expected_revision, 0)
            connection.execute(
                insert(control_plane_state).values(
                    id=STATE_ROW_ID,
                    revision=next_revision,
                    schema_version=schema_version,
                    state_json=state,
                    state_digest=state_digest,
                    updated_at=timestamp,
                )
            )
            return

        result = connection.execute(
            update(control_plane_state)
            .where(
                control_plane_state.c.id == STATE_ROW_ID,
                control_plane_state.c.revision == expected_revision,
            )
            .values(
                revision=next_revision,
                schema_version=schema_version,
                state_json=state,
                state_digest=state_digest,
                updated_at=timestamp,
            )
        )
        if result.rowcount != 1:
            actual_revision = connection.scalar(
                select(control_plane_state.c.revision).where(
                    control_plane_state.c.id == STATE_ROW_ID
                )
            )
            raise StateConflictError(
                expected_revision,
                int(actual_revision) if actual_revision is not None else 0,
            )

    def _append_audit_in_transaction(
        self,
        connection: Connection,
        audit: AuditEvent,
        *,
        revision: Optional[int],
        timestamp: datetime,
    ) -> None:
        head = connection.execute(
            select(control_plane_audit_head)
            .where(control_plane_audit_head.c.id == AUDIT_HEAD_ID)
            .with_for_update()
        ).mappings().one_or_none()
        if head is None:
            raise StoreNotReadyError("Control-plane audit head is missing")

        sequence = int(head["sequence"]) + 1
        event_id = str(uuid.uuid4())
        material = build_audit_material(
            event_id=event_id,
            sequence=sequence,
            revision=revision,
            timestamp=timestamp,
            actor=audit.actor,
            action=audit.action,
            resource_type=audit.resource_type,
            resource_id=audit.resource_id,
            status=audit.status,
            request_id=audit.request_id,
            details=audit.details,
            previous_hash=str(head["event_hash"]),
        )
        event_hash = audit_event_hash(material)
        connection.execute(
            insert(control_plane_audit).values(
                id=event_id,
                sequence=sequence,
                revision=revision,
                timestamp=timestamp,
                actor=audit.actor,
                action=audit.action,
                resource_type=audit.resource_type,
                resource_id=audit.resource_id,
                status=audit.status,
                request_id=audit.request_id,
                details_json=material["details"],
                previous_hash=material["previous_hash"],
                event_hash=event_hash,
            )
        )
        result = connection.execute(
            update(control_plane_audit_head)
            .where(
                control_plane_audit_head.c.id == AUDIT_HEAD_ID,
                control_plane_audit_head.c.sequence == head["sequence"],
                control_plane_audit_head.c.event_hash == head["event_hash"],
            )
            .values(sequence=sequence, event_hash=event_hash)
        )
        if result.rowcount != 1:
            raise AuditIntegrityError("Audit head changed during append")

    def _insert_outbox(
        self,
        connection: Connection,
        *,
        revision: int,
        event_type: str,
        payload: Dict[str, Any],
        timestamp: datetime,
    ) -> None:
        connection.execute(
            insert(control_plane_outbox).values(
                id=str(uuid.uuid4()),
                revision=revision,
                event_type=event_type,
                payload_json=payload,
                created_at=timestamp,
                published_at=None,
                last_error=None,
                publish_attempts=0,
            )
        )

    @staticmethod
    def _public_audit_row(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(row["id"]),
            "sequence": int(row["sequence"]),
            "revision": int(row["revision"]) if row["revision"] is not None else None,
            "timestamp": utc_datetime(row["timestamp"]),
            "actor": str(row["actor"]),
            "action": str(row["action"]),
            "resource_type": str(row["resource_type"]),
            "resource_id": str(row["resource_id"]),
            "status": str(row["status"]),
            "request_id": str(row["request_id"]),
            "details": dict(row["details_json"]),
            "previous_hash": str(row["previous_hash"]),
            "event_hash": str(row["event_hash"]),
        }
