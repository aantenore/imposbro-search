"""Versioned indexing event contract and strict boundary validation."""

from __future__ import annotations

import os
import re
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Mapping, Optional, Tuple, Union


ENVELOPE_VERSION = 2
MAX_INT64 = (1 << 63) - 1
IDENTITY_SEPARATOR = "\x1f"
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
DOCUMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,256}$")
EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
TENANT_DELETE_FILTER_PATTERN = re.compile(
    r"^\(id:=(?P<document_id>[A-Za-z0-9_.-]{1,256})\) && "
    r"[A-Za-z_][A-Za-z0-9_.]*:="
    r"(?:[A-Za-z0-9_-]{1,128}|"
    r"\[[A-Za-z0-9_-]{1,128}(?:,[A-Za-z0-9_-]{1,128})*\])$"
)
TRACEPARENT_PATTERN = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<parent_id>[0-9a-f]{16})-(?P<trace_flags>[0-9a-f]{2})$"
)
OPERATIONS = {"upsert", "delete", "tombstone"}
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


class EventEnvelopeError(ValueError):
    """Raised when a Kafka payload violates the declared envelope contract."""


@dataclass(frozen=True)
class EventIdentity:
    tenant_id: str
    collection: str
    document_id: str

    @property
    def logical_key(self) -> str:
        return IDENTITY_SEPARATOR.join(
            (self.tenant_id, self.collection, self.document_id)
        )

    @property
    def kafka_key(self) -> bytes:
        return self.logical_key.encode("utf-8")


@dataclass(frozen=True)
class TraceMetadata:
    request_id: str = ""
    correlation_id: str = ""
    traceparent: str = ""
    tracestate: str = ""


@dataclass(frozen=True)
class IndexingEventV2:
    envelope_version: Literal[2]
    event_id: str
    identity: EventIdentity
    document_version: int
    sequence: int
    operation: Literal["upsert", "delete", "tombstone"]
    routing_revision: int
    rollout_id: Optional[str]
    target_clusters: Tuple[str, ...]
    occurred_at: datetime
    trace: TraceMetadata = field(default_factory=TraceMetadata)
    document: Optional[Dict[str, Any]] = None
    delete_filter: Optional[str] = None


@dataclass(frozen=True)
class LegacyIndexingEvent:
    """Normalized legacy event; accepted only behind the explicit dev switch."""

    operation: Literal["upsert", "delete"]
    collection: str
    document_id: str
    target_cluster: Optional[str]
    document: Optional[Dict[str, Any]]
    request_id: str
    delete_filter: Optional[str]


ParsedIndexingEvent = Union[IndexingEventV2, LegacyIndexingEvent]


def legacy_events_enabled() -> bool:
    """Return the explicit compatibility switch; malformed values fail startup."""
    value = os.environ.get("INDEXING_ALLOW_LEGACY_EVENTS", "false").strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise RuntimeError(
        "INDEXING_ALLOW_LEGACY_EVENTS must be a boolean; legacy events are unsafe "
        "without versioned idempotency metadata"
    )


def parse_indexing_event(
    payload: Mapping[str, Any],
    *,
    allow_legacy: bool,
) -> ParsedIndexingEvent:
    if not isinstance(payload, Mapping):
        raise EventEnvelopeError("Indexing event must be a JSON object")
    version = payload.get("envelope_version")
    if version == ENVELOPE_VERSION:
        return _parse_v2(payload)
    if version is not None:
        raise EventEnvelopeError(f"Unsupported indexing envelope_version '{version}'")
    if not allow_legacy:
        raise EventEnvelopeError(
            "Legacy indexing event rejected; envelope_version=2 is required"
        )
    return _parse_legacy(payload)


