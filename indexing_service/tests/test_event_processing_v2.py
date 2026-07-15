"""End-to-end worker-domain tests for fan-out, replay, and tombstones."""

from __future__ import annotations

import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
import typesense


_app_dir = Path(__file__).resolve().parent.parent / "app"
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

import consumer  # noqa: E402
from checkpoint_store import (  # noqa: E402
    CheckpointStoreError,
    EventOrderingError,
    InMemoryCheckpointStore,
)
from event_envelope import EventEnvelopeError, parse_indexing_event  # noqa: E402


def v2_event(
    *,
    event_id="evt-00000001",
    version=1,
    sequence=1,
    operation="upsert",
    targets=None,
    traceparent=None,
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
        "routing_revision": 9,
        "rollout_id": "rollout-0001",
        "target_clusters": targets or ["cluster-a"],
        "occurred_at": "2026-07-10T08:00:00Z",
        "trace": {
            "request_id": "request-1",
            **({"traceparent": traceparent} if traceparent else {}),
        },
    }
    if operation == "upsert":
        payload["document"] = {"id": "doc-1", "name": f"version-{version}"}
    return payload


KAFKA_KEY = b"tenant-a\x1fproducts\x1fdoc-1"
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


class FakeDocumentReference:
    def __init__(self, documents, document_id):
        self.documents = documents
        self.document_id = document_id

    def delete(self):
        if self.documents.raise_not_found:
            raise typesense.exceptions.ObjectNotFound("missing")
        self.documents.deleted.append(self.document_id)


class FakeDocuments:
    def __init__(self, *, fail_upsert_once=False):
        self.upserted = []
        self.deleted = []
        self.raise_not_found = False
        self.fail_upsert_once = fail_upsert_once

    def upsert(self, document):
        if self.fail_upsert_once:
            self.fail_upsert_once = False
            raise RuntimeError("temporary target failure")
        self.upserted.append(dict(document))

    def __getitem__(self, document_id):
        return FakeDocumentReference(self, document_id)

    def delete(self, _params):
        if self.raise_not_found:
            raise typesense.exceptions.ObjectNotFound("missing")
        return {"num_deleted": 1}


class FakeCollections:
    def __init__(self, documents):
        self.documents = documents

    def __getitem__(self, _collection_name):
        return type("Collection", (), {"documents": self.documents})()


class FakeClient:
    def __init__(self, *, fail_upsert_once=False):
        self.documents = FakeDocuments(fail_upsert_once=fail_upsert_once)
        self.collections = FakeCollections(self.documents)


def test_one_logical_event_fans_out_once_and_duplicate_replay_is_a_noop():
    cluster_a = FakeClient()
    cluster_b = FakeClient()
    clients = {"cluster-a": cluster_a, "cluster-b": cluster_b}
    store = InMemoryCheckpointStore()
    payload = v2_event(targets=["cluster-a", "cluster-b"])

    consumer.process_message(
        payload,
        clients,
        checkpoint_store=store,
        message_key=KAFKA_KEY,
        allow_legacy=False,
    )
    consumer.process_message(
        payload,
        clients,
        checkpoint_store=store,
        message_key=KAFKA_KEY,
        allow_legacy=False,
    )

    assert cluster_a.documents.upserted == [{"id": "doc-1", "name": "version-1"}]
    assert cluster_b.documents.upserted == [{"id": "doc-1", "name": "version-1"}]


def test_traceparent_is_in_apply_and_error_logs_without_metric_labels(caplog):
    caplog.set_level("INFO")
    successful = FakeClient()
    consumer.process_message(
        v2_event(traceparent=TRACEPARENT),
        {"cluster-a": successful},
        checkpoint_store=InMemoryCheckpointStore(),
        message_key=KAFKA_KEY,
        allow_legacy=False,
    )
    failing = FakeClient(fail_upsert_once=True)
    with pytest.raises(RuntimeError, match="temporary target failure"):
        consumer.process_message(
            v2_event(traceparent=TRACEPARENT),
            {"cluster-a": failing},
            checkpoint_store=InMemoryCheckpointStore(),
            message_key=KAFKA_KEY,
            allow_legacy=False,
        )

    assert caplog.text.count(f"traceparent={TRACEPARENT}") >= 2
    metric_label_names = set(consumer.metrics.INDEXING_EVENTS._labelnames)
    assert "traceparent" not in metric_label_names


