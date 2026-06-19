"""Tests for data-plane API authentication."""


def test_data_endpoints_require_api_key_when_configured(client, monkeypatch):
    """Search and ingest require DATA_API_KEY when configured."""
    from settings import settings

    monkeypatch.setattr(settings, "DATA_API_KEY", "data-secret")
    monkeypatch.setattr(settings, "ALLOW_UNAUTHENTICATED_DATA", False)

    search = client.get("/search/products?q=test&query_by=name")
    ingest = client.post("/ingest/products", json={"id": "doc-1", "name": "Product"})

    assert search.status_code == 401
    assert ingest.status_code == 401


def test_data_endpoints_accept_x_api_key(client, monkeypatch):
    """X-API-Key grants access to data-plane endpoints only when it matches DATA_API_KEY."""
    from settings import settings

    monkeypatch.setattr(settings, "DATA_API_KEY", "data-secret")
    monkeypatch.setattr(settings, "ALLOW_UNAUTHENTICATED_DATA", False)

    r = client.post(
        "/ingest/products",
        headers={"X-API-Key": "data-secret"},
        json={"id": "doc-1", "name": "Product"},
    )

    assert r.status_code == 200
    assert r.json()["document_id"] == "doc-1"


def test_data_endpoints_accept_bearer_token(client, monkeypatch):
    """Authorization: Bearer is supported for data-plane API clients."""
    from settings import settings

    monkeypatch.setattr(settings, "DATA_API_KEY", "data-secret")
    monkeypatch.setattr(settings, "ALLOW_UNAUTHENTICATED_DATA", False)

    r = client.get(
        "/search/products?q=test&query_by=name",
        headers={"Authorization": "Bearer data-secret"},
    )

    assert r.status_code == 404