def _parse_v2(payload: Mapping[str, Any]) -> IndexingEventV2:
    allowed_keys = {
        "envelope_version",
        "event_id",
        "identity",
        "document_version",
        "sequence",
        "operation",
        "routing_revision",
        "rollout_id",
        "target_clusters",
        "occurred_at",
        "trace",
        "document",
        "delete_filter",
    }
    unknown = sorted(set(payload) - allowed_keys)
    if unknown:
        raise EventEnvelopeError("Unknown v2 event fields: " + ", ".join(unknown))

    event_id = _identifier(payload.get("event_id"), "event_id", EVENT_ID_PATTERN)
    identity = _parse_identity(payload.get("identity"))
    document_version = _positive_int(payload.get("document_version"), "document_version")
    sequence = _positive_int(payload.get("sequence"), "sequence")
    routing_revision = _positive_int(payload.get("routing_revision"), "routing_revision")

    operation = str(payload.get("operation") or "").strip().lower()
    if operation not in OPERATIONS:
        raise EventEnvelopeError(
            "operation must be one of upsert, delete, or tombstone"
        )

    rollout_value = payload.get("rollout_id")
    rollout_id = None
    if rollout_value is not None:
        rollout_id = _identifier(
            rollout_value,
            "rollout_id",
            NAME_PATTERN,
        )

    targets_value = payload.get("target_clusters")
    if not isinstance(targets_value, list) or not targets_value:
        raise EventEnvelopeError("target_clusters must be a non-empty array")
    targets = tuple(
        _identifier(target, "target_clusters[]", NAME_PATTERN)
        for target in targets_value
    )
    if len(set(targets)) != len(targets):
        raise EventEnvelopeError("target_clusters must not contain duplicates")

    occurred_at = _timestamp(payload.get("occurred_at"))
    trace_value = payload.get("trace")
    trace = _parse_trace({} if trace_value is None else trace_value)
    document_value = payload.get("document")
    delete_filter_value = payload.get("delete_filter")

    document: Optional[Dict[str, Any]] = None
    delete_filter: Optional[str] = None
    if operation == "upsert":
        if not isinstance(document_value, dict) or not document_value:
            raise EventEnvelopeError("document is required for an upsert event")
        if str(document_value.get("id") or "") != identity.document_id:
            raise EventEnvelopeError(
                "document.id must exactly match identity.document_id"
            )
        if delete_filter_value is not None:
            raise EventEnvelopeError("delete_filter is not allowed for upsert events")
        document = _json_object(document_value, "document")
    else:
        if document_value is not None:
            raise EventEnvelopeError(
                "document is not allowed for delete or tombstone events"
            )
        if delete_filter_value is not None:
            delete_filter = _tenant_delete_filter(
                delete_filter_value,
                identity.document_id,
            )

    return IndexingEventV2(
        envelope_version=ENVELOPE_VERSION,
        event_id=event_id,
        identity=identity,
        document_version=document_version,
        sequence=sequence,
        operation=operation,
        routing_revision=routing_revision,
        rollout_id=rollout_id,
        target_clusters=targets,
        occurred_at=occurred_at,
        trace=trace,
        document=document,
        delete_filter=delete_filter,
    )


def _parse_legacy(payload: Mapping[str, Any]) -> LegacyIndexingEvent:
    operation = str(payload.get("action") or "upsert").strip().lower()
    if operation not in {"upsert", "delete"}:
        raise EventEnvelopeError(f"Unsupported indexing action '{operation}'")
    collection = _identifier(payload.get("collection"), "collection", NAME_PATTERN)
    document = payload.get("document")
    document_id = str(payload.get("document_id") or "")
    if operation == "upsert":
        if not isinstance(document, dict) or not document:
            raise EventEnvelopeError("Legacy upsert event requires document")
        document_id = str(document.get("id") or "")
    if not DOCUMENT_ID_PATTERN.fullmatch(document_id):
        raise EventEnvelopeError("Legacy event has an invalid document id")

    target = payload.get("target_cluster")
    target_cluster = (
        _identifier(target, "target_cluster", NAME_PATTERN) if target else None
    )
    request_id = _safe_text(
        payload.get("request_id") or "-",
        "request_id",
        maximum=256,
    )
    filter_value = payload.get("filter_by")
    delete_filter = (
        _safe_text(filter_value, "filter_by", maximum=4096)
        if filter_value is not None
        else None
    )
    return LegacyIndexingEvent(
        operation=operation,
        collection=collection,
        document_id=document_id,
        target_cluster=target_cluster,
        document=dict(document) if isinstance(document, dict) else None,
        request_id=request_id,
        delete_filter=delete_filter,
    )


def _parse_identity(value: Any) -> EventIdentity:
    if not isinstance(value, Mapping):
        raise EventEnvelopeError("identity must be an object")
    unknown = sorted(set(value) - {"tenant_id", "collection", "document_id"})
    if unknown:
        raise EventEnvelopeError("Unknown identity fields: " + ", ".join(unknown))
    tenant_id = _safe_text(value.get("tenant_id"), "identity.tenant_id", maximum=256)
    if IDENTITY_SEPARATOR in tenant_id:
        raise EventEnvelopeError("identity.tenant_id contains a reserved separator")
    collection = _identifier(value.get("collection"), "identity.collection", NAME_PATTERN)
    document_id = _identifier(
        value.get("document_id"),
        "identity.document_id",
        DOCUMENT_ID_PATTERN,
    )
    return EventIdentity(
        tenant_id=tenant_id,
        collection=collection,
        document_id=document_id,
    )