def test_retry_resumes_only_the_target_without_a_checkpoint():
    cluster_a = FakeClient()
    cluster_b = FakeClient(fail_upsert_once=True)
    clients = {"cluster-a": cluster_a, "cluster-b": cluster_b}
    store = InMemoryCheckpointStore()
    payload = v2_event(targets=["cluster-a", "cluster-b"])

    with pytest.raises(RuntimeError, match="temporary target failure"):
        consumer.process_message(
            payload,
            clients,
            checkpoint_store=store,
            message_key=KAFKA_KEY,
            allow_legacy=False,
        )
    consumer.process_message(
        payload,
        clients,
        checkpoint_store=store,
        message_key=KAFKA_KEY,
        allow_legacy=False,
    )

    assert len(cluster_a.documents.upserted) == 1
    assert len(cluster_b.documents.upserted) == 1


def test_preflight_rejects_mutated_retry_before_any_new_target_write():
    cluster_a = FakeClient()
    cluster_b = FakeClient(fail_upsert_once=True)
    clients = {"cluster-a": cluster_a, "cluster-b": cluster_b}
    store = InMemoryCheckpointStore()
    original = v2_event(targets=["cluster-a", "cluster-b"])

    with pytest.raises(RuntimeError, match="temporary target failure"):
        consumer.process_message(
            original,
            clients,
            checkpoint_store=store,
            message_key=KAFKA_KEY,
            allow_legacy=False,
        )

    mutated = v2_event(targets=["cluster-b", "cluster-a"])
    mutated["document"]["name"] = "mutated-retry"
    with pytest.raises(EventOrderingError, match="event_id was reused"):
        consumer.process_message(
            mutated,
            clients,
            checkpoint_store=store,
            message_key=KAFKA_KEY,
            allow_legacy=False,
        )

    assert cluster_a.documents.upserted == [{"id": "doc-1", "name": "version-1"}]
    assert cluster_b.documents.upserted == []


def test_stale_checkpoint_on_one_target_suppresses_missing_target_fanout():
    cluster_a = FakeClient()
    cluster_b = FakeClient()
    clients = {"cluster-a": cluster_a, "cluster-b": cluster_b}
    store = InMemoryCheckpointStore()
    kwargs = {
        "typesense_clients": clients,
        "checkpoint_store": store,
        "message_key": KAFKA_KEY,
        "allow_legacy": False,
    }
    consumer.process_message(v2_event(targets=["cluster-a"]), **kwargs)
    consumer.process_message(
        v2_event(
            event_id="evt-00000002",
            version=2,
            sequence=2,
            operation="tombstone",
            targets=["cluster-a"],
        ),
        **kwargs,
    )

    consumer.process_message(
        v2_event(
            event_id="evt-delayed2",
            version=1,
            sequence=1,
            targets=["cluster-b", "cluster-a"],
        ),
        **kwargs,
    )

    assert cluster_b.documents.upserted == []


def test_event_fence_prevents_newer_event_from_being_overwritten_by_older_thread():
    newer_applied = threading.Event()
    release_newer = threading.Event()
    older_started = threading.Event()
    errors = []

    class BlockingDocuments(FakeDocuments):
        def __init__(self):
            super().__init__()
            self.current = None
            self._lock = threading.Lock()

        def upsert(self, document):
            with self._lock:
                self.current = dict(document)
                self.upserted.append(dict(document))
            if document["name"] == "version-2":
                newer_applied.set()
                if not release_newer.wait(timeout=2):
                    raise RuntimeError("test release timed out")

    documents = BlockingDocuments()
    client = type(
        "BlockingClient",
        (),
        {"collections": FakeCollections(documents)},
    )()
    store = InMemoryCheckpointStore(lock_timeout_ms=1000)
    kwargs = {
        "typesense_clients": {"cluster-a": client},
        "checkpoint_store": store,
        "message_key": KAFKA_KEY,
        "allow_legacy": False,
    }

    def run(payload, started=None):
        if started is not None:
            started.set()
        try:
            consumer.process_message(payload, **kwargs)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    newer = threading.Thread(
        target=run,
        args=(v2_event(event_id="evt-00000002", version=2, sequence=2),),
    )
    newer.start()
    assert newer_applied.wait(timeout=1)
    older = threading.Thread(target=run, args=(v2_event(), older_started))
    older.start()
    assert older_started.wait(timeout=1)
    assert older.is_alive()

    release_newer.set()
    newer.join(timeout=2)
    older.join(timeout=2)

    assert not errors
    assert not newer.is_alive()
    assert not older.is_alive()
    assert documents.current == {"id": "doc-1", "name": "version-2"}
    assert documents.upserted == [{"id": "doc-1", "name": "version-2"}]


