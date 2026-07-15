"""Transactional control-plane persistence contracts and adapters."""

from .audit_delivery import (
    AuditDeliveryAck,
    AuditDeliveryBatch,
    AuditDeliveryCheckpoint,
    AuditDeliveryCoordinator,
    AuditDeliveryError,
    AuditDeliveryIntegrityError,
    AuditDeliveryRepository,
    AuditSink,
    AuditSinkError,
)
from .audit_http_sink import HTTPAuditSink
from .contracts import (
    AuditEvent,
    AuditIntegrityError,
    ControlPlaneRecord,
    ControlPlaneStore,
    CURRENT_STATE_SCHEMA_VERSION,
    OutboxRecord,
    StateConflictError,
    StateIntegrityError,
    StoreNotReadyError,
    UnsupportedStateSchemaError,
)
from .in_memory_store import InMemoryControlPlaneStore
from .legacy_reader import LegacyStateError, LegacyStateSnapshot, TypesenseLegacyStateReader
from .postgres_store import PostgresControlPlaneStore
from .deletion_ledger import (
    DeletionLedgerError,
    DeletionLedgerRepository,
    DeletionReconciler,
    DeletionRecord,
    DeletionTarget,
    StaleTombstoneError,
    SuppressionReceipt,
    TombstoneRegistration,
    register_tombstone_in_transaction,
)

__all__ = [
    "AuditEvent",
    "AuditDeliveryAck",
    "AuditDeliveryBatch",
    "AuditDeliveryCheckpoint",
    "AuditDeliveryCoordinator",
    "AuditDeliveryError",
    "AuditDeliveryIntegrityError",
    "AuditDeliveryRepository",
    "AuditIntegrityError",
    "AuditSink",
    "AuditSinkError",
    "HTTPAuditSink",
    "ControlPlaneRecord",
    "ControlPlaneStore",
    "CURRENT_STATE_SCHEMA_VERSION",
    "DeletionLedgerError",
    "DeletionLedgerRepository",
    "DeletionReconciler",
    "DeletionRecord",
    "DeletionTarget",
    "InMemoryControlPlaneStore",
    "LegacyStateError",
    "LegacyStateSnapshot",
    "OutboxRecord",
    "PostgresControlPlaneStore",
    "StateConflictError",
    "StateIntegrityError",
    "StaleTombstoneError",
    "SuppressionReceipt",
    "StoreNotReadyError",
    "TypesenseLegacyStateReader",
    "TombstoneRegistration",
    "register_tombstone_in_transaction",
    "UnsupportedStateSchemaError",
]
