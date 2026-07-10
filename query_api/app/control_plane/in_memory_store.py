"""Deterministic thread-safe store used by unit and contract tests."""

import copy
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .contracts import (
    AuditEvent,
    CURRENT_STATE_SCHEMA_VERSION,
    ControlPlaneRecord,
    OutboxRecord,
    AuditIntegrityError,
    StateConflictError,
    UnsupportedStateSchemaError,
)
from .integrity import (
    ZERO_AUDIT_HASH,
    audit_event_hash,
    build_audit_material,
    canonical_state_digest,
    normalize_json_object,
)


class InMemoryControlPlaneStore:
    """Implements the production store contract without external dependencies."""

    def __init__(self):
        self._lock = threading.RLock()
        self._record: Optional[ControlPlaneRecord] = None
        self._audit: List[Dict[str, Any]] = []
        self._outbox: List[OutboxRecord] = []
        self._audit_hash = ZERO_AUDIT_HASH

    def check_ready(self) -> None:
        return None

    def load(self) -> Optional[ControlPlaneRecord]:
        with self._lock:
            return copy.deepcopy(self._record)

    def latest_revision(self) -> Optional[int]:
        with self._lock:
            return self._record.revision if self._record else None

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
        if schema_version != CURRENT_STATE_SCHEMA_VERSION:
            raise UnsupportedStateSchemaError(
                CURRENT_STATE_SCHEMA_VERSION,
                schema_version,
            )
        state_copy = normalize_json_object(state, field_name="state")
        payload = normalize_json_object(
            event_payload or {},
            field_name="event_payload",
        )
        with self._lock:
            actual_revision = self._record.revision if self._record else 0
            if actual_revision != expected_revision:
                raise StateConflictError(expected_revision, actual_revision)
            now = datetime.now(timezone.utc)
            next_revision = actual_revision + 1
            record = ControlPlaneRecord(
                revision=next_revision,
                schema_version=schema_version,
                state=state_copy,
                state_digest=canonical_state_digest(state_copy),
                updated_at=now,
            )
            audit_record, next_hash = self._build_audit_record(
                audit,
                revision=next_revision,
                timestamp=now,
            )
            outbox_record = OutboxRecord(
                event_id=str(uuid.uuid4()),
                revision=next_revision,
                event_type=event_type,
                payload={
                    **payload,
                    "revision": next_revision,
                    "schema_version": schema_version,
                    "state_digest": record.state_digest,
                },
                created_at=now,
            )
            self._record = record
            self._audit.append(audit_record)
            self._audit_hash = next_hash
            self._outbox.append(outbox_record)
            return copy.deepcopy(record)

    def append_audit(self, audit: AuditEvent, *, revision: Optional[int] = None) -> bool:
        with self._lock:
            record, next_hash = self._build_audit_record(
                audit,
                revision=revision,
                timestamp=datetime.now(timezone.utc),
            )
            self._audit.append(record)
            self._audit_hash = next_hash
            return True

    def _build_audit_record(
        self,
        audit: AuditEvent,
        *,
        revision: Optional[int],
        timestamp: datetime,
    ) -> tuple[Dict[str, Any], str]:
        sequence = len(self._audit) + 1
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
            previous_hash=self._audit_hash,
        )
        event_hash = audit_event_hash(material)
        return {**material, "event_hash": event_hash}, event_hash

    def list_audit(
        self,
        *,
        limit: int = 50,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._lock:
            rows = [
                row
                for row in self._audit
                if (action is None or row["action"] == action)
                and (resource_type is None or row["resource_type"] == resource_type)
            ]
            return copy.deepcopy(list(reversed(rows[-limit:])))

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
        with self._lock:
            return copy.deepcopy(
                [
                    row
                    for row in self._audit
                    if int(row["sequence"]) > after_sequence
                ][:limit]
            )

    def list_outbox(
        self,
        *,
        after_revision: int = 0,
        limit: int = 100,
    ) -> List[OutboxRecord]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._lock:
            return copy.deepcopy(
                [row for row in self._outbox if row.revision > after_revision][:limit]
            )

    def list_unpublished_outbox(self, *, limit: int = 100) -> List[OutboxRecord]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._lock:
            return copy.deepcopy(
                [row for row in self._outbox if row.published_at is None][:limit]
            )

    def verify_audit_chain(self) -> None:
        with self._lock:
            previous_hash = ZERO_AUDIT_HASH
            expected_sequence = 1
            for row in self._audit:
                if row["sequence"] != expected_sequence:
                    raise AuditIntegrityError(
                        f"Audit sequence gap at {expected_sequence}"
                    )
                if row["previous_hash"] != previous_hash:
                    raise AuditIntegrityError(
                        f"Audit previous hash mismatch at sequence {expected_sequence}"
                    )
                material = {key: value for key, value in row.items() if key != "event_hash"}
                calculated_hash = audit_event_hash(material)
                if row["event_hash"] != calculated_hash:
                    raise AuditIntegrityError(
                        f"Audit event hash mismatch at sequence {expected_sequence}"
                    )
                previous_hash = calculated_hash
                expected_sequence += 1
            if self._audit_hash != previous_hash:
                raise AuditIntegrityError("Audit head does not match the event chain")

    def mark_outbox_published(
        self,
        event_id: str,
        *,
        published_at: Optional[datetime] = None,
    ) -> bool:
        with self._lock:
            for index, record in enumerate(self._outbox):
                if record.event_id != event_id or record.published_at is not None:
                    continue
                self._outbox[index] = replace(
                    record,
                    published_at=published_at or datetime.now(timezone.utc),
                    last_error=None,
                    publish_attempts=record.publish_attempts + 1,
                )
                return True
            return False

    def record_outbox_failure(self, event_id: str, error: str) -> bool:
        with self._lock:
            for index, record in enumerate(self._outbox):
                if record.event_id != event_id or record.published_at is not None:
                    continue
                self._outbox[index] = replace(
                    record,
                    last_error=str(error)[:4096],
                    publish_attempts=record.publish_attempts + 1,
                )
                return True
            return False