def test_event_fence_lock_timeout_is_fail_fast():
    event = parse_indexing_event(v2_event(), allow_legacy=False)
    store = InMemoryCheckpointStore(lock_timeout_ms=20)
    acquired = threading.Event()
    release = threading.Event()

    def hold_fence():
        with store.event_scope(event):
            acquired.set()
            release.wait(timeout=2)

    holder = threading.Thread(target=hold_fence)
    holder.start()
    assert acquired.wait(timeout=1)
    try:
        with pytest.raises(CheckpointStoreError, match="Timed out"):
            with store.event_scope(event):
                pass
    finally:
        release.set()
        holder.join(timeout=2)


def test_tombstone_blocks_delayed_upsert_and_newer_recreate_is_explicit():
    client = FakeClient()
    store = InMemoryCheckpointStore()
    kwargs = {
        "typesense_clients": {"cluster-a": client},
        "checkpoint_store": store,
        "message_key": KAFKA_KEY,
        "allow_legacy": False,
    }
    consumer.process_message(v2_event(), **kwargs)
    consumer.process_message(
        v2_event(
            event_id="evt-00000002",
            version=2,
            sequence=2,
            operation="tombstone",
        ),
        **kwargs,
    )
    consumer.process_message(
        v2_event(event_id="evt-delayed1", version=1, sequence=1),
        **kwargs,
    )

    assert client.documents.deleted == ["doc-1"]
    assert client.documents.upserted == [{"id": "doc-1", "name": "version-1"}]

    consumer.process_message(
        v2_event(event_id="evt-00000003", version=3, sequence=3),
        **kwargs,
    )
    assert client.documents.upserted[-1] == {"id": "doc-1", "name": "version-3"}


@pytest.mark.parametrize("message_key", [None, b"wrong-key"])
def test_v2_event_fails_closed_on_missing_or_wrong_kafka_key(message_key):
    with pytest.raises(EventEnvelopeError, match="Kafka"):
        consumer.process_message(
            v2_event(),
            {"cluster-a": FakeClient()},
            checkpoint_store=InMemoryCheckpointStore(),
            message_key=message_key,
            allow_legacy=False,
        )


def test_v2_event_fails_closed_without_checkpoint_store():
    with pytest.raises(CheckpointStoreError, match="durable checkpoint"):
        consumer.process_message(
            v2_event(),
            {"cluster-a": FakeClient()},
            message_key=KAFKA_KEY,
            allow_legacy=False,
        )


def test_checkpoint_read_failure_prevents_document_side_effect():
    class FailingCheckpointStore:
        backend_name = "failing-test"

        def ensure_ready(self):
            return None

        @contextmanager
        def event_scope(self, _event):
            yield self

        def load(self, _event, _target_cluster):
            raise CheckpointStoreError("checkpoint unavailable")

        def save(self, _checkpoint):
            raise AssertionError("save must not be reached")

    client = FakeClient()
    with pytest.raises(CheckpointStoreError, match="unavailable"):
        consumer.process_message(
            v2_event(),
            {"cluster-a": client},
            checkpoint_store=FailingCheckpointStore(),
            message_key=KAFKA_KEY,
            allow_legacy=False,
        )

    assert client.documents.upserted == []


def test_legacy_event_requires_explicit_compatibility_mode():
    payload = {
        "collection": "products",
        "target_cluster": "cluster-a",
        "document": {"id": "doc-1"},
    }
    client = FakeClient()

    with pytest.raises(EventEnvelopeError, match="Legacy indexing event rejected"):
        consumer.process_message(payload, {"cluster-a": client}, allow_legacy=False)
    consumer.process_message(payload, {"cluster-a": client}, allow_legacy=True)

    assert client.documents.upserted == [{"id": "doc-1"}]


def test_non_retryable_envelope_error_is_quarantined_once(monkeypatch):
    calls = []

    class FakeDlqProducer:
        def send(self, topic, value):
            calls.append((topic, value))

        def flush(self):
            return None

    original = consumer.process_message
    attempts = []

    def counting_process(*args, **kwargs):
        attempts.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(consumer, "process_message", counting_process)
    consumer.process_message_with_retries(
        v2_event(),
        {"cluster-a": FakeClient()},
        dlq_producer=FakeDlqProducer(),
        source_topic="imposbro_search_sharded_products",
        topic_prefix="imposbro_search_sharded",
        max_attempts=5,
        checkpoint_store=InMemoryCheckpointStore(),
        message_key=b"wrong-key",
        allow_legacy=False,
    )

    assert len(attempts) == 1
    assert calls[0][1]["error"] == "EventEnvelopeError"
