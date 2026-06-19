"""Tests for search endpoint."""
import os

os.environ.setdefault("TESTING", "1")


class FakeDocuments:
    def __init__(self, result):
        self.result = result

    def search(self, params):
        return self.result


class FakeCollection:
    def __init__(self, result):
        self.documents = FakeDocuments(result)


class FakeCollections:
    def __init__(self, result):
        self.result = result

    def __getitem__(self, collection_name):
        return FakeCollection(self.result)


class FakeTypesenseClient:
    def __init__(self, result):
        self.collections = FakeCollections(result)


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


def test_search_merge_sorts_higher_text_match_first(client):
    """Merged federated results preserve Typesense _text_match desc semantics."""
    client.app.state.federation_service.get_clients_for_search.return_value = [
        FakeTypesenseClient(
            {
                "found": 1,
                "hits": [
                    {"document": {"id": "low", "name": "Low"}, "text_match": 10},
                ],
            }
        ),
        FakeTypesenseClient(
            {
                "found": 1,
                "hits": [
                    {"document": {"id": "high", "name": "High"}, "text_match": 100},
                ],
            }
        ),
    ]

    r = client.get("/search/products?q=test&query_by=name")

    assert r.status_code == 200
    hits = r.json()["hits"]
    assert [hit["document"]["id"] for hit in hits] == ["high", "low"]
