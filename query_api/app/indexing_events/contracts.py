"""Provider-neutral contracts for durable ordered indexing events."""

from __future__ import annotations

import hashlib
import json
import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, Tuple


IDENTITY_SEPARATOR = "\x1f"


class IdempotencyConflictError(RuntimeError):
    """Raised when a key is reused for a different logical mutation."""


class IndexingEventStoreNotReadyError(RuntimeError):
    """Raised when the durable sequencer/outbox cannot safely serve writes."""


class RepairSourceNotFoundError(RuntimeError):
    """Raised when a repair is requested for an identity with no durable event."""


@dataclass(frozen=True)
class IndexingEventDraft:
    tenant_id: str
    collection: str
    document_id: str
    operation: str
    target_clusters: Tuple[str, ...]
    routing_revision: int
    idempotency_key: str
    rollout_id: Optional[str] = None
    trace: Dict[str, str] = field(default_factory=dict)
    document: Optional[Dict[str, Any]] = None
    delete_filter: Optional[str] = None

    @property
    def logical_key(self) -> str:
        return IDENTITY_SEPARATOR.join(
            (self.tenant_id, self.collection, self.document_id)
        )

    @property
    def identity_hash(self) -> str:
        return hashlib.sha256(self.logical_key.encode("utf-8")).hexdigest()

    @property
    def idempotency_key_hash(self) -> str:
        return indexing_idempotency_key_hash(
            self.tenant_id,
            self.collection,
            self.document_id,
            self.idempotency_key,
        )

    @property
    def request_digest(self) -> str:
        # Routing and trace are server-derived metadata. Excluding them lets an
        # HTTP retry recover the exact originally stored envelope after a route
        # revision changes, while still rejecting key reuse for another mutation.
        material = {
            "tenant_id": self.tenant_id,
            "collection": self.collection,
            "document_id": self.document_id,
            "operation": self.operation,
            "document": self.document,
            "delete_filter": self.delete_filter,
        }
        encoded = json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PreparedIndexingEvent:
    event_id: str
    sequence: int
    payload: Dict[str, Any]
    created_at: datetime
    global_position: int = 0
    published_at: Optional[datetime] = None
    publish_attempts: int = 0
    last_error: Optional[str] = None

    @property
    def kafka_key(self) -> bytes:
        identity = self.payload["identity"]
        return IDENTITY_SEPARATOR.join(
            (
                identity["tenant_id"],
                identity["collection"],
                identity["document_id"],
            )
        ).encode("utf-8")

    @property
    def identity_hash(self) -> str:
        identity = self.payload["identity"]
        logical_key = IDENTITY_SEPARATOR.join(
            (
                identity["tenant_id"],
                identity["collection"],
                identity["document_id"],
            )
        )
        return hashlib.sha256(logical_key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LatestRepairDraft:
    """Route-only repair request that clones the latest durable mutation."""

    identity_hash: str
    idempotency_key: str
    target_clusters: Tuple[str, ...]
    routing_revision: int
    rollout_id: Optional[str] = None
    trace: Dict[str, str] = field(default_factory=dict)


class IndexingEventStore(Protocol):
    def check_ready(self) -> None:
        """Fail unless sequencing and outbox storage are available."""

    def prepare(self, draft: IndexingEventDraft) -> PreparedIndexingEvent:
        """Idempotently allocate sequence and persist an unpublished envelope."""

    def list_pending(self, *, limit: int = 100) -> List[PreparedIndexingEvent]:
        """Return oldest unpublished envelopes for crash recovery."""

    def mark_published(self, event_id: str) -> bool:
        """Mark an envelope acknowledged by Kafka."""

    def record_failure(self, event_id: str, error: str) -> bool:
        """Record one failed publish attempt without losing the envelope."""

    def pending_count(self) -> int:
        """Return current unpublished backlog size."""

    def high_water_mark(self) -> int:
        """Return the greatest globally allocated outbox position, or zero."""

    def list_latest_by_identity(
        self,
        *,
        after_position: int,
        through_position: int,
        limit: int = 1000,
    ) -> List[PreparedIndexingEvent]:
        """Return the latest event per identity inside `(after, through]`."""

    def prepare_latest_repair(
        self,
        repair: LatestRepairDraft,
    ) -> PreparedIndexingEvent:
        """Atomically clone the latest mutation into a newly routed event."""


def indexing_idempotency_key_hash(
    tenant_id: str,
    collection: str,
    document_id: str,
    idempotency_key: str,
) -> str:
    material = IDENTITY_SEPARATOR.join(
        (tenant_id, collection, document_id, idempotency_key)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_event_payload(
    draft: IndexingEventDraft,
    event_id: str,
    sequence: int,
    occurred_at: datetime,
) -> Dict[str, Any]:
    """Build the canonical v2 envelope after durable sequence allocation."""
    payload: Dict[str, Any] = {
        "envelope_version": 2,
        "event_id": event_id,
        "identity": {
            "tenant_id": draft.tenant_id,
            "collection": draft.collection,
            "document_id": draft.document_id,
        },
        "document_version": sequence,
        "sequence": sequence,
        "operation": draft.operation,
        "routing_revision": draft.routing_revision,
        "rollout_id": draft.rollout_id,
        "target_clusters": list(draft.target_clusters),
        "occurred_at": occurred_at.isoformat(),
        "trace": dict(draft.trace),
    }
    if draft.document is not None:
        payload["document"] = copy.deepcopy(draft.document)
    if draft.delete_filter is not None:
        payload["delete_filter"] = draft.delete_filter
    return payload


def build_latest_repair_draft(
    repair: LatestRepairDraft,
    source: PreparedIndexingEvent,
) -> IndexingEventDraft:
    """Clone mutation data while replacing only repair routing metadata."""
    if source.identity_hash != repair.identity_hash:
        raise ValueError("Repair source identity does not match the requested identity")
    payload = source.payload
    identity = payload["identity"]
    operation = str(payload["operation"])
    if operation not in {"upsert", "delete", "tombstone"}:
        raise ValueError("Repair source has an unsupported operation")
    return IndexingEventDraft(
        tenant_id=str(identity["tenant_id"]),
        collection=str(identity["collection"]),
        document_id=str(identity["document_id"]),
        operation=operation,
        target_clusters=tuple(repair.target_clusters),
        routing_revision=repair.routing_revision,
        idempotency_key=repair.idempotency_key,
        rollout_id=repair.rollout_id,
        trace=dict(repair.trace),
        document=copy.deepcopy(payload.get("document")),
        delete_filter=payload.get("delete_filter"),
    )
