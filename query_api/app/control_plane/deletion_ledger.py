"""Durable tombstone and restore-suppression contract.

The ledger is provider-neutral: target adapters acknowledge only a bounded
target ID, checkpoint sequence, and receipt digest.  Document payloads never
enter the ledger, and callers must apply every returned suppression before a
restored data store can serve traffic.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Protocol, Tuple

from sqlalchemy import Engine, create_engine, delete, insert, inspect, select, update

from .integrity import utc_datetime
from .schema import deletion_ledger, deletion_ledger_targets


HASH = re.compile(r"^[a-f0-9]{64}$")
TARGET = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class DeletionLedgerError(RuntimeError):
    """Base deletion ledger contract failure."""


class StaleTombstoneError(DeletionLedgerError):
    """Raised when replay tries to replace a newer tombstone."""


@dataclass(frozen=True)
class TombstoneRegistration:
    identity_hash: str
    tenant_id: str
    collection: str
    document_id: str
    tombstone_sequence: int
    document_version: int
    event_id: str
    event_digest: str
    routing_revision: int
    target_ids: Tuple[str, ...]
    requested_at: datetime
    rollout_id: str = ""
    retention_until: Optional[datetime] = None


@dataclass(frozen=True)
class DeletionRecord:
    identity_hash: str
    tenant_id: str
    collection: str
    document_id: str
    tombstone_sequence: int
    document_version: int
    event_id: str
    event_digest: str
    routing_revision: int
    rollout_id: str
    requested_at: datetime
    retention_until: Optional[datetime]
    legal_hold: bool
    converged_at: Optional[datetime]


@dataclass(frozen=True)
class SuppressionReceipt:
    identity_hash: str
    target_id: str
    tombstone_sequence: int
    checkpoint_sequence: int
    status: str
    receipt_id: str


class DeletionTarget(Protocol):
    """Provider adapter used while a restore remains isolated from traffic."""

    def suppress(
        self,
        record: DeletionRecord,
        target_id: str,
    ) -> SuppressionReceipt:
        """Delete or prove absence and return a receipt for the exact tombstone."""


class DeletionLedgerRepository:
    def __init__(self, engine: Engine | str):
        self.engine = create_engine(engine) if isinstance(engine, str) else engine

    def check_ready(self) -> None:
        inspector = inspect(self.engine)
        required = {
            deletion_ledger.name: {column.name for column in deletion_ledger.columns},
            deletion_ledger_targets.name: {
                column.name for column in deletion_ledger_targets.columns
            },
        }
        tables = set(inspector.get_table_names())
        if set(required) - tables:
            raise DeletionLedgerError("Deletion ledger migration is incomplete")
        for table_name, columns in required.items():
            actual = {item["name"] for item in inspector.get_columns(table_name)}
            if columns - actual:
                raise DeletionLedgerError("Deletion ledger schema is incomplete")

    def register(self, registration: TombstoneRegistration) -> DeletionRecord:
        _validate_registration(registration)
        timestamp = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            row = register_tombstone_in_transaction(
                connection, registration, now=timestamp
            )
        return _record(row)

    def record_target(
        self,
        identity_hash: str,
        target_id: str,
        *,
        tombstone_sequence: int,
        checkpoint_sequence: int,
        status: str,
        receipt: str,
        verified_at: Optional[datetime] = None,
    ) -> DeletionRecord:
        _hash(identity_hash, "identity_hash")
        if not TARGET.fullmatch(target_id):
            raise ValueError("target_id is invalid")
        if status not in {"applied", "absent"}:
            raise ValueError("target status must be applied or absent")
        if checkpoint_sequence < tombstone_sequence:
            raise StaleTombstoneError("Target checkpoint is older than tombstone")
        if not receipt or len(receipt) > 2048:
            raise ValueError("receipt must be a bounded non-empty provider identifier")
        receipt_hash = hashlib.sha256(receipt.encode("utf-8")).hexdigest()
        timestamp = verified_at or datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            ledger_row = connection.execute(
                select(deletion_ledger)
                .where(deletion_ledger.c.identity_hash == identity_hash)
                .with_for_update()
            ).mappings().one_or_none()
            if ledger_row is None:
                raise DeletionLedgerError("Deletion identity is not registered")
            if int(ledger_row["tombstone_sequence"]) != tombstone_sequence:
                raise StaleTombstoneError("Target receipt does not match current tombstone")
            result = connection.execute(
                update(deletion_ledger_targets)
                .where(
                    deletion_ledger_targets.c.identity_hash == identity_hash,
                    deletion_ledger_targets.c.target_id == target_id,
                    deletion_ledger_targets.c.tombstone_sequence == tombstone_sequence,
                )
                .values(
                    status=status,
                    checkpoint_sequence=checkpoint_sequence,
                    receipt_hash=receipt_hash,
                    verified_at=timestamp,
                    updated_at=timestamp,
                )
            )
            if result.rowcount != 1:
                raise DeletionLedgerError("Target is not part of current deletion")
            pending = connection.scalar(
                select(deletion_ledger_targets.c.target_id)
                .where(
                    deletion_ledger_targets.c.identity_hash == identity_hash,
                    deletion_ledger_targets.c.status == "pending",
                )
                .limit(1)
            )
            connection.execute(
                update(deletion_ledger)
                .where(deletion_ledger.c.identity_hash == identity_hash)
                .values(
                    converged_at=timestamp if pending is None else None,
                    last_verified_at=timestamp,
                    updated_at=timestamp,
                )
            )
            row = connection.execute(
                select(deletion_ledger).where(
                    deletion_ledger.c.identity_hash == identity_hash
                )
            ).mappings().one()
        return _record(row)

    def should_suppress(self, identity_hash: str, candidate_sequence: int) -> bool:
        _hash(identity_hash, "identity_hash")
        if candidate_sequence < 1:
            raise ValueError("candidate_sequence must be positive")
        with self.engine.connect() as connection:
            tombstone_sequence = connection.scalar(
                select(deletion_ledger.c.tombstone_sequence).where(
                    deletion_ledger.c.identity_hash == identity_hash
                )
            )
        return tombstone_sequence is not None and candidate_sequence <= int(tombstone_sequence)

    def list_restore_suppressions(
        self,
        *,
        after_identity_hash: str = "",
        limit: int = 1000,
    ) -> List[DeletionRecord]:
        if after_identity_hash:
            _hash(after_identity_hash, "after_identity_hash")
        if limit < 1 or limit > 5000:
            raise ValueError("limit must be between 1 and 5000")
        statement = select(deletion_ledger)
        if after_identity_hash:
            statement = statement.where(
                deletion_ledger.c.identity_hash > after_identity_hash
            )
        statement = statement.order_by(deletion_ledger.c.identity_hash).limit(limit)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [_record(row) for row in rows]

    def set_legal_hold(
        self,
        identity_hash: str,
        *,
        enabled: bool,
        reference: str,
    ) -> None:
        _hash(identity_hash, "identity_hash")
        if not reference.strip():
            raise ValueError("legal hold changes require a reference")
        reference_hash = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(deletion_ledger)
                .where(deletion_ledger.c.identity_hash == identity_hash)
                .values(
                    legal_hold=enabled,
                    legal_hold_reference_hash=reference_hash,
                    updated_at=datetime.now(timezone.utc),
                )
            )
        if result.rowcount != 1:
            raise DeletionLedgerError("Deletion identity is not registered")

    def pending_targets(self, identity_hash: str) -> Tuple[str, ...]:
        _hash(identity_hash, "identity_hash")
        with self.engine.connect() as connection:
            rows = connection.scalars(
                select(deletion_ledger_targets.c.target_id)
                .where(
                    deletion_ledger_targets.c.identity_hash == identity_hash,
                    deletion_ledger_targets.c.status == "pending",
                )
                .order_by(deletion_ledger_targets.c.target_id)
            ).all()
        return tuple(str(value) for value in rows)

    def target_ids(self, identity_hash: str) -> Tuple[str, ...]:
        _hash(identity_hash, "identity_hash")
        with self.engine.connect() as connection:
            rows = connection.scalars(
                select(deletion_ledger_targets.c.target_id)
                .where(deletion_ledger_targets.c.identity_hash == identity_hash)
                .order_by(deletion_ledger_targets.c.target_id)
            ).all()
        return tuple(str(value) for value in rows)


class DeletionReconciler:
    """Apply all restore suppressions before a restored target can serve traffic."""

    def __init__(self, repository: DeletionLedgerRepository, target: DeletionTarget):
        self.repository = repository
        self.target = target

    def replay_allowed(self, identity_hash: str, sequence: int) -> bool:
        """Reject an old restored/replayed upsert at or below the tombstone."""
        return not self.repository.should_suppress(identity_hash, sequence)

    def reconcile_once(self, *, limit: int = 1000) -> int:
        """Reconcile one bounded page; caller must keep traffic closed until zero."""
        reconciled = 0
        for record in self.repository.list_restore_suppressions(limit=limit):
            for target_id in self.repository.pending_targets(record.identity_hash):
                receipt = self.target.suppress(record, target_id)
                if (
                    receipt.identity_hash != record.identity_hash
                    or receipt.target_id != target_id
                    or receipt.tombstone_sequence != record.tombstone_sequence
                ):
                    raise DeletionLedgerError(
                        "Suppression provider acknowledged a different deletion boundary"
                    )
                self.repository.record_target(
                    record.identity_hash,
                    target_id,
                    tombstone_sequence=record.tombstone_sequence,
                    checkpoint_sequence=receipt.checkpoint_sequence,
                    status=receipt.status,
                    receipt=receipt.receipt_id,
                )
                reconciled += 1
        return reconciled

    def reconcile_restore(self, *, limit: int = 1000) -> int:
        """Re-verify every target after restore, even if an older receipt exists."""
        reconciled = 0
        for record in self.repository.list_restore_suppressions(limit=limit):
            for target_id in self.repository.target_ids(record.identity_hash):
                receipt = self.target.suppress(record, target_id)
                if (
                    receipt.identity_hash != record.identity_hash
                    or receipt.target_id != target_id
                    or receipt.tombstone_sequence != record.tombstone_sequence
                ):
                    raise DeletionLedgerError(
                        "Suppression provider acknowledged a different deletion boundary"
                    )
                self.repository.record_target(
                    record.identity_hash,
                    target_id,
                    tombstone_sequence=record.tombstone_sequence,
                    checkpoint_sequence=receipt.checkpoint_sequence,
                    status=receipt.status,
                    receipt=receipt.receipt_id,
                )
                reconciled += 1
        return reconciled


def register_tombstone_in_transaction(
    connection,
    registration: TombstoneRegistration,
    *,
    now: Optional[datetime] = None,
):
    """Register a tombstone inside the caller's event-outbox transaction."""
    _validate_registration(registration)
    targets = tuple(dict.fromkeys(registration.target_ids))
    timestamp = now or datetime.now(timezone.utc)
    current = connection.execute(
        select(deletion_ledger)
        .where(deletion_ledger.c.identity_hash == registration.identity_hash)
        .with_for_update()
    ).mappings().one_or_none()
    if current is not None:
        current_sequence = int(current["tombstone_sequence"])
        if registration.tombstone_sequence < current_sequence:
            raise StaleTombstoneError("A newer deletion tombstone already exists")
        if registration.tombstone_sequence == current_sequence:
            if (
                str(current["event_id"]) != registration.event_id
                or str(current["event_digest"]) != registration.event_digest
            ):
                raise DeletionLedgerError("Tombstone sequence conflicts with existing event")
            return current
        connection.execute(
            update(deletion_ledger)
            .where(deletion_ledger.c.identity_hash == registration.identity_hash)
            .values(
                tenant_id=registration.tenant_id,
                collection=registration.collection,
                document_id=registration.document_id,
                tombstone_sequence=registration.tombstone_sequence,
                document_version=registration.document_version,
                event_id=registration.event_id,
                event_digest=registration.event_digest,
                routing_revision=registration.routing_revision,
                rollout_id=registration.rollout_id,
                requested_at=registration.requested_at,
                retention_until=_later_retention(
                    current["retention_until"], registration.retention_until
                ),
                converged_at=None,
                last_verified_at=None,
                updated_at=timestamp,
            )
        )
        connection.execute(
            delete(deletion_ledger_targets).where(
                deletion_ledger_targets.c.identity_hash == registration.identity_hash
            )
        )
    else:
        connection.execute(
            insert(deletion_ledger).values(
                identity_hash=registration.identity_hash,
                tenant_id=registration.tenant_id,
                collection=registration.collection,
                document_id=registration.document_id,
                tombstone_sequence=registration.tombstone_sequence,
                document_version=registration.document_version,
                event_id=registration.event_id,
                event_digest=registration.event_digest,
                routing_revision=registration.routing_revision,
                rollout_id=registration.rollout_id,
                requested_at=registration.requested_at,
                retention_until=registration.retention_until,
                legal_hold=False,
                legal_hold_reference_hash=None,
                converged_at=None,
                last_verified_at=None,
                updated_at=timestamp,
            )
        )
    for target_id in targets:
        connection.execute(
            insert(deletion_ledger_targets).values(
                identity_hash=registration.identity_hash,
                target_id=target_id,
                tombstone_sequence=registration.tombstone_sequence,
                status="pending",
                checkpoint_sequence=None,
                receipt_hash=None,
                verified_at=None,
                updated_at=timestamp,
            )
        )
    return connection.execute(
        select(deletion_ledger).where(
            deletion_ledger.c.identity_hash == registration.identity_hash
        )
    ).mappings().one()


