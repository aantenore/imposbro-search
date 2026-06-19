"""Tests for admin endpoints."""
import json
import os
from unittest.mock import MagicMock

os.environ.setdefault("TESTING", "1")


def test_admin_requires_api_key_when_configured(client, monkeypatch):
    """Admin endpoints require X-API-Key or Bearer token when ADMIN_API_KEY is set."""
    from settings import settings

    monkeypatch.setattr(settings, "ADMIN_API_KEY", "admin-secret")

    missing = client.get("/admin/stats")
    assert missing.status_code == 401

    valid = client.get("/admin/stats", headers={"X-API-Key": "admin-secret"})
    assert valid.status_code == 200


def test_scoped_admin_key_grants_admin_access(client, monkeypatch):
    """SCOPED_API_KEYS can grant admin access without the legacy ADMIN_API_KEY."""
    from settings import settings

    monkeypatch.setattr(settings, "ADMIN_API_KEY", "")
    monkeypatch.setattr(settings, "SCOPED_API_KEYS", json.dumps([
        {"name": "operator", "key": "ops-secret", "scopes": ["admin"]}
    ]))
    monkeypatch.setattr(settings, "ALLOW_UNAUTHENTICATED_ADMIN", False)

    r = client.get("/admin/stats", headers={"Authorization": "Bearer ops-secret"})

    assert r.status_code == 200


def test_scoped_non_admin_key_cannot_access_admin(client, monkeypatch):
    """Data/search scoped keys are not accepted by admin endpoints."""
    from settings import settings

    monkeypatch.setattr(settings, "ADMIN_API_KEY", "")
    monkeypatch.setattr(settings, "SCOPED_API_KEYS", json.dumps([
        {"name": "reader", "key": "search-secret", "scopes": ["search"]}
    ]))
    monkeypatch.setattr(settings, "ALLOW_UNAUTHENTICATED_ADMIN", False)

    r = client.get("/admin/stats", headers={"X-API-Key": "search-secret"})

    assert r.status_code == 401


def test_admin_requires_api_key_when_no_dev_bypass(client, monkeypatch):
    """Admin endpoints are not public-by-default when no bypass is configured."""
    from settings import settings

    monkeypatch.setattr(settings, "ADMIN_API_KEY", "")
    monkeypatch.setattr(settings, "ALLOW_UNAUTHENTICATED_ADMIN", False)

    r = client.get("/admin/stats")

    assert r.status_code == 401
    assert "required" in r.json().get("detail", "").lower()


def test_delete_default_cluster_returns_400(client):
    """DELETE /admin/federation/clusters/default returns 400."""
    r = client.delete("/admin/federation/clusters/default")
    assert r.status_code == 400
    assert "default" in r.json().get("detail", "").lower()


def test_get_clusters_includes_default_and_returns_200(client):
    """GET /admin/federation/clusters returns 200 and includes virtual default entry."""
    r = client.get("/admin/federation/clusters")
    assert r.status_code == 200
    data = r.json()
    assert "default" in data
    assert data["default"].get("api_key") == "N/A"


def test_get_clusters_masks_registered_cluster_api_keys(client):
    """Public admin cluster listing never returns raw cluster API keys."""
    client.app.state.federation_service.clusters_config = {
        "cluster-a": {
            "name": "cluster-a",
            "host": "typesense-a",
            "port": 8108,
            "api_key": "super-secret-key",
        }
    }

    r = client.get("/admin/federation/clusters")

    assert r.status_code == 200
    data = r.json()
    assert data["cluster-a"]["api_key"] == "************-key"
    assert data["cluster-a"]["api_key"] != "super-secret-key"


