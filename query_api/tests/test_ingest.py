"""Tests for ingest endpoint validation and error handling."""
import os
import pytest
from unittest.mock import ANY, MagicMock, call

import typesense
from fastapi import HTTPException, Request

# Ensure test mode so we get mock federation
os.environ["TESTING"] = "1"


class _FakeDocumentRef:
    def __init__(self, document=None, error=None):
        self.document = document
        self.error = error

    def retrieve(self):
        if self.error:
            raise self.error
        if self.document is None:
            raise typesense.exceptions.ObjectNotFound("not found")
        return self.document


class _FakeDocuments:
    def __init__(self, documents=None, error=None):
        self.documents = documents or {}
        self.error = error

    def __getitem__(self, document_id):
        if self.error:
            return _FakeDocumentRef(error=self.error)
        return _FakeDocumentRef(self.documents.get(document_id))


class _FakeCollection:
    def __init__(self, documents):
        self.documents = documents


class _FakeCollections:
    def __init__(self, documents):
        self.documents = documents

    def __getitem__(self, collection_name):
        return _FakeCollection(self.documents)


class _FakeClient:
    def __init__(self, documents=None, error=None):
        self.documents = _FakeDocuments(documents, error=error)
        self.collections = _FakeCollections(self.documents)


def test_ingest_requires_id(client):
    """POST /ingest/{collection} without 'id' in body returns 400."""
    r = client.post("/ingest/products", json={"name": "No ID"})
    assert r.status_code == 400
    assert "id" in r.json().get("detail", "").lower()


def test_ingest_rejects_id_that_document_routes_cannot_address(client):
    """Accepted IDs must be safe for the matching GET/DELETE path contract."""
    r = client.post(
        "/ingest/products",
        json={"id": "tenant/doc 1", "name": "Product"},
    )

    assert r.status_code == 400
    assert "document 'id'" in r.json().get("detail", "").lower()
    client.app.state.kafka_service.publish_document.assert_not_called()


def test_ingest_rejects_non_string_id(client):
    r = client.post(
        "/ingest/products",
        json={"id": 123, "name": "Product"},
    )

    assert r.status_code == 400
    client.app.state.kafka_service.publish_document.assert_not_called()


