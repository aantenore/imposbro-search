"""Boundary and compatibility tests for indexing event envelope v2."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_app_dir = Path(__file__).resolve().parent.parent / "app"
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

from event_envelope import (  # noqa: E402
    EventEnvelopeError,
    IndexingEventV2,
    LegacyIndexingEvent,
    legacy_events_enabled,
    parse_indexing_event,
)


def v2_event(**overrides):
    event = {
        "envelope_version": 2,
        "event_id": "evt-00000001",
        "identity": {
            "tenant_id": "tenant-a",
            "collection": "products",
            "document_id": "doc-1",
        },
        "document_version": 1,
        "sequence": 1,
        "operation": "upsert",
        "routing_revision": 7,
        "rollout_id": "rollout-0001",
        "target_clusters": ["cluster-a", "cluster-b"],
        "occurred_at": "2026-07-10T08:00:00Z",
        "trace": {
            "request_id": "request-1",
            "correlation_id": "correlation-1",
            "traceparent": (
                "00-4bf92f3577b34da6a3ce929d0e0e4736-"
                "00f067aa0ba902b7-01"
            ),
        },
        "document": {"id": "doc-1", "name": "Product"},
    }
    event.update(overrides)
    return event


def test_parse_v2_event_exposes_canonical_identity_and_kafka_key():
    event = parse_indexing_event(v2_event(), allow_legacy=False)

    assert isinstance(event, IndexingEventV2)
    assert event.identity.logical_key == "tenant-a\x1fproducts\x1fdoc-1"
    assert event.identity.kafka_key == b"tenant-a\x1fproducts\x1fdoc-1"
    assert event.target_clusters == ("cluster-a", "cluster-b")
    assert event.occurred_at.isoformat() == "2026-07-10T08:00:00+00:00"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda event: event.update(unknown=True), "Unknown v2 event fields"),
        (
            lambda event: event.update(target_clusters=["cluster-a", "cluster-a"]),
            "must not contain duplicates",
        ),
        (
            lambda event: event.update(occurred_at="2026-07-10T08:00:00"),
            "RFC3339",
        ),
        (
            lambda event: event.update(occurred_at="2026-07-10 08:00:00Z"),
            "RFC3339",
        ),
        (
            lambda event: event["document"].update(id="different"),
            "document.id must exactly match",
        ),
        (
            lambda event: event.update(document_version=0),
            "document_version must be an integer greater than zero",
        ),
        (
            lambda event: event.update(sequence=1 << 63),
            "sequence must not exceed",
        ),
        (
            lambda event: event.update(event_id=12345678),
            "event_id must be a string",
        ),
        (
            lambda event: event.update(target_clusters=[" cluster-a"]),
            "leading or trailing whitespace",
        ),
        (
            lambda event: event.update(trace=[]),
            "trace must be an object",
        ),
        (
            lambda event: event.update(trace={"traceparent": "00-invalid"}),
            "valid W3C traceparent",
        ),
    ],
)
def test_parse_v2_event_rejects_ambiguous_or_unversioned_fields(mutate, message):
    payload = v2_event()
    mutate(payload)

    with pytest.raises(EventEnvelopeError, match=message):
        parse_indexing_event(payload, allow_legacy=False)


def test_traceparent_accepts_forward_compatible_version_and_rejects_ff():
    future_version = v2_event()
    future_version["trace"]["traceparent"] = (
        "01-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    )

    parsed = parse_indexing_event(future_version, allow_legacy=False)

    assert parsed.trace.traceparent.startswith("01-")

    forbidden_version = v2_event()
    forbidden_version["trace"]["traceparent"] = (
        "ff-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    )
    with pytest.raises(EventEnvelopeError, match="valid W3C traceparent"):
        parse_indexing_event(forbidden_version, allow_legacy=False)


def test_rollout_id_uses_the_same_name_contract_as_query_api():
    parsed = parse_indexing_event(v2_event(rollout_id="r"), allow_legacy=False)

    assert parsed.rollout_id == "r"


def test_parse_tombstone_requires_no_document():
    payload = v2_event(
        operation="tombstone",
        document=None,
        document_version=2,
        sequence=2,
        event_id="evt-00000002",
    )

    event = parse_indexing_event(payload, allow_legacy=False)

    assert event.operation == "tombstone"
    assert event.document is None


def test_delete_filter_is_limited_to_exact_document_and_tenant_conjunction():
    payload = v2_event(
        operation="delete",
        document=None,
        delete_filter="(id:=doc-1) && tenant_id:=tenant-a",
    )

    event = parse_indexing_event(payload, allow_legacy=False)

    assert event.delete_filter == "(id:=doc-1) && tenant_id:=tenant-a"

    for unsafe_filter in (
        "tenant_id:=tenant-a",
        "(id:=another-doc) && tenant_id:=tenant-a",
        "(id:=doc-1) || tenant_id:=tenant-a",
    ):
        payload["delete_filter"] = unsafe_filter
        with pytest.raises(EventEnvelopeError, match="exact document-id"):
            parse_indexing_event(payload, allow_legacy=False)


def test_legacy_event_is_rejected_unless_explicitly_enabled():
    payload = {
        "collection": "products",
        "target_cluster": "cluster-a",
        "document": {"id": "doc-1"},
    }

    with pytest.raises(EventEnvelopeError, match="Legacy indexing event rejected"):
        parse_indexing_event(payload, allow_legacy=False)

    event = parse_indexing_event(payload, allow_legacy=True)
    assert isinstance(event, LegacyIndexingEvent)
    assert event.document_id == "doc-1"


def test_legacy_compatibility_setting_defaults_off_and_is_strict(monkeypatch):
    monkeypatch.delenv("INDEXING_ALLOW_LEGACY_EVENTS", raising=False)
    assert legacy_events_enabled() is False

    monkeypatch.setenv("INDEXING_ALLOW_LEGACY_EVENTS", "invalid")
    with pytest.raises(RuntimeError, match="must be a boolean"):
        legacy_events_enabled()