def test_get_internal_clusters_returns_unmasked_registered_cluster_api_keys(client):
    """Internal service config endpoint returns raw keys for trusted workers."""
    client.app.state.federation_service.clusters_config = {
        "cluster-a": {
            "name": "cluster-a",
            "host": "typesense-a",
            "port": 8108,
            "api_key": "super-secret-key",
        }
    }

    r = client.get("/admin/federation/clusters/internal")

    assert r.status_code == 200
    data = r.json()
    assert data == {
        "cluster-a": {
            "name": "cluster-a",
            "host": "typesense-a",
            "port": 8108,
            "api_key": "super-secret-key",
        }
    }


def test_invalid_admin_path_names_return_422(client):
    """Cluster and collection path params use the same NAME_PATTERN guard."""
    bad_cluster = client.delete("/admin/federation/clusters/bad!name")
    bad_collection = client.delete("/admin/collections/bad!name")

    assert bad_cluster.status_code == 422
    assert bad_collection.status_code == 422


def test_invalid_collection_schema_name_returns_422(client):
    """Collection creation validates collection names before touching services."""
    r = client.post(
        "/admin/collections",
        json={
            "name": "bad!name",
            "fields": [{"name": "title", "type": "string"}],
        },
    )

    assert r.status_code == 422


def test_invalid_collection_field_name_returns_422(client):
    """Collection field names are validated server-side, not only in the UI."""
    r = client.post(
        "/admin/collections",
        json={
            "name": "products",
            "fields": [{"name": "bad field", "type": "string"}],
        },
    )

    assert r.status_code == 422


def test_invalid_collection_field_type_returns_422(client):
    """Unsupported Typesense field types are rejected by the API."""
    r = client.post(
        "/admin/collections",
        json={
            "name": "products",
            "fields": [{"name": "title", "type": "varchar"}],
        },
    )

    assert r.status_code == 422


def test_invalid_default_sorting_field_returns_422(client):
    """default_sorting_field must reference a declared numeric field."""
    r = client.post(
        "/admin/collections",
        json={
            "name": "products",
            "fields": [{"name": "title", "type": "string"}],
            "default_sorting_field": "title",
        },
    )

    assert r.status_code == 422


def test_admin_stats_returns_200(client):
    """GET /admin/stats returns 200 with clusters and collections."""
    r = client.get("/admin/stats")
    assert r.status_code == 200
    data = r.json()
    assert "clusters" in data
    assert "collections" in data
    assert "metrics_url" in data


def test_register_cluster_records_safe_audit_event(client, monkeypatch):
    """Admin mutations record an audit event without storing raw credentials."""
    from settings import settings

    monkeypatch.setattr(settings, "ADMIN_API_KEY", "admin-secret")

    r = client.post(
        "/admin/federation/clusters",
        headers={"X-API-Key": "admin-secret"},
        json={
            "name": "cluster-a",
            "host": "typesense-a",
            "port": 8108,
            "api_key": "raw-cluster-secret",
        },
    )

    assert r.status_code == 201
    audit_kwargs = client.app.state.state_manager.record_admin_audit.call_args.kwargs
    assert audit_kwargs["action"] == "cluster_registered"
    assert audit_kwargs["resource_type"] == "cluster"
    assert audit_kwargs["resource_id"] == "cluster-a"
    assert audit_kwargs["actor"].startswith("api_key:")
    assert "admin-secret" not in audit_kwargs["actor"]
    assert "raw-cluster-secret" not in str(audit_kwargs["details"])
    assert audit_kwargs["details"] == {
        "host": "typesense-a",
        "port": 8108,
        "collections_backfilled": 0,
    }


def test_get_audit_log_returns_recent_events(client):
    """GET /admin/audit-log exposes sanitized audit entries."""
    client.app.state.state_manager.list_admin_audit.return_value = [
        {
            "id": "audit-1",
            "timestamp_ms": 1,
            "timestamp": "2026-06-19T00:00:00+00:00",
            "actor": "api_key:abc123",
            "action": "collection_created",
            "resource_type": "collection",
            "resource_id": "products",
            "status": "success",
            "details": {"cluster_count": 2},
        }
    ]

    r = client.get("/admin/audit-log?limit=1&action=collection_created")

    assert r.status_code == 200
    assert r.json()["entries"][0]["resource_id"] == "products"
    client.app.state.state_manager.list_admin_audit.assert_called_once_with(
        limit=1,
        action="collection_created",
        resource_type=None,
    )


