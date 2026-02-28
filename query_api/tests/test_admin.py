"""Tests for admin endpoints."""
import os

os.environ.setdefault("TESTING", "1")


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
