"""Tests for federation service state and schema reconciliation."""
import sys
from pathlib import Path


_app_dir = Path(__file__).resolve().parent.parent / "app"
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

import typesense

from services.federation import FederationService


class FakeCollectionHandle:
    def __init__(self, collections, name):
        self.collections = collections
        self.name = name

    def retrieve(self):
        if self.name not in self.collections.schemas:
            raise typesense.exceptions.ObjectNotFound("not found")
        return self.collections.schemas[self.name]


class FakeCollections:
    def __init__(self, existing=None):
        self.schemas = dict(existing or {})
        self.created = []

    def __getitem__(self, name):
        return FakeCollectionHandle(self, name)

    def create(self, schema):
        if schema["name"] in self.schemas:
            raise typesense.exceptions.ObjectAlreadyExists("already exists")
        self.schemas[schema["name"]] = schema
        self.created.append(schema["name"])


class FakeClient:
    def __init__(self, existing=None):
        self.collections = FakeCollections(existing)


def test_backfill_collection_schemas_creates_missing_collections():
    federation = FederationService()
    federation.collection_schemas = {
        "products": {
            "name": "products",
            "fields": [{"name": "title", "type": "string"}],
        }
    }
    federation.clients = {"cluster-new": FakeClient()}

    created = federation.backfill_collection_schemas("cluster-new")

    assert created == ["products"]
    assert "products" in federation.clients["cluster-new"].collections.schemas


def test_backfill_collection_schemas_is_idempotent_for_existing_collections():
    schema = {
        "name": "products",
        "fields": [{"name": "title", "type": "string"}],
    }
    federation = FederationService()
    federation.collection_schemas = {"products": schema}
    federation.clients = {"cluster-new": FakeClient({"products": schema})}

    created = federation.backfill_collection_schemas("cluster-new")

    assert created == []
    assert federation.clients["cluster-new"].collections.created == []


def test_reconcile_collection_schemas_reports_existing_and_created():
    schema = {
        "name": "products",
        "fields": [{"name": "title", "type": "string"}],
    }
    federation = FederationService()
    federation.collection_schemas = {"products": schema}
    federation.clients = {
        "cluster-existing": FakeClient({"products": schema}),
        "cluster-missing": FakeClient(),
    }

    report = federation.reconcile_collection_schemas()

    assert report["cluster-existing"] == {"existing": ["products"], "created": []}
    assert report["cluster-missing"] == {"existing": [], "created": ["products"]}
