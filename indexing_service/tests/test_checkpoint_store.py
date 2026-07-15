"""Ordering and durable checkpoint adapter tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import typesense


_app_dir = Path(__file__).resolve().parent.parent / "app"
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

from checkpoint_store import (  # noqa: E402
    CheckpointStoreError,
    EventOrderingError,
    InMemoryCheckpointStore,
    TypesenseCheckpointStore,
    build_checkpoint_store_from_env,
    checkpoint_document_id,
    checkpoint_from_event,
    classify_event,
)
from event_envelope import parse_indexing_event  # noqa: E402


def event(
    *,
    event_id="evt-00000001",
    version=1,
    sequence=1,
    operation="upsert",
    document_name=None,
    occurred_at="2026-07-10T08:00:00Z",
    request_id=None,
    targets=None,
):
    payload = {
        "envelope_version": 2,
        "event_id": event_id,
        "identity": {
            "tenant_id": "tenant-a",
            "collection": "products",
            "document_id": "doc-1",
        },
        "document_version": version,
        "sequence": sequence,
        "operation": operation,
        "routing_revision": 1,
        "rollout_id": None,
        "target_clusters": targets or ["cluster-a"],
        "occurred_at": occurred_at,
        "trace": {"request_id": request_id} if request_id else {},
    }
    if operation == "upsert":
        payload["document"] = {
            "id": "doc-1",
            "name": document_name or f"version-{version}",
        }
    return parse_indexing_event(payload, allow_legacy=False)


def test_ordering_classifies_apply_duplicate_and_stale_without_resurrection():
    first = event()
    checkpoint = checkpoint_from_event(first, "cluster-a")

    assert classify_event(first, None) == "apply"
    assert classify_event(first, checkpoint) == "duplicate"
    assert (
        classify_event(
            event(event_id="evt-00000000", version=1, sequence=1),
            checkpoint_from_event(
                event(event_id="evt-00000002", version=2, sequence=2, operation="tombstone"),
                "cluster-a",
            ),
        )
        == "stale"
    )
    assert (
        classify_event(
            event(event_id="evt-00000003", version=3, sequence=3),
            checkpoint_from_event(
                event(event_id="evt-00000002", version=2, sequence=2, operation="tombstone"),
                "cluster-a",
            ),
        )
        == "apply"
    )


def test_ordering_rejects_reused_ids_and_partial_version_advances():
    checkpoint = checkpoint_from_event(event(), "cluster-a")

    with pytest.raises(EventOrderingError, match="event_id was reused"):
        classify_event(
            event(event_id="evt-00000001", version=2, sequence=2),
            checkpoint,
        )
    with pytest.raises(EventOrderingError, match="event_id was reused"):
        classify_event(
            event(document_name="mutated retry"),
            checkpoint,
        )
    with pytest.raises(EventOrderingError, match="must both increase"):
        classify_event(
            event(event_id="evt-00000002", version=1, sequence=2),
            checkpoint,
        )


def test_retry_metadata_does_not_change_the_mutation_digest():
    checkpoint = checkpoint_from_event(
        event(targets=["cluster-a", "cluster-b"]),
        "cluster-a",
    )
    retried = event(
        occurred_at="2026-07-10T09:30:00+01:00",
        request_id="request-retry",
        targets=["cluster-b", "cluster-a"],
    )

    assert classify_event(retried, checkpoint) == "duplicate"


class FakeDocuments:
    def __init__(self, *, fail_upsert=False):
        self.values = {}
        self.requested_id = None
        self.fail_upsert = fail_upsert

    def __getitem__(self, document_id):
        self.requested_id = document_id
        return self

    def retrieve(self):
        if self.requested_id not in self.values:
            raise typesense.exceptions.ObjectNotFound("missing")
        return self.values[self.requested_id]

    def upsert(self, document):
        if self.fail_upsert:
            raise RuntimeError("write denied")
        self.values[document["id"]] = dict(document)


class FakeCollection:
    def __init__(self, schema, *, fail_upsert=False):
        self.schema = schema
        self.documents = FakeDocuments(fail_upsert=fail_upsert)

    def retrieve(self):
        if self.schema is None:
            raise typesense.exceptions.ObjectNotFound("missing")
        return self.schema


class FakeCollections:
    def __init__(self, *, fail_upsert=False):
        self.values = {}
        self.fail_upsert = fail_upsert

    def __getitem__(self, name):
        self.values.setdefault(
            name,
            FakeCollection(None, fail_upsert=self.fail_upsert),
        )
        return self.values[name]

    def create(self, schema):
        current = self.values.get(schema["name"])
        if current is not None and current.schema is not None:
            raise typesense.exceptions.ObjectAlreadyExists("exists")
        collection = FakeCollection(
            dict(schema),
            fail_upsert=self.fail_upsert,
        )
        self.values[schema["name"]] = collection
        return schema


class FakeClient:
    def __init__(self, *, fail_upsert=False):
        self.collections = FakeCollections(fail_upsert=fail_upsert)


def test_typesense_checkpoint_store_is_shared_and_durable_per_target():
    client = FakeClient()
    clients = {"cluster-a": client}
    store = TypesenseCheckpointStore(clients)
    store.ensure_ready()
    incoming = event()
    checkpoint = checkpoint_from_event(incoming, "cluster-a")

    store.save(checkpoint)
    restarted_store = TypesenseCheckpointStore(clients)

    assert restarted_store.load(incoming, "cluster-a") == checkpoint


def test_typesense_checkpoint_store_rejects_schema_drift():
    client = FakeClient()
    client.collections.values["_imposbro_indexing_checkpoints"] = FakeCollection(
        {"name": "_imposbro_indexing_checkpoints", "fields": []}
    )
    store = TypesenseCheckpointStore({"cluster-a": client})

    with pytest.raises(CheckpointStoreError, match="schema mismatch"):
        store.ensure_ready()


def test_typesense_checkpoint_store_fails_readiness_without_write_access():
    store = TypesenseCheckpointStore({"cluster-a": FakeClient(fail_upsert=True)})

    with pytest.raises(CheckpointStoreError, match="not writable"):
        store.ensure_ready()


def test_typesense_checkpoint_store_rejects_corrupt_identity_state():
    client = FakeClient()
    store = TypesenseCheckpointStore({"cluster-a": client})
    store.ensure_ready()
    incoming = event()
    checkpoint = checkpoint_from_event(incoming, "cluster-a")
    store.save(checkpoint)
    documents = client.collections.values[store.collection_name].documents.values
    documents[checkpoint_document_id(checkpoint)]["tenant_id"] = "other-tenant"

    with pytest.raises(CheckpointStoreError, match="identity mismatch"):
        store.load(incoming, "cluster-a")


def test_memory_backend_requires_explicit_volatile_opt_in(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "development")
    monkeypatch.setenv("INDEXING_CHECKPOINT_BACKEND", "memory")
    monkeypatch.delenv("INDEXING_ALLOW_VOLATILE_CHECKPOINTS", raising=False)

    with pytest.raises(CheckpointStoreError, match="volatile"):
        build_checkpoint_store_from_env({})

    monkeypatch.setenv("INDEXING_ALLOW_VOLATILE_CHECKPOINTS", "true")
    assert isinstance(build_checkpoint_store_from_env({}), InMemoryCheckpointStore)
