"""Failure-ordering tests for desired-state-first provider operations."""

from unittest.mock import MagicMock

from control_plane import InMemoryControlPlaneStore
from services.federation import FederationService
from services.state_manager import StateManager


class FailingCollection:
    def __init__(self, owner, error):
        self.owner = owner
        self.error = error

    def delete(self):
        self.owner.delete_calls += 1
        if self.error:
            raise self.error
        return {"name": "deleted"}


class FailingCollections:
    def __init__(self, *, create_error=None, delete_error=None):
        self.create_error = create_error
        self.delete_error = delete_error
        self.created = []
        self.delete_calls = 0

    def create(self, schema):
        self.created.append(dict(schema))
        if self.create_error:
            raise self.create_error

    def __getitem__(self, _name):
        return FailingCollection(self, self.delete_error)


class FakeClient:
    def __init__(self, **kwargs):
        self.collections = FailingCollections(**kwargs)


def _install(client, federation, manager):
    client.app.state.federation_service = federation
    client.app.state.state_manager = manager
    client.app.state.config_notifier = MagicMock()


def test_collection_create_commits_desired_state_before_provider_failure(client):
    store = InMemoryControlPlaneStore()
    manager = StateManager(store=store)
    federation = FederationService()
    provider = FakeClient(create_error=RuntimeError("provider down"))
    federation.clients = {"cluster-a": provider}
    _install(client, federation, manager)

    response = client.post(
        "/api/v1/admin/collections",
        headers={"If-Match": '"0"'},
        json={
            "name": "products",
            "fields": [{"name": "name", "type": "string"}],
        },
    )

    assert response.status_code == 503
    assert response.json()["code"] == "collection_reconciliation_pending"
    assert manager.current_revision == 1
    persisted = store.load().state
    assert persisted["collection_schemas"]["products"]["name"] == "products"
    assert persisted["collection_routing_rules"]["products"].get("disabled") is not True
    assert store.list_audit()[0]["action"] == "collection_created"
    assert len(provider.collections.created) == 1


def test_collection_delete_tombstones_before_destructive_provider_failure(client):
    store = InMemoryControlPlaneStore()
    manager = StateManager(store=store)
    federation = FederationService()
    provider = FakeClient(delete_error=RuntimeError("provider down"))
    federation.clients = {"cluster-a": provider}
    federation.collection_schemas = {
        "products": {"name": "products", "fields": []}
    }
    federation.routing_rules = {
        "products": {
            "collection": "products",
            "rules": [],
            "default_cluster": "cluster-a",
        }
    }
    assert manager.save_state(
        federation.clusters_config,
        federation.routing_rules,
        federation.collection_schemas,
        federation.collection_aliases,
        federation.routing_rollouts,
    )
    federation.mark_applied_revision(manager.current_revision)
    _install(client, federation, manager)

    response = client.delete(
        "/api/v1/admin/collections/products",
        headers={"If-Match": '"1"'},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "collection_deletion_pending"
    assert manager.current_revision == 2
    assert federation.routing_rules["products"]["disabled"] is True
    assert "products" in federation.collection_schemas
    assert federation.get_named_clients_for_search("products") == []
    assert federation.get_targets_for_document("products", {"id": "1"}) == []
    persisted = store.load().state
    assert persisted["collection_routing_rules"]["products"]["disabled"] is True
    assert store.list_audit()[0]["action"] == "collection_deletion_started"


def test_successful_collection_delete_keeps_tombstone_after_finalization(client):
    store = InMemoryControlPlaneStore()
    manager = StateManager(store=store)
    federation = FederationService()
    provider = FakeClient()
    federation.clients = {"cluster-a": provider}
    federation.collection_schemas = {
        "products": {"name": "products", "fields": []}
    }
    federation.routing_rules = {
        "products": {
            "collection": "products",
            "rules": [],
            "default_cluster": "cluster-a",
        }
    }
    assert manager.save_state(
        federation.clusters_config,
        federation.routing_rules,
        federation.collection_schemas,
        federation.collection_aliases,
        federation.routing_rollouts,
    )
    _install(client, federation, manager)

    response = client.delete(
        "/api/v1/admin/collections/products",
        headers={"If-Match": '"1"'},
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 3
    assert provider.collections.delete_calls == 1
    assert "products" not in federation.collection_schemas
    assert federation.routing_rules["products"]["disabled"] is True
    assert [item["action"] for item in store.list_audit(limit=2)] == [
        "collection_deleted",
        "collection_deletion_started",
    ]