def test_state_export_masks_cluster_api_keys_by_default(client):
    """Control-plane state backups are inspectable without leaking cluster secrets."""
    client.app.state.federation_service.clusters_config = {
        "cluster-a": {
            "name": "cluster-a",
            "host": "typesense-a",
            "port": 8108,
            "api_key": "super-secret-key",
        }
    }
    client.app.state.federation_service.routing_rules = {
        "products": {"rules": [], "default_cluster": "default"}
    }
    client.app.state.federation_service.collection_schemas = {
        "products": {"name": "products", "fields": [{"name": "title", "type": "string"}]}
    }
    client.app.state.federation_service.collection_aliases = {
        "cluster-a": {"products_live": {"collection_name": "products"}}
    }

    r = client.get("/admin/state/export")

    assert r.status_code == 200
    data = r.json()
    assert data["version"] == "imposbro.state.v1"
    assert data["secrets_included"] is False
    assert data["federation_clusters_config"]["cluster-a"]["api_key"] == (
        "************-key"
    )
    assert data["collection_aliases"] == {
        "cluster-a": {"products_live": {"collection_name": "products"}}
    }
    assert "super-secret-key" not in json.dumps(data)


def test_state_export_can_include_raw_secrets_when_requested(client):
    """Restore-ready state exports require an explicit include_secrets opt-in."""
    client.app.state.federation_service.clusters_config = {
        "cluster-a": {
            "name": "cluster-a",
            "host": "typesense-a",
            "port": 8108,
            "api_key": "super-secret-key",
        }
    }

    r = client.get("/admin/state/export?include_secrets=true")

    assert r.status_code == 200
    data = r.json()
    assert data["secrets_included"] is True
    assert data["federation_clusters_config"]["cluster-a"]["api_key"] == (
        "super-secret-key"
    )
    audit_kwargs = client.app.state.state_manager.record_admin_audit.call_args.kwargs
    assert audit_kwargs["action"] == "state_exported"
    assert audit_kwargs["details"] == {
        "clusters": 1,
        "routing_rules": 0,
        "collection_schemas": 0,
        "collection_aliases": 0,
        "secrets_included": True,
    }
    assert "super-secret-key" not in str(audit_kwargs["details"])


def test_state_import_dry_run_validates_without_mutating(client):
    """State import defaults to dry-run and does not persist or reload runtime state."""
    snapshot = {
        "version": "imposbro.state.v1",
        "secrets_included": False,
        "federation_clusters_config": {
            "cluster-a": {
                "name": "cluster-a",
                "host": "typesense-a",
                "port": 8108,
                "api_key": "************-key",
            }
        },
        "collection_routing_rules": {
            "products": {"rules": [], "default_cluster": "default"}
        },
        "collection_schemas": {
            "products": {"name": "products", "fields": [{"name": "title", "type": "string"}]}
        },
    }

    r = client.post("/admin/state/import", json=snapshot)

    assert r.status_code == 200
    data = r.json()
    assert data["dry_run"] is True
    assert data["importable"] is False
    assert data["counts"] == {
        "clusters": 1,
        "routing_rules": 1,
        "collection_schemas": 1,
        "collection_aliases": 0,
    }
    client.app.state.state_manager.save_state.assert_not_called()
    client.app.state.federation_service.reload_from_state.assert_not_called()


def test_state_import_rejects_schema_name_mismatch(client):
    """Snapshot map key and schema.name must agree before import can proceed."""
    snapshot = {
        "version": "imposbro.state.v1",
        "secrets_included": True,
        "federation_clusters_config": {
            "cluster-a": {
                "name": "cluster-a",
                "host": "typesense-a",
                "port": 8108,
                "api_key": "raw-secret",
            }
        },
        "collection_routing_rules": {},
        "collection_schemas": {
            "products": {
                "name": "other",
                "fields": [{"name": "title", "type": "string"}],
            }
        },
    }

    r = client.post("/admin/state/import", json=snapshot)

    assert r.status_code == 400
    assert "mismatch" in r.json().get("detail", "").lower()


