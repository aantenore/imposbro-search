"""Tests for internal state and audit persistence helpers."""
import sys
from pathlib import Path


_app_dir = Path(__file__).resolve().parent.parent / "app"
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

import pytest

from services.state_manager import (
    StateLoadError,
    StateManager,
    AUDIT_COLLECTION_NAME,
    STATE_DOCUMENT_ID,
)


class FakeDocuments:
    def __init__(self):
        self.upserted = []
        self.search_response = {"hits": []}
        self.retrieved = {}

    def upsert(self, document):
        self.upserted.append(document)

    def __getitem__(self, document_id):
        self.document_id = document_id
        return self

    def retrieve(self):
        return self.retrieved[self.document_id]

    def search(self, params):
        self.last_search_params = params
        return self.search_response


class FakeCollection:
    def __init__(self):
        self.documents = FakeDocuments()

    def retrieve(self):
        return {}


class FakeCollections:
    def __init__(self):
        self.collections = {}
        self.created = []

    def __getitem__(self, name):
        if name not in self.collections:
            self.collections[name] = FakeCollection()
        return self.collections[name]

    def create(self, schema):
        self.created.append(schema)
        self.collections[schema["name"]] = FakeCollection()


class FakeClient:
    def __init__(self):
        self.collections = FakeCollections()


def test_record_admin_audit_persists_safe_event():
    client = FakeClient()
    manager = StateManager(client)

    ok = manager.record_admin_audit(
        actor="api_key:abc123",
        action="collection_created",
        resource_type="collection",
        resource_id="products",
        details={"cluster_count": 2},
    )

    assert ok is True
    document = client.collections[AUDIT_COLLECTION_NAME].documents.upserted[0]
    assert document["actor"] == "api_key:abc123"
    assert document["action"] == "collection_created"
    assert document["details_json"] == '{"cluster_count": 2}'


def test_list_admin_audit_returns_parsed_details_and_filters():
    client = FakeClient()
    manager = StateManager(client)
    audit_documents = client.collections[AUDIT_COLLECTION_NAME].documents
    audit_documents.search_response = {
        "hits": [
            {
                "document": {
                    "id": "audit-1",
                    "timestamp_ms": 1,
                    "timestamp": "2026-06-19T00:00:00+00:00",
                    "actor": "api_key:abc123",
                    "action": "routing_updated",
                    "resource_type": "routing_rule",
                    "resource_id": "products",
                    "status": "success",
                    "details_json": '{"rules_count": 1}',
                }
            }
        ]
    }

    entries = manager.list_admin_audit(
        limit=10,
        action="routing_updated",
        resource_type="routing_rule",
    )

    assert entries[0]["details"] == {"rules_count": 1}
    assert audit_documents.last_search_params["filter_by"] == (
        "action:=routing_updated && resource_type:=routing_rule"
    )


def test_load_state_raises_on_corrupt_persisted_state():
    client = FakeClient()
    manager = StateManager(client)
    state_documents = client.collections["_imposbro_state"].documents
    state_documents.retrieved[STATE_DOCUMENT_ID] = {"state_data": "not-json"}

    with pytest.raises(StateLoadError):
        manager.load_state()