def test_ingest_accepts_valid_document(client):
    """POST /ingest/{collection} with valid document returns 200 and routed_to."""
    r = client.post(
        "/ingest/products",
        json={"id": "doc-1", "name": "Product One", "region": "EU"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("status") == "ok"
    assert data.get("document_id") == "doc-1"
    assert "routed_to" in data


def test_ingest_propagates_request_id_to_kafka(client):
    """Ingest carries the HTTP request id into the async indexing message."""
    client.app.state.federation_service.get_targets_for_document = MagicMock(
        return_value=[(MagicMock(), "default-data-cluster")]
    )

    r = client.post(
        "/ingest/products",
        headers={
            "X-Request-ID": "trace-123",
            "X-Document-Version": "42",
            "X-Event-Sequence": "43",
            "Idempotency-Key": "ingest-retry-123",
            "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        },
        json={"id": "doc-1", "name": "Product One"},
    )

    assert r.status_code == 200
    assert r.headers["x-request-id"] == "trace-123"
    published = client.app.state.kafka_service.publish_document.call_args.kwargs
    assert published["request_id"] == "trace-123"
    assert published["traceparent"] == (
        "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    )
    assert published["document_version"] == 42
    assert published["sequence"] == 43
    assert published["idempotency_key"] == "ingest-retry-123"


def test_enterprise_event_metadata_fails_closed_when_headers_are_missing(monkeypatch):
    from routers.search import _indexing_event_metadata
    from settings import settings

    monkeypatch.setattr(settings, "DEPLOYMENT_PROFILE", "enterprise")
    request = Request({"type": "http", "headers": []})

    with pytest.raises(HTTPException) as exc_info:
        _indexing_event_metadata(request)

    assert exc_info.value.status_code == 428
    assert exc_info.value.detail["code"] == "document_version_required"


def test_batch_event_metadata_is_keyed_by_document_identity(client):
    client.app.state.federation_service.get_targets_for_document = MagicMock(
        return_value=[(MagicMock(), "default-data-cluster")]
    )

    response = client.post(
        "/ingest/products/batch",
        json={
            "documents": [{"id": "doc-1", "name": "Product One"}],
            "event_metadata": {
                "doc-1": {
                    "document_version": 8,
                    "sequence": 11,
                    "idempotency_key": "batch-retry-123",
                }
            },
        },
    )

    assert response.status_code == 200
    published = client.app.state.kafka_service.publish_document.call_args.kwargs
    assert published["document_version"] == 8
    assert published["sequence"] == 11
    assert published["idempotency_key"] == "batch-retry-123"


def test_batch_ingest_accepts_documents_and_propagates_request_id(client):
    """Batch ingest publishes one existing Kafka document message per document."""
    client.app.state.federation_service.get_targets_for_document = MagicMock(
        return_value=[(MagicMock(), "default-data-cluster")]
    )

    r = client.post(
        "/ingest/products/batch",
        headers={"X-Request-ID": "trace-batch"},
        json={
            "documents": [
                {"id": "doc-1", "name": "Product One"},
                {"id": "doc-2", "name": "Product Two"},
            ]
        },
    )

    assert r.status_code == 200
    assert r.headers["x-request-id"] == "trace-batch"
    assert r.json() == {
        "status": "ok",
        "requested": 2,
        "accepted": 2,
        "rejected": 0,
        "request_id": "trace-batch",
        "items": [
            {
                "index": 0,
                "document_id": "doc-1",
                "status": "ok",
                "routed_to": "default-data-cluster",
                "error": None,
            },
            {
                "index": 1,
                "document_id": "doc-2",
                "status": "ok",
                "routed_to": "default-data-cluster",
                "error": None,
            },
        ],
    }
    client.app.state.kafka_service.publish_document.assert_has_calls(
        [
            call(
                collection_name="products",
                document={"id": "doc-1", "name": "Product One"},
                target_clusters=["default-data-cluster"],
                tenant_id="development",
                document_version=ANY,
                sequence=ANY,
                routing_revision=1,
                rollout_id=None,
                idempotency_key=ANY,
                request_id="trace-batch",
                traceparent=ANY,
            ),
            call(
                collection_name="products",
                document={"id": "doc-2", "name": "Product Two"},
                target_clusters=["default-data-cluster"],
                tenant_id="development",
                document_version=ANY,
                sequence=ANY,
                routing_revision=1,
                rollout_id=None,
                idempotency_key=ANY,
                request_id="trace-batch",
                traceparent=ANY,
            ),
        ]
    )


def test_batch_ingest_rejects_oversized_batches(client, monkeypatch):
    """Batch size is operator-configurable and enforced before publishing."""
    from settings import settings

    monkeypatch.setattr(settings, "INGEST_BATCH_MAX_DOCUMENTS", 1)

    r = client.post(
        "/ingest/products/batch",
        json={
            "documents": [
                {"id": "doc-1", "name": "Product One"},
                {"id": "doc-2", "name": "Product Two"},
            ]
        },
    )

    assert r.status_code == 413
    assert "maximum is 1" in r.json()["detail"]
    client.app.state.kafka_service.publish_document.assert_not_called()


def test_batch_ingest_reports_partial_document_rejections(client):
    """Invalid documents are rejected per item while valid documents still publish."""
    client.app.state.federation_service.get_targets_for_document = MagicMock(
        return_value=[(MagicMock(), "default-data-cluster")]
    )

    r = client.post(
        "/ingest/products/batch",
        json={
            "documents": [
                {"id": "doc-1", "name": "Product One"},
                {"name": "Missing ID"},
            ]
        },
    )

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "partial"
    assert data["accepted"] == 1
    assert data["rejected"] == 1
    assert data["items"][0]["status"] == "ok"
    assert data["items"][1]["status"] == "rejected"
    assert "id" in data["items"][1]["error"].lower()
    assert client.app.state.kafka_service.publish_document.call_count == 1


def test_batch_ingest_reports_no_target_rejections(client):
    """No-target routing failures are per-document rejections in batch mode."""
    client.app.state.federation_service.get_targets_for_document = MagicMock(
        return_value=[]
    )

    r = client.post(
        "/ingest/products/batch",
        json={"documents": [{"id": "doc-1", "name": "Product One"}]},
    )

    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    assert r.json()["items"][0]["status"] == "rejected"
    assert "No target cluster available" in r.json()["items"][0]["error"]
    client.app.state.kafka_service.publish_document.assert_not_called()


def test_batch_ingest_publishes_one_logical_message_for_all_fanout_targets(client):
    """One event carries all targets so the worker can checkpoint fan-out."""
    client.app.state.federation_service.get_targets_for_document = MagicMock(
        return_value=[
            (MagicMock(), "cluster-a"),
            (MagicMock(), "cluster-b"),
        ]
    )

    r = client.post(
        "/ingest/products/batch",
        json={"documents": [{"id": "doc-1", "name": "Product One"}]},
    )

    assert r.status_code == 200
    assert r.json()["items"][0]["routed_to"] == "cluster-a,cluster-b"
    client.app.state.kafka_service.publish_document.assert_called_once_with(
        collection_name="products",
        document={"id": "doc-1", "name": "Product One"},
        target_clusters=["cluster-a", "cluster-b"],
        tenant_id="development",
        document_version=ANY,
        sequence=ANY,
        routing_revision=1,
        rollout_id=None,
        idempotency_key=ANY,
        request_id=r.json()["request_id"],
        traceparent=ANY,
    )


def test_get_document_returns_first_matching_candidate_cluster(client):
    """GET /documents/{collection}/{id} retrieves from candidate search clusters."""
    client.app.state.federation_service.get_named_clients_for_search = MagicMock(
        return_value=[
            ("cluster-a", _FakeClient()),
            ("cluster-b", _FakeClient({"doc-1": {"id": "doc-1", "name": "Product"}})),
        ]
    )

    r = client.get("/documents/products/doc-1")

    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "collection": "products",
        "document_id": "doc-1",
        "found_in": "cluster-b",
        "document": {"id": "doc-1", "name": "Product"},
    }


def test_get_document_returns_404_when_not_found(client):
    """GET document returns 404 when every candidate reports a miss."""
    client.app.state.federation_service.get_named_clients_for_search = MagicMock(
        return_value=[("cluster-a", _FakeClient())]
    )

    r = client.get("/documents/products/doc-1")

    assert r.status_code == 404
    assert r.json()["detail"] == "Document not found."


def test_get_document_returns_503_when_lookup_cannot_check_all_candidates(client):
    """A cluster failure is surfaced when no authoritative document result exists."""
    client.app.state.federation_service.get_named_clients_for_search = MagicMock(
        return_value=[
            ("cluster-a", _FakeClient(error=RuntimeError("down"))),
            ("cluster-b", _FakeClient()),
        ]
    )

    r = client.get("/documents/products/doc-1")

    assert r.status_code == 503
    assert "cluster-a" in r.json()["detail"]


def test_delete_document_publishes_delete_to_all_candidate_clusters(client):
    """DELETE /documents/{collection}/{id} fans out idempotent delete events."""
    client.app.state.federation_service.get_named_clients_for_delete = MagicMock(
        return_value=[("cluster-a", MagicMock()), ("cluster-b", MagicMock())]
    )

    r = client.delete(
        "/documents/products/doc-1",
        headers={"X-Request-ID": "trace-456"},
    )

    assert r.status_code == 200
    assert r.headers["x-request-id"] == "trace-456"
    assert r.json() == {
        "status": "ok",
        "document_id": "doc-1",
        "routed_to": "cluster-a,cluster-b",
    }
    client.app.state.kafka_service.publish_delete_document.assert_called_once_with(
        collection_name="products",
        document_id="doc-1",
        target_clusters=["cluster-a", "cluster-b"],
        tenant_id="development",
        document_version=ANY,
        sequence=ANY,
        routing_revision=1,
        rollout_id=None,
        idempotency_key=ANY,
        request_id="trace-456",
        traceparent=ANY,
        filter_by=None,
    )


def test_delete_document_requires_at_least_one_candidate_cluster(client):
    """Delete fails closed when no data cluster is available."""
    client.app.state.federation_service.get_named_clients_for_delete = MagicMock(
        return_value=[]
    )

    r = client.delete("/documents/products/doc-1")

    assert r.status_code == 503
    assert "No target cluster available" in r.json().get("detail", "")


def test_delete_document_rejects_unsafe_document_ids(client):
    """Document ids in path are constrained for safe filter construction."""
    r = client.delete("/documents/products/doc:1")

    assert r.status_code == 422


def test_get_document_rejects_unsafe_document_ids(client):
    """Read path uses the same conservative document id validation as delete."""
    r = client.get("/documents/products/doc:1")

    assert r.status_code == 422