def test_state_import_rejects_routing_collection_mismatch(client):
    """Snapshot routing map key and routing collection must agree."""
    snapshot = {
        "version": "imposbro.state.v1",
        "secrets_included": True,
        "federation_clusters_config": {
            "cluster-a": {
                "name": "cluster-a",
                "host": "typesense-a",
                "port": 8108,
                "api_key": "raw-secret",
            }
        },
        "collection_routing_rules": {
            "products": {
                "collection": "other",
                "rules": [],
                "default_cluster": "default",
            }
        },
        "collection_schemas": {},
    }

    r = client.post("/admin/state/import", json=snapshot)

    assert r.status_code == 400
    assert "mismatch" in r.json().get("detail", "").lower()


def test_state_import_apply_rejects_masked_secrets(client):
    """Applying a masked snapshot would break restored cluster clients, so reject it."""
    snapshot = {
        "version": "imposbro.state.v1",
        "secrets_included": False,
        "federation_clusters_config": {
            "cluster-a": {
                "name": "cluster-a",
                "host": "typesense-a",
                "port": 8108,
                "api_key": "************-key",
            }
        },
        "collection_routing_rules": {},
        "collection_schemas": {},
    }

    r = client.post("/admin/state/import?apply=true", json=snapshot)

    assert r.status_code == 400
    assert "masked" in r.json().get("detail", "").lower()
    client.app.state.state_manager.save_state.assert_not_called()


def test_state_import_apply_persists_reloads_notifies_and_audits(client):
    """A restore-ready snapshot is persisted, loaded into runtime, broadcast, and audited."""
    client.app.state.state_manager.save_state.return_value = True
    fake_typesense = MagicMock()
    client.app.state.federation_service.get_client_for_cluster.return_value = (
        fake_typesense
    )
    snapshot = {
        "version": "imposbro.state.v1",
        "secrets_included": True,
        "federation_clusters_config": {
            "cluster-a": {
                "name": "cluster-a",
                "host": "typesense-a",
                "port": 8108,
                "api_key": "raw-secret",
            }
        },
        "collection_routing_rules": {
            "products": {"rules": [], "default_cluster": "default"}
        },
        "collection_schemas": {
            "products": {"name": "products", "fields": [{"name": "title", "type": "string"}]}
        },
        "collection_aliases": {
            "cluster-a": {"products_live": {"collection_name": "products"}}
        },
    }

    r = client.post("/admin/state/import?apply=true", json=snapshot)

    assert r.status_code == 200
    assert r.json()["dry_run"] is False
    client.app.state.state_manager.save_state.assert_called_once_with(
        snapshot["federation_clusters_config"],
        snapshot["collection_routing_rules"],
        snapshot["collection_schemas"],
        snapshot["collection_aliases"],
    )
    client.app.state.federation_service.reload_from_state.assert_called_once_with(
        snapshot["federation_clusters_config"],
        snapshot["collection_routing_rules"],
        snapshot["collection_schemas"],
        snapshot["collection_aliases"],
    )
    fake_typesense.aliases.upsert.assert_called_once_with(
        "products_live",
        {"collection_name": "products"},
    )
    client.app.state.federation_service.reconcile_collection_schemas.assert_called_once_with()
    client.app.state.config_notifier.notify.assert_called_once_with("state_imported")
    audit_kwargs = client.app.state.state_manager.record_admin_audit.call_args.kwargs
    assert audit_kwargs["action"] == "state_imported"
    assert audit_kwargs["details"] == {
        "clusters": 1,
        "routing_rules": 1,
        "collection_schemas": 1,
        "collection_aliases": 1,
    }
    assert "raw-secret" not in str(audit_kwargs)


