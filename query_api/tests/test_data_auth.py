"""Tests for data-plane API authentication."""
import json
from unittest.mock import MagicMock


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


def test_scoped_search_key_cannot_ingest(client, monkeypatch):
    """A search-scoped key can search but cannot publish ingest messages."""
    from settings import settings

    monkeypatch.setattr(settings, "DATA_API_KEY", "")
    monkeypatch.setattr(settings, "SCOPED_API_KEYS", json.dumps([
        {"name": "reader", "key": "search-secret", "scopes": ["search"]}
    ]))
    monkeypatch.setattr(settings, "ALLOW_UNAUTHENTICATED_DATA", False)

    search = client.get(
        "/search/products?q=test&query_by=name",
        headers={"X-API-Key": "search-secret"},
    )
    ingest = client.post(
        "/ingest/products",
        headers={"X-API-Key": "search-secret"},
        json={"id": "doc-1", "name": "Product"},
    )

    assert search.status_code == 404
    assert ingest.status_code == 401


def test_scoped_ingest_key_cannot_search(client, monkeypatch):
    """An ingest-scoped key can ingest but cannot query search endpoints."""
    from settings import settings

    monkeypatch.setattr(settings, "DATA_API_KEY", "")
    monkeypatch.setattr(settings, "SCOPED_API_KEYS", json.dumps([
        {"name": "writer", "key": "ingest-secret", "scopes": ["ingest"]}
    ]))
    monkeypatch.setattr(settings, "ALLOW_UNAUTHENTICATED_DATA", False)

    ingest = client.post(
        "/ingest/products",
        headers={"X-API-Key": "ingest-secret"},
        json={"id": "doc-1", "name": "Product"},
    )
    search = client.get(
        "/search/products?q=test&query_by=name",
        headers={"X-API-Key": "ingest-secret"},
    )

    assert ingest.status_code == 200
    assert search.status_code == 401


def test_scoped_data_key_grants_search_and_ingest(client, monkeypatch):
    """The coarse data scope remains available for simple data-plane clients."""
    from settings import settings

    monkeypatch.setattr(settings, "DATA_API_KEY", "")
    monkeypatch.setattr(settings, "SCOPED_API_KEYS", json.dumps([
        {"name": "pipeline", "key": "data-secret", "scopes": ["data"]}
    ]))
    monkeypatch.setattr(settings, "ALLOW_UNAUTHENTICATED_DATA", False)

    search = client.get(
        "/search/products?q=test&query_by=name",
        headers={"Authorization": "Bearer data-secret"},
    )
    ingest = client.post(
        "/ingest/products",
        headers={"Authorization": "Bearer data-secret"},
        json={"id": "doc-1", "name": "Product"},
    )

    assert search.status_code == 404
    assert ingest.status_code == 200


def test_collection_scoped_search_key_only_grants_matching_collection(client, monkeypatch):
    """Collection-scoped search keys use safe glob patterns for least privilege."""
    from settings import settings

    monkeypatch.setattr(settings, "DATA_API_KEY", "")
    monkeypatch.setattr(settings, "SCOPED_API_KEYS", json.dumps([
        {
            "name": "catalog-reader",
            "key": "products-secret",
            "scopes": ["search:products_*"],
        }
    ]))
    monkeypatch.setattr(settings, "ALLOW_UNAUTHENTICATED_DATA", False)

    matching = client.get(
        "/search/products_2026?q=test&query_by=name",
        headers={"X-API-Key": "products-secret"},
    )
    denied = client.get(
        "/search/orders_2026?q=test&query_by=name",
        headers={"X-API-Key": "products-secret"},
    )

    assert matching.status_code == 404
    assert denied.status_code == 401


def test_collection_scoped_ingest_key_only_grants_matching_collection(client, monkeypatch):
    """Collection-scoped ingest keys cannot write to unrelated collections."""
    from settings import settings

    monkeypatch.setattr(settings, "DATA_API_KEY", "")
    monkeypatch.setattr(settings, "SCOPED_API_KEYS", json.dumps([
        {
            "name": "orders-writer",
            "key": "orders-secret",
            "scopes": ["ingest:orders_*"],
        }
    ]))
    monkeypatch.setattr(settings, "ALLOW_UNAUTHENTICATED_DATA", False)
    client.app.state.federation_service.get_targets_for_document = MagicMock(
        return_value=[(MagicMock(), "default-data-cluster")]
    )

    matching = client.post(
        "/ingest/orders_2026",
        headers={"X-API-Key": "orders-secret"},
        json={"id": "doc-1", "name": "Order"},
    )
    denied = client.post(
        "/ingest/products_2026",
        headers={"X-API-Key": "orders-secret"},
        json={"id": "doc-2", "name": "Product"},
    )

    assert matching.status_code == 200
    assert denied.status_code == 401


def test_invalid_scoped_api_keys_configuration_fails_closed(client, monkeypatch):
    """Malformed scoped key config fails closed instead of opening data endpoints."""
    from settings import settings

    monkeypatch.setattr(settings, "DATA_API_KEY", "")
    monkeypatch.setattr(settings, "SCOPED_API_KEYS", "not-json")
    monkeypatch.setattr(settings, "ALLOW_UNAUTHENTICATED_DATA", True)

    r = client.get("/search/products?q=test&query_by=name")

    assert r.status_code == 500
    assert "SCOPED_API_KEYS" in r.json().get("detail", "")
