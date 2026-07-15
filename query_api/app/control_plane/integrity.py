"""Canonical serialization and integrity helpers for control-plane records."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Mapping


ZERO_AUDIT_HASH = "0" * 64


def normalize_json_object(value: Mapping[str, Any], *, field_name: str) -> Dict[str, Any]:
    """Return a detached JSON object and reject values databases cannot preserve."""
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a JSON object")
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain only finite JSON values") from exc
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # Defensive: Mapping above should guarantee this.
        raise TypeError(f"{field_name} must be a JSON object")
    return decoded


def canonical_json(value: Any) -> str:
    """Serialize a previously validated JSON value deterministically."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_state_digest(state: Mapping[str, Any]) -> str:
    """Return the canonical SHA-256 digest used by every store implementation."""
    normalized = normalize_json_object(state, field_name="state")
    return hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()


def timestamp_text(value: datetime) -> str:
    """Normalize database timestamps before including them in an audit hash."""
    return utc_datetime(value).isoformat()


def utc_datetime(value: datetime) -> datetime:
    """Return an aware UTC datetime across PostgreSQL and SQLite adapters."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def build_audit_material(
    *,
    event_id: str,
    sequence: int,
    revision: int | None,
    timestamp: datetime,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str,
    status: str,
    request_id: str,
    details: Mapping[str, Any],
    previous_hash: str,
) -> Dict[str, Any]:
    """Build the exact material committed to the tamper-evident audit chain."""
    return {
        "id": event_id,
        "sequence": sequence,
        "revision": revision,
        "timestamp": timestamp_text(timestamp),
        "actor": actor,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "status": status,
        "request_id": request_id,
        "details": normalize_json_object(details, field_name="audit.details"),
        "previous_hash": previous_hash,
    }


def audit_event_hash(material: Mapping[str, Any]) -> str:
    """Hash one audit event including the previous event hash."""
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