def test_routing_update_rolls_back_runtime_state_when_persist_fails(client):
    """A failed control-plane save must not leave only this replica mutated."""
    from services.federation import FederationService

    federation = FederationService()
    federation.clients = {
        "cluster-a": MagicMock(),
        "cluster-b": MagicMock(),
    }
    federation.collection_schemas = {
        "products": {"name": "products", "fields": [{"name": "title", "type": "string"}]}
    }
    federation.routing_rules = {
        "products": {"rules": [], "default_cluster": "cluster-a"}
    }
    client.app.state.federation_service = federation
    client.app.state.state_manager.save_state.return_value = False

    r = client.post(
        "/admin/routing-rules",
        json={
            "collection": "products",
            "rules": [
                {
                    "field": "region",
                    "value": "eu",
                    "cluster": "cluster-b",
                }
            ],
            "default_cluster": "cluster-b",
        },
    )

    assert r.status_code == 500
    assert "rolled back" in r.json().get("detail", "")
    assert federation.routing_rules == {
        "products": {"rules": [], "default_cluster": "cluster-a"}
    }


def test_get_collection_schema_prefers_stored_desired_schema(client):
    """Schema reads use control-plane desired state when available."""
    client.app.state.federation_service.collection_schemas = {
        "products": {
            "name": "products",
            "fields": [{"name": "title", "type": "string", "facet": False}],
        }
    }

    r = client.get("/admin/collections/products")

    assert r.status_code == 200
    assert r.json()["fields"] == [
        {"name": "title", "type": "string", "facet": False}
    ]


def test_create_collection_preserves_vector_field_metadata(client):
    """Collection schemas keep Typesense vector metadata for hybrid/vector search."""
    r = client.post(
        "/admin/collections",
        json={
            "name": "semantic_products",
            "fields": [
                {"name": "title", "type": "string", "facet": False},
                {
                    "name": "embedding",
                    "type": "float[]",
                    "facet": False,
                    "num_dim": 3,
                    "embed": {
                        "from": ["title"],
                        "model_config": {"model_name": "ts/all-MiniLM-L12-v2"},
                    },
                },
            ],
        },
    )

    assert r.status_code == 201
    schema = client.app.state.federation_service.collection_schemas[
        "semantic_products"
    ]
    assert schema["fields"][1]["num_dim"] == 3
    assert schema["fields"][1]["embed"]["from"] == ["title"]
    created_schema = (
        client.app.state.federation_service.clients[
            "default-data-cluster"
        ].collections.create.call_args.args[0]
    )
    assert created_schema["fields"][1]["num_dim"] == 3
    assert created_schema["fields"][1]["embed"]["model_config"]["model_name"] == (
        "ts/all-MiniLM-L12-v2"
    )


def test_create_collection_omits_null_optional_field_metadata(client):
    """Optional field metadata is not serialized as null for Typesense schemas."""
    r = client.post(
        "/admin/collections",
        json={
            "name": "manual_vectors",
            "fields": [
                {"name": "title", "type": "string", "facet": False},
                {
                    "name": "embedding",
                    "type": "float[]",
                    "facet": False,
                    "num_dim": 3,
                },
            ],
        },
    )

    assert r.status_code == 201
    created_schema = (
        client.app.state.federation_service.clients[
            "default-data-cluster"
        ].collections.create.call_args.args[0]
    )
    assert "default_sorting_field" not in created_schema
    assert "num_dim" not in created_schema["fields"][0]
    assert "embed" not in created_schema["fields"][0]
    assert created_schema["fields"][1]["num_dim"] == 3
    assert "embed" not in created_schema["fields"][1]