def _parse_trace(value: Any) -> TraceMetadata:
    if not isinstance(value, Mapping):
        raise EventEnvelopeError("trace must be an object")
    allowed = {"request_id", "correlation_id", "traceparent", "tracestate"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise EventEnvelopeError("Unknown trace fields: " + ", ".join(unknown))
    return TraceMetadata(
        request_id=_safe_optional_text(value.get("request_id"), "trace.request_id"),
        correlation_id=_safe_optional_text(
            value.get("correlation_id"),
            "trace.correlation_id",
        ),
        traceparent=_traceparent(value.get("traceparent")),
        tracestate=_safe_optional_text(value.get("tracestate"), "trace.tracestate"),
    )


def _identifier(value: Any, field_name: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str):
        raise EventEnvelopeError(f"{field_name} must be a string")
    text_value = value.strip()
    if text_value != value:
        raise EventEnvelopeError(
            f"{field_name} must not contain leading or trailing whitespace"
        )
    if not pattern.fullmatch(text_value):
        raise EventEnvelopeError(f"{field_name} has an invalid format")
    return text_value


def _safe_text(value: Any, field_name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise EventEnvelopeError(f"{field_name} must be a string")
    normalized = value.strip()
    if normalized != value:
        raise EventEnvelopeError(
            f"{field_name} must not contain leading or trailing whitespace"
        )
    if not normalized or len(normalized) > maximum:
        raise EventEnvelopeError(
            f"{field_name} must contain between 1 and {maximum} characters"
        )
    if any(not character.isprintable() for character in normalized):
        raise EventEnvelopeError(f"{field_name} must not contain control characters")
    return normalized


def _safe_optional_text(value: Any, field_name: str) -> str:
    if value in (None, ""):
        return ""
    return _safe_text(value, field_name, maximum=1024)


def _traceparent(value: Any) -> str:
    if value in (None, ""):
        return ""
    normalized = _safe_text(value, "trace.traceparent", maximum=55)
    match = TRACEPARENT_PATTERN.fullmatch(normalized)
    if (
        not match
        or match.group("version") == "ff"
        or match.group("trace_id") == "0" * 32
        or match.group("parent_id") == "0" * 16
    ):
        raise EventEnvelopeError("trace.traceparent must be a valid W3C traceparent")
    return normalized


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EventEnvelopeError(f"{field_name} must be an integer greater than zero")
    if value > MAX_INT64:
        raise EventEnvelopeError(f"{field_name} must not exceed {MAX_INT64}")
    return value


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not RFC3339_PATTERN.fullmatch(value):
        raise EventEnvelopeError("occurred_at must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventEnvelopeError("occurred_at must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise EventEnvelopeError("occurred_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _tenant_delete_filter(value: Any, document_id: str) -> str:
    """Accept only the exact tenant-safe filter shape emitted by Query API."""
    normalized = _safe_text(value, "delete_filter", maximum=1024)
    match = TENANT_DELETE_FILTER_PATTERN.fullmatch(normalized)
    if not match or match.group("document_id") != document_id:
        raise EventEnvelopeError(
            "delete_filter must be an exact document-id and tenant conjunction"
        )
    return normalized


def indexing_event_digest(event: IndexingEventV2) -> str:
    """Bind the event ID to mutation semantics, excluding diagnostic metadata."""
    material = {
        "envelope_version": event.envelope_version,
        "event_id": event.event_id,
        "identity": {
            "tenant_id": event.identity.tenant_id,
            "collection": event.identity.collection,
            "document_id": event.identity.document_id,
        },
        "document_version": event.document_version,
        "sequence": event.sequence,
        "operation": event.operation,
        "routing_revision": event.routing_revision,
        "rollout_id": event.rollout_id,
        "target_clusters": sorted(event.target_clusters),
        "document": event.document,
        "delete_filter": event.delete_filter,
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_object(value: Mapping[str, Any], field_name: str) -> Dict[str, Any]:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise EventEnvelopeError(
            f"{field_name} must contain only finite JSON values"
        ) from exc
    if not isinstance(decoded, dict):
        raise EventEnvelopeError(f"{field_name} must be a JSON object")
    return decoded
