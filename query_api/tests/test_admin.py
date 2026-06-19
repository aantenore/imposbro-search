"""Tests for admin endpoints."""
import os

os.environ.setdefault("TESTING", "1")


def test_admin_requires_api_key_when_configured(client, monkeypatch):
    """Admin endpoints require X-API-Key or Bearer token when ADMIN_API_KEY is set."""
    from settings import settings

    monkeypatch.setattr(settings, "ADMIN_API_KEY", "admin-secret")

    missing = client.get("/admin/stats")
    assert missing.status_code == 401

    valid = client.get("/admin/stats", headers={"X-API-Key": "admin-secret"})
    assert valid.status_code == 200


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


def test_reconcile_collections_returns_cluster_report(client):
    """POST /admin/collections/reconcile exposes the schema reconciliation report."""
    client.app.state.federation_service.collection_schemas = {
        "products": {"name": "products", "fields": []}
    }
    client.app.state.federation_service.reconcile_collection_schemas.return_value = {
        "cluster-a": {"existing": [], "created": ["products"]}
    }

    r = client.post("/admin/collections/reconcile")

    assert r.status_code == 200
    data = r.json()
    assert data["collections_desired"] == 1
    assert data["clusters"]["cluster-a"]["created"] == ["products"]