def test_reconcile_collections_returns_cluster_report(client):
    """POST /admin/collections/reconcile exposes the schema reconciliation report."""
    client.app.state.federation_service.collection_schemas = {
        "products": {"name": "products", "fields": [{"name": "title", "type": "string"}]}
    }
    client.app.state.federation_service.reconcile_collection_schemas.return_value = {
        "cluster-a": {"existing": [], "created": ["products"]}
    }

    r = client.post("/admin/collections/reconcile")

    assert r.status_code == 200
    data = r.json()
    assert data["collections_desired"] == 1
    assert data["clusters"]["cluster-a"]["created"] == ["products"]


def test_upsert_alias_uses_typesense_alias_collection_api(client):
    """Alias upsert goes through Typesense's collection-level aliases API."""
    client.app.state.state_manager.save_state.return_value = True
    client.app.state.federation_service.collection_aliases = {}
    fake_typesense = MagicMock()
    client.app.state.federation_service.get_client_for_cluster.return_value = (
        fake_typesense
    )

    r = client.put(
        "/admin/aliases/products_live"
        "?collection_name=products_v2&cluster_name=cluster-a"
    )

    assert r.status_code == 200
    fake_typesense.aliases.upsert.assert_called_once_with(
        "products_live",
        {"collection_name": "products_v2"},
    )
    assert client.app.state.federation_service.collection_aliases == {
        "cluster-a": {"products_live": {"collection_name": "products_v2"}}
    }
    client.app.state.state_manager.save_state.assert_called_once_with(
        client.app.state.federation_service.clusters_config,
        client.app.state.federation_service.routing_rules,
        client.app.state.federation_service.collection_schemas,
        {"cluster-a": {"products_live": {"collection_name": "products_v2"}}},
    )
    client.app.state.config_notifier.notify.assert_called_once_with(
        "alias_upserted:cluster-a:products_live"
    )
    audit_kwargs = client.app.state.state_manager.record_admin_audit.call_args.kwargs
    assert audit_kwargs["action"] == "alias_upserted"
    assert audit_kwargs["resource_type"] == "alias"
    assert audit_kwargs["resource_id"] == "products_live"
    assert audit_kwargs["details"] == {
        "collection_name": "products_v2",
        "cluster_name": "cluster-a",
    }


def test_delete_alias_removes_persisted_desired_alias(client):
    """Alias delete removes the desired alias binding from persisted control-plane state."""
    client.app.state.state_manager.save_state.return_value = True
    client.app.state.federation_service.collection_aliases = {
        "cluster-a": {"products_live": {"collection_name": "products_v2"}}
    }
    fake_typesense = MagicMock()
    client.app.state.federation_service.get_client_for_cluster.return_value = (
        fake_typesense
    )

    r = client.delete("/admin/aliases/products_live?cluster_name=cluster-a")

    assert r.status_code == 200
    fake_typesense.aliases.__getitem__.assert_called_once_with("products_live")
    fake_typesense.aliases.__getitem__.return_value.delete.assert_called_once_with()
    assert client.app.state.federation_service.collection_aliases == {}
    client.app.state.state_manager.save_state.assert_called_once_with(
        client.app.state.federation_service.clusters_config,
        client.app.state.federation_service.routing_rules,
        client.app.state.federation_service.collection_schemas,
        {},
    )
    client.app.state.config_notifier.notify.assert_called_once_with(
        "alias_deleted:cluster-a:products_live"
    )


def test_set_routing_rules_omits_null_fanout_field(client):
    """Single-cluster rules must not be serialized with clusters=None."""
    client.app.state.federation_service.collection_schemas = {
        "products": {"name": "products", "fields": [{"name": "title", "type": "string"}]}
    }

    r = client.post(
        "/admin/routing-rules",
        json={
            "collection": "products",
            "rules": [
                {
                    "field": "region",
                    "value": "eu",
                    "cluster": "default-data-cluster",
                }
            ],
            "default_cluster": "default",
        },
    )

    assert r.status_code == 201
    rules = client.app.state.federation_service.set_routing_rules.call_args.kwargs[
        "rules"
    ]
    assert rules == [
        {
            "field": "region",
            "value": "eu",
            "cluster": "default-data-cluster",
        }
    ]
