"""Provider-neutral, durable delivery of the tamper-evident audit chain.

The database records only a bounded destination identifier and a redacted error
code.  Delivery providers remain adapters and must acknowledge the exact chain
boundary they durably accepted.  This contract does not claim a destination is
WORM, immutable, or independently controlled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Protocol, Tuple

from sqlalchemy import Engine, case, create_engine, func, insert, inspect, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .integrity import (
    ZERO_AUDIT_HASH,
    audit_event_hash,
    build_audit_material,
    utc_datetime,
)
from .schema import (
    audit_delivery_checkpoints,
    control_plane_audit,
    control_plane_audit_head,
)


DESTINATION_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class AuditDeliveryError(RuntimeError):
    """Base failure for delivery contract violations."""


class AuditDeliveryIntegrityError(AuditDeliveryError):
    """Raised when the chain or checkpoint cannot be advanced safely."""


class AuditSinkError(AuditDeliveryError):
    """A provider failure carrying a pre-redacted, low-cardinality code."""

    def __init__(self, code: str = "sink_delivery_failed"):
        self.code = validate_error_code(code)
        super().__init__(self.code)


@dataclass(frozen=True)
class AuditDeliveryCheckpoint:
    destination_id: str
    last_sequence: int
    last_event_hash: str
    last_success_at: Optional[datetime]
    failure_attempts: int
    last_error_code: Optional[str]
    next_retry_at: Optional[datetime]


@dataclass(frozen=True)
class AuditDeliveryBatch:
    destination_id: str
    after_sequence: int
    previous_hash: str
    records: Tuple[Dict[str, Any], ...]
    head_sequence: int
    head_hash: str
    deferred_until: Optional[datetime] = None

    @property
    def last_sequence(self) -> int:
        return int(self.records[-1]["sequence"]) if self.records else self.after_sequence

    @property
    def last_event_hash(self) -> str:
        return str(self.records[-1]["event_hash"]) if self.records else self.previous_hash


@dataclass(frozen=True)
class AuditDeliveryAck:
    destination_id: str
    last_sequence: int
    last_event_hash: str


@dataclass(frozen=True)
class AuditDeliveryStats:
    destinations: int
    head_sequence: int
    max_backlog: int
    failed_destinations: int


class AuditSink(Protocol):
    """Adapter port for an HTTP, object-store, SIEM, or other audit sink."""

    def deliver(self, batch: AuditDeliveryBatch) -> AuditDeliveryAck:
        """Durably accept a batch or raise AuditSinkError with a safe code."""


class AuditDeliveryRepository:
    """PostgreSQL-backed delivery cursor independent from any sink vendor."""

    def __init__(self, engine: Engine | str):
        self.engine = create_engine(engine) if isinstance(engine, str) else engine

    def check_ready(self) -> None:
        inspector = inspect(self.engine)
        required = {
            audit_delivery_checkpoints.name: {
                column.name for column in audit_delivery_checkpoints.columns
            },
            control_plane_audit.name: {column.name for column in control_plane_audit.columns},
            control_plane_audit_head.name: {
                column.name for column in control_plane_audit_head.columns
            },
        }
        tables = set(inspector.get_table_names())
        missing = sorted(set(required) - tables)
        if missing:
            raise AuditDeliveryError("Audit delivery migration is incomplete")
        for table_name, columns in required.items():
            actual = {item["name"] for item in inspector.get_columns(table_name)}
            if columns - actual:
                raise AuditDeliveryError("Audit delivery schema is incomplete")

    def ensure_destination(
        self,
        destination_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> AuditDeliveryCheckpoint:
        destination_id = validate_destination_id(destination_id)
        timestamp = now or datetime.now(timezone.utc)
        values = {
            "destination_id": destination_id,
            "last_sequence": 0,
            "last_event_hash": ZERO_AUDIT_HASH,
            "last_success_at": None,
            "failure_attempts": 0,
            "last_failure_at": None,
            "last_error_code": None,
            "next_retry_at": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                statement = postgres_insert(audit_delivery_checkpoints).values(**values)
                statement = statement.on_conflict_do_nothing(
                    index_elements=[audit_delivery_checkpoints.c.destination_id]
                )
            elif connection.dialect.name == "sqlite":
                statement = sqlite_insert(audit_delivery_checkpoints).values(**values)
                statement = statement.on_conflict_do_nothing(
                    index_elements=[audit_delivery_checkpoints.c.destination_id]
                )
            else:
                statement = insert(audit_delivery_checkpoints).values(**values)
            connection.execute(statement)
        return self.get_checkpoint(destination_id)

    def get_checkpoint(self, destination_id: str) -> AuditDeliveryCheckpoint:
        destination_id = validate_destination_id(destination_id)
        with self.engine.connect() as connection:
            row = connection.execute(
                select(audit_delivery_checkpoints).where(
                    audit_delivery_checkpoints.c.destination_id == destination_id
                )
            ).mappings().one_or_none()
        if row is None:
            raise AuditDeliveryError("Audit destination is not registered")
        return _checkpoint(row)

    def next_batch(
        self,
        destination_id: str,
        *,
        limit: int = 500,
        now: Optional[datetime] = None,
    ) -> AuditDeliveryBatch:
        if limit < 1 or limit > 5000:
            raise ValueError("limit must be between 1 and 5000")
        checkpoint = self.get_checkpoint(destination_id)
        timestamp = now or datetime.now(timezone.utc)
        with self.engine.connect() as connection:
            head = connection.execute(
                select(control_plane_audit_head)
            ).mappings().one_or_none()
            if head is None:
                raise AuditDeliveryIntegrityError("Audit head is missing")
            head_sequence = int(head["sequence"])
            head_hash = str(head["event_hash"])
            self._verify_checkpoint_boundary(connection, checkpoint)
            if checkpoint.next_retry_at and checkpoint.next_retry_at > timestamp:
                return AuditDeliveryBatch(
                    destination_id=checkpoint.destination_id,
                    after_sequence=checkpoint.last_sequence,
                    previous_hash=checkpoint.last_event_hash,
                    records=(),
                    head_sequence=head_sequence,
                    head_hash=head_hash,
                    deferred_until=checkpoint.next_retry_at,
                )
            rows = connection.execute(
                select(control_plane_audit)
                .where(control_plane_audit.c.sequence > checkpoint.last_sequence)
                .order_by(control_plane_audit.c.sequence.asc())
                .limit(limit)
            ).mappings().all()

        records = tuple(_public_audit_row(row) for row in rows)
        if head_sequence > checkpoint.last_sequence and not records:
            raise AuditDeliveryIntegrityError("Audit head contains an unresolvable sequence gap")
        _verify_page(checkpoint, records)
        if records and int(records[-1]["sequence"]) == head_sequence:
            if str(records[-1]["event_hash"]) != head_hash:
                raise AuditDeliveryIntegrityError("Audit head hash does not match page")
        if head_sequence < checkpoint.last_sequence:
            raise AuditDeliveryIntegrityError("Audit head is behind delivery checkpoint")
        return AuditDeliveryBatch(
            destination_id=checkpoint.destination_id,
            after_sequence=checkpoint.last_sequence,
            previous_hash=checkpoint.last_event_hash,
            records=records,
            head_sequence=head_sequence,
            head_hash=head_hash,
        )

    def advance(self, ack: AuditDeliveryAck, *, now: Optional[datetime] = None) -> bool:
        destination_id = validate_destination_id(ack.destination_id)
        if ack.last_sequence < 1 or not re.fullmatch(r"[a-f0-9]{64}", ack.last_event_hash):
            raise AuditDeliveryIntegrityError("Sink acknowledgement is malformed")
        timestamp = now or datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            checkpoint = connection.execute(
                select(audit_delivery_checkpoints)
                .where(audit_delivery_checkpoints.c.destination_id == destination_id)
                .with_for_update()
            ).mappings().one_or_none()
            if checkpoint is None:
                raise AuditDeliveryError("Audit destination is not registered")
            current = int(checkpoint["last_sequence"])
            if ack.last_sequence <= current:
                return ack.last_sequence == current and ack.last_event_hash == checkpoint["last_event_hash"]
            row = connection.execute(
                select(control_plane_audit).where(
                    control_plane_audit.c.sequence == ack.last_sequence
                )
            ).mappings().one_or_none()
            if row is None or str(row["event_hash"]) != ack.last_event_hash:
                raise AuditDeliveryIntegrityError("Sink acknowledgement is not an audit boundary")
            result = connection.execute(
                update(audit_delivery_checkpoints)
                .where(
                    audit_delivery_checkpoints.c.destination_id == destination_id,
                    audit_delivery_checkpoints.c.last_sequence == current,
                )
                .values(
                    last_sequence=ack.last_sequence,
                    last_event_hash=ack.last_event_hash,
                    last_success_at=timestamp,
                    failure_attempts=0,
                    last_failure_at=None,
                    last_error_code=None,
                    next_retry_at=None,
                    updated_at=timestamp,
                )
            )
        return result.rowcount == 1

    def record_failure(
        self,
        destination_id: str,
        error_code: str,
        *,
        retry_at: datetime,
        now: Optional[datetime] = None,
    ) -> None:
        destination_id = validate_destination_id(destination_id)
        error_code = validate_error_code(error_code)
        timestamp = now or datetime.now(timezone.utc)
        if retry_at <= timestamp:
            raise ValueError("retry_at must be in the future")
        with self.engine.begin() as connection:
            result = connection.execute(
                update(audit_delivery_checkpoints)
                .where(audit_delivery_checkpoints.c.destination_id == destination_id)
                .values(
                    failure_attempts=audit_delivery_checkpoints.c.failure_attempts + 1,
                    last_failure_at=timestamp,
                    last_error_code=error_code,
                    next_retry_at=retry_at,
                    updated_at=timestamp,
                )
            )
        if result.rowcount != 1:
            raise AuditDeliveryError("Audit destination is not registered")

    def stats(self) -> AuditDeliveryStats:
        with self.engine.connect() as connection:
            head = int(connection.scalar(select(control_plane_audit_head.c.sequence)) or 0)
            rows = connection.execute(
                select(
                    func.count().label("destinations"),
                    func.min(audit_delivery_checkpoints.c.last_sequence).label("minimum"),
                    func.sum(
                        case(
                            (audit_delivery_checkpoints.c.failure_attempts > 0, 1),
                            else_=0,
                        )
                    ).label("failed"),
                )
            ).mappings().one()
        destinations = int(rows["destinations"] or 0)
        minimum = int(rows["minimum"] or head)
        return AuditDeliveryStats(
            destinations=destinations,
            head_sequence=head,
            max_backlog=max(head - minimum, 0) if destinations else 0,
            failed_destinations=int(rows["failed"] or 0),
        )

    @staticmethod
    def _verify_checkpoint_boundary(connection, checkpoint: AuditDeliveryCheckpoint) -> None:
        if checkpoint.last_sequence == 0:
            if checkpoint.last_event_hash != ZERO_AUDIT_HASH:
                raise AuditDeliveryIntegrityError("Initial checkpoint hash is invalid")
            return
        row = connection.execute(
            select(control_plane_audit.c.event_hash).where(
                control_plane_audit.c.sequence == checkpoint.last_sequence
            )
        ).one_or_none()
        if row is None or str(row[0]) != checkpoint.last_event_hash:
            raise AuditDeliveryIntegrityError("Delivery checkpoint is not in audit chain")


class AuditDeliveryCoordinator:
    """One bounded delivery attempt with exponential retry metadata."""

    def __init__(
        self,
        repository: AuditDeliveryRepository,
        sink: AuditSink,
        *,
        base_retry_seconds: int = 5,
        max_retry_seconds: int = 300,
    ):
        if base_retry_seconds < 1 or max_retry_seconds < base_retry_seconds:
            raise ValueError("invalid audit delivery retry budget")
        self.repository = repository
        self.sink = sink
        self.base_retry_seconds = base_retry_seconds
        self.max_retry_seconds = max_retry_seconds

    def deliver_once(
        self,
        destination_id: str,
        *,
        limit: int = 500,
        now: Optional[datetime] = None,
    ) -> AuditDeliveryCheckpoint:
        timestamp = now or datetime.now(timezone.utc)
        self.repository.ensure_destination(destination_id, now=timestamp)
        batch = self.repository.next_batch(destination_id, limit=limit, now=timestamp)
        if not batch.records or batch.deferred_until:
            return self.repository.get_checkpoint(destination_id)
        try:
            ack = self.sink.deliver(batch)
            if (
                ack.destination_id != batch.destination_id
                or ack.last_sequence != batch.last_sequence
                or ack.last_event_hash != batch.last_event_hash
            ):
                raise AuditSinkError("sink_ack_mismatch")
            self.repository.advance(ack, now=timestamp)
        except AuditSinkError as exc:
            checkpoint = self.repository.get_checkpoint(destination_id)
            delay = min(
                self.max_retry_seconds,
                self.base_retry_seconds * (2 ** min(checkpoint.failure_attempts, 10)),
            )
            self.repository.record_failure(
                destination_id,
                exc.code,
                retry_at=timestamp + timedelta(seconds=delay),
                now=timestamp,
            )
        except Exception:
            checkpoint = self.repository.get_checkpoint(destination_id)
            delay = min(
                self.max_retry_seconds,
                self.base_retry_seconds * (2 ** min(checkpoint.failure_attempts, 10)),
            )
            self.repository.record_failure(
                destination_id,
                "sink_unexpected_error",
                retry_at=timestamp + timedelta(seconds=delay),
                now=timestamp,
            )
        return self.repository.get_checkpoint(destination_id)


def validate_destination_id(value: str) -> str:
    candidate = str(value).strip()
    if not DESTINATION_ID.fullmatch(candidate):
        raise ValueError("destination_id must be a bounded lowercase identifier")
    return candidate


def validate_error_code(value: str) -> str:
    candidate = str(value).strip()
    if not ERROR_CODE.fullmatch(candidate):
        raise ValueError("audit delivery errors must use a redacted stable code")
    return candidate


def _verify_page(
    checkpoint: AuditDeliveryCheckpoint,
    records: Tuple[Dict[str, Any], ...],
) -> None:
    previous_hash = checkpoint.last_event_hash
    expected_sequence = checkpoint.last_sequence + 1
    for record in records:
        if int(record["sequence"]) != expected_sequence:
            raise AuditDeliveryIntegrityError("Audit delivery page has a sequence gap")
        if record["previous_hash"] != previous_hash:
            raise AuditDeliveryIntegrityError("Audit delivery page hash link is invalid")
        material = build_audit_material(
            event_id=record["id"],
            sequence=record["sequence"],
            revision=record["revision"],
            timestamp=record["timestamp"],
            actor=record["actor"],
            action=record["action"],
            resource_type=record["resource_type"],
            resource_id=record["resource_id"],
            status=record["status"],
            request_id=record["request_id"],
            details=record["details"],
            previous_hash=record["previous_hash"],
        )
        if audit_event_hash(material) != record["event_hash"]:
            raise AuditDeliveryIntegrityError("Audit delivery event hash is invalid")
        previous_hash = record["event_hash"]
        expected_sequence += 1


def _checkpoint(row) -> AuditDeliveryCheckpoint:
    return AuditDeliveryCheckpoint(
        destination_id=str(row["destination_id"]),
        last_sequence=int(row["last_sequence"]),
        last_event_hash=str(row["last_event_hash"]),
        last_success_at=utc_datetime(row["last_success_at"]) if row["last_success_at"] else None,
        failure_attempts=int(row["failure_attempts"]),
        last_error_code=row["last_error_code"],
        next_retry_at=utc_datetime(row["next_retry_at"]) if row["next_retry_at"] else None,
    )


def _public_audit_row(row) -> Dict[str, Any]:
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
