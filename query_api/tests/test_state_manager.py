"""Tests for internal state and audit persistence helpers."""
import json
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
from control_plane import AuditEvent, InMemoryControlPlaneStore, StateConflictError
from secret_resolver import EnvSecretProvider, SecretResolutionError, SecretResolver


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


def test_load_state_defaults_legacy_snapshots_without_collection_schemas_or_aliases():
    client = FakeClient()
    manager = StateManager(client)
    state_documents = client.collections["_imposbro_state"].documents
    state_documents.retrieved[STATE_DOCUMENT_ID] = {
        "state_data": json.dumps(
            {
                "federation_clusters_config": {},
                "collection_routing_rules": {},
            }
        )
    }

    (
        clusters_config,
        routing_rules,
        collection_schemas,
        collection_aliases,
    ) = manager.load_state()

    assert clusters_config == {}
    assert routing_rules == {}
    assert collection_schemas == {}
    assert collection_aliases == {}


def test_state_snapshot_round_trips_collection_schemas_and_aliases():
    client = FakeClient()
    manager = StateManager(client)
    schema = {
        "name": "products",
        "fields": [{"name": "title", "type": "string", "facet": False}],
    }

    ok = manager.save_state(
        {"cluster-a": {"host": "typesense-a", "port": 8108, "api_key": "secret"}},
        {"products": {"rules": [], "default_cluster": "default"}},
        {"products": schema},
        {"cluster-a": {"products_live": {"collection_name": "products"}}},
    )

    assert ok is True
    state_documents = client.collections["_imposbro_state"].documents
    saved = json.loads(state_documents.upserted[-1]["state_data"])
    assert saved["collection_schemas"] == {"products": schema}
    assert saved["collection_aliases"] == {
        "cluster-a": {"products_live": {"collection_name": "products"}}
    }

    state_documents.retrieved[STATE_DOCUMENT_ID] = {
        "state_data": json.dumps(saved),
    }
    (
        clusters_config,
        routing_rules,
        collection_schemas,
        collection_aliases,
    ) = manager.load_state()

    assert "cluster-a" in clusters_config
    assert "products" in routing_rules
    assert collection_schemas == {"products": schema}
    assert collection_aliases == {
        "cluster-a": {"products_live": {"collection_name": "products"}}
    }


def test_transactional_state_manager_commits_revision_audit_and_outbox_together():
    store = InMemoryControlPlaneStore()
    manager = StateManager(store=store)

    saved = manager.save_state(
        {"cluster-a": {"host": "a", "api_key": "development-key"}},
        {"products": {"rules": [], "default_cluster": "cluster-a"}},
        audit=AuditEvent(
            actor="oidc:test",
            action="routing_updated",
            resource_type="routing_rule",
            resource_id="products",
            request_id="request-1",
        ),
        event_type="routing.updated",
        event_payload={"collection": "products"},
    )

    assert saved is True
    assert manager.current_revision == 1
    assert manager.authoritative_revision() == 1
    assert store.list_audit()[0]["request_id"] == "request-1"
    assert store.list_outbox()[0].payload["collection"] == "products"
    clusters, routing, schemas, aliases = manager.load_state()
    assert clusters == {
        "cluster-a": {"host": "a", "api_key": "development-key"}
    }
    assert "products" in routing
    assert schemas == {}
    assert aliases == {}


def test_transactional_state_manager_propagates_stale_writer_conflict():
    store = InMemoryControlPlaneStore()
    first = StateManager(store=store)
    stale = StateManager(store=store)
    audit = AuditEvent(
        actor="oidc:test",
        action="state_updated",
        resource_type="control_plane_state",
        resource_id="current",
    )

    assert first.save_state({}, {}, audit=audit)
    with pytest.raises(StateConflictError) as exc_info:
        stale.save_state(
            {"cluster-b": {"host": "b", "api_key": "development-key"}},
            {},
            audit=audit,
        )

    assert exc_info.value.expected_revision == 0
    assert exc_info.value.actual_revision == 1
    assert store.load().state["federation_clusters_config"] == {}


def test_transactional_state_manager_round_trips_rollouts_and_appends_audit():
    store = InMemoryControlPlaneStore()
    manager = StateManager(store=store)
    rollouts = {
        "rollout-1": {
            "rollout_id": "rollout-1",
            "collection": "products",
            "phase": "draft",
        }
    }
    assert manager.save_state({}, {}, routing_rollouts=rollouts)
    manager.load_state()

    assert manager.routing_rollouts == rollouts
    assert manager.record_admin_audit(
        actor="oidc:test",
        action="state_read",
        resource_type="control_plane_state",
        resource_id="current",
    ) is True
    assert store.list_audit(limit=2)[0]["action"] == "state_read"


def test_enterprise_state_persists_reference_and_rejects_inline_secret():
    store = InMemoryControlPlaneStore()
    resolver = SecretResolver(
        {"env": EnvSecretProvider({"DATA_CLUSTER_KEY": "raw-value"})}
    )
    manager = StateManager(
        store=store,
        secret_resolver=resolver,
        allow_inline_cluster_secrets=False,
    )
    audit = AuditEvent(
        actor="oidc:test",
        action="cluster_registered",
        resource_type="cluster",
        resource_id="cluster-a",
    )

    assert manager.save_state(
        {
            "cluster-a": {
                "host": "node-a",
                "api_key_ref": "env:DATA_CLUSTER_KEY",
            }
        },
        {},
        audit=audit,
    )
    persisted = store.load().state
    assert persisted["federation_clusters_config"]["cluster-a"] == {
        "host": "node-a",
        "api_key_ref": "env:DATA_CLUSTER_KEY",
    }
    assert "raw-value" not in json.dumps(persisted)

    with pytest.raises(SecretResolutionError, match="development"):
        manager.save_state(
            {"cluster-b": {"host": "node-b", "api_key": "raw-value"}},
            {},
            audit=audit,
        )