def _validate_registration(value: TombstoneRegistration) -> None:
    _hash(value.identity_hash, "identity_hash")
    _hash(value.event_digest, "event_digest")
    for name, text, maximum in (
        ("tenant_id", value.tenant_id, 256),
        ("collection", value.collection, 128),
        ("document_id", value.document_id, 256),
        ("event_id", value.event_id, 128),
    ):
        if not text or len(text) > maximum:
            raise ValueError(f"{name} is invalid")
    if len(value.rollout_id) > 128:
        raise ValueError("rollout_id is invalid")
    if value.tombstone_sequence < 1 or value.document_version < 1:
        raise ValueError("tombstone sequence/version must be positive")
    if value.routing_revision < 1:
        raise ValueError("routing_revision must be positive")
    targets = tuple(dict.fromkeys(value.target_ids))
    if not targets or any(not TARGET.fullmatch(target) for target in targets):
        raise ValueError("at least one bounded target_id is required")
    if value.retention_until and value.retention_until <= value.requested_at:
        raise ValueError("retention must extend beyond the tombstone request")


def _later_retention(current, requested):
    if current is None or requested is None:
        return None
    current_value = utc_datetime(current)
    requested_value = utc_datetime(requested)
    return max(current_value, requested_value)


def _hash(value: str, name: str) -> str:
    if not HASH.fullmatch(str(value)):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return str(value)


def _record(row) -> DeletionRecord:
    return DeletionRecord(
        identity_hash=str(row["identity_hash"]),
        tenant_id=str(row["tenant_id"]),
        collection=str(row["collection"]),
        document_id=str(row["document_id"]),
        tombstone_sequence=int(row["tombstone_sequence"]),
        document_version=int(row["document_version"]),
        event_id=str(row["event_id"]),
        event_digest=str(row["event_digest"]),
        routing_revision=int(row["routing_revision"]),
        rollout_id=str(row["rollout_id"]),
        requested_at=utc_datetime(row["requested_at"]),
        retention_until=utc_datetime(row["retention_until"]) if row["retention_until"] else None,
        legal_hold=bool(row["legal_hold"]),
        converged_at=utc_datetime(row["converged_at"]) if row["converged_at"] else None,
    )
