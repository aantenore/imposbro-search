"""Tests for search endpoint."""
import os

os.environ.setdefault("TESTING", "1")


def test_search_collection_not_found_returns_404(client):
    """GET /search/{collection} when no clusters have the collection returns 404."""
    # In test lifespan, get_clients_for_search returns [] so any collection yields 404
    r = client.get("/search/products?q=test&query_by=name")
    assert r.status_code == 404
    assert "not found" in r.json().get("detail", "").lower()


def test_search_invalid_collection_name_returns_422(client):
    """GET /search/{collection} with invalid name (e.g. special chars) returns 422."""
    r = client.get("/search/invalid!name?q=test&query_by=name")
    assert r.status_code == 422
