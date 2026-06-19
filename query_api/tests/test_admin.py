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
