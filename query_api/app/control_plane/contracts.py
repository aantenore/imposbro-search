"""Application-level contracts for authoritative control-plane state."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol


CURRENT_STATE_SCHEMA_VERSION = 1


class StateConflictError(RuntimeError):
    """Raised when a stale control-plane revision attempts to commit."""

    def __init__(self, expected_revision: int, actual_revision: Optional[int]):
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            "Control-plane revision conflict: "
            f"expected {expected_revision}, actual {actual_revision}"
        )


class StoreNotReadyError(RuntimeError):
    """Raised when the production store schema has not been migrated."""


class UnsupportedStateSchemaError(RuntimeError):
    """Raised before reading or committing an unsupported state schema."""

    def __init__(self, expected_version: int, actual_version: int):
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            "Unsupported control-plane state schema: "
            f"expected {expected_version}, actual {actual_version}"
        )


class StateIntegrityError(RuntimeError):
    """Raised when persisted state does not match its canonical digest."""


class AuditIntegrityError(RuntimeError):
    """Raised when the append-only audit hash chain does not verify."""


@dataclass(frozen=True)
class AuditEvent:
    """Safe operator event committed with a state transition when applicable."""

    actor: str
    action: str
    resource_type: str
    resource_id: str
    status: str = "success"
    request_id: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ControlPlaneRecord:
    """One immutable committed control-plane revision."""

    revision: int
    schema_version: int
    state: Dict[str, Any]
    state_digest: str
    updated_at: datetime


@dataclass(frozen=True)
class OutboxRecord:
    """Durable change notification written in the state transaction."""

    event_id: str
    revision: int
    event_type: str
    payload: Dict[str, Any]
    created_at: datetime
    published_at: Optional[datetime] = None
    last_error: Optional[str] = None
    publish_attempts: int = 0


class ControlPlaneStore(Protocol):
    """Narrow store port; implementations must preserve atomic commit semantics."""

    def check_ready(self) -> None:
        """Fail when the store schema or connection is not ready."""

    def load(self) -> Optional[ControlPlaneRecord]:
        """Load the latest committed state, or None before bootstrap."""

    def latest_revision(self) -> Optional[int]:
        """Return the authoritative committed revision without loading state."""

    def commit(
        self,
        *,
        expected_revision: int,
        state: Dict[str, Any],
        audit: AuditEvent,
        event_type: str,
        event_payload: Optional[Dict[str, Any]] = None,
        schema_version: int = 1,
    ) -> ControlPlaneRecord:
        """Atomically CAS state and append audit plus outbox records."""

    def append_audit(self, audit: AuditEvent, *, revision: Optional[int] = None) -> bool:
        """Append an audit-only event in its own transaction."""

    def list_audit(
        self,
        *,
        limit: int = 50,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return newest safe audit records."""

    def export_audit(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """Return an ascending, hash-chain-preserving export page."""

    def list_outbox(
        self,
        *,
        after_revision: int = 0,
        limit: int = 100,
    ) -> List[OutboxRecord]:
        """Return durable change records ordered by revision."""

    def list_unpublished_outbox(self, *, limit: int = 100) -> List[OutboxRecord]:
        """Return oldest undelivered control-plane notifications."""

    def verify_audit_chain(self) -> None:
        """Raise AuditIntegrityError if the persisted hash chain was altered."""

    def mark_outbox_published(
        self,
        event_id: str,
        *,
        published_at: Optional[datetime] = None,
    ) -> bool:
        """Mark one pending outbox event delivered; return False if already final."""

    def record_outbox_failure(self, event_id: str, error: str) -> bool:
        """Record one failed delivery attempt for a still-pending outbox event."""
