"""Tests for search endpoint."""
import os

os.environ.setdefault("TESTING", "1")


class FakeDocuments:
    def __init__(self, result):
        self.result = result

    def search(self, params):
        self.params = params
        if isinstance(self.result, Exception):
            raise self.result
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
    client.app.state.federation_service.get_named_clients_for_search.return_value = [
        (
            "cluster-low",
            FakeTypesenseClient(
                {
                    "found": 1,
                    "hits": [
                        {"document": {"id": "low", "name": "Low"}, "text_match": 10},
                    ],
                }
            ),
        ),
        (
            "cluster-high",
            FakeTypesenseClient(
                {
                    "found": 1,
                    "hits": [
                        {"document": {"id": "high", "name": "High"}, "text_match": 100},
                    ],
                }
            ),
        ),
    ]

    r = client.get("/search/products?q=test&query_by=name")

    assert r.status_code == 200
    hits = r.json()["hits"]
    assert [hit["document"]["id"] for hit in hits] == ["high", "low"]


def test_search_respects_simple_global_sort_by(client):
    """Federated merge applies simple sort_by expressions after scatter."""
    client.app.state.federation_service.get_named_clients_for_search.return_value = [
        (
            "cluster-expensive",
            FakeTypesenseClient(
                {
                    "found": 1,
                    "hits": [
                        {
                            "document": {"id": "expensive", "name": "Expensive", "price": 20},
                            "text_match": 100,
                        },
                    ],
                }
            ),
        ),
        (
            "cluster-cheap",
            FakeTypesenseClient(
                {
                    "found": 1,
                    "hits": [
                        {
                            "document": {"id": "cheap", "name": "Cheap", "price": 5},
                            "text_match": 10,
                        },
                    ],
                }
            ),
        ),
    ]

    r = client.get("/search/products?q=test&query_by=name&sort_by=price:asc")

    assert r.status_code == 200
    hits = r.json()["hits"]
    assert [hit["document"]["id"] for hit in hits] == ["cheap", "expensive"]


def test_search_deduplicates_using_global_sort(client):
    """When a fanned-out document appears twice, keep the globally best hit."""
    client.app.state.federation_service.get_named_clients_for_search.return_value = [
        (
            "cluster-a",
            FakeTypesenseClient(
                {
                    "found": 1,
                    "hits": [
                        {
                            "document": {"id": "sku-1", "name": "A", "price": 20},
                            "text_match": 100,
                        },
                    ],
                }
            ),
        ),
        (
            "cluster-b",
            FakeTypesenseClient(
                {
                    "found": 1,
                    "hits": [
                        {
                            "document": {"id": "sku-1", "name": "B", "price": 5},
                            "text_match": 10,
                        },
                    ],
                }
            ),
        ),
    ]

    r = client.get("/search/products?q=test&query_by=name&sort_by=price:asc")

    assert r.status_code == 200
    hits = r.json()["hits"]
    assert len(hits) == 1
    assert hits[0]["document"]["price"] == 5


def test_search_partial_failure_is_visible(client):
    """A single failed shard is reported as partial instead of hidden."""
    client.app.state.federation_service.get_named_clients_for_search.return_value = [
        (
            "cluster-ok",
            FakeTypesenseClient(
                {
                    "found": 1,
                    "hits": [
                        {"document": {"id": "ok", "name": "OK"}, "text_match": 10},
                    ],
                }
            ),
        ),
        ("cluster-down", FakeTypesenseClient(RuntimeError("boom"))),
    ]

    r = client.get("/search/products?q=test&query_by=name")

    assert r.status_code == 200
    data = r.json()
    assert data["partial"] is True
    assert data["failed_clusters"] == ["cluster-down"]
    assert data["clusters_responded"] == 1


def test_search_all_clusters_failed_returns_503(client):
    """If no shard responds, returning an empty success would be misleading."""
    client.app.state.federation_service.get_named_clients_for_search.return_value = [
        ("cluster-a", FakeTypesenseClient(RuntimeError("a down"))),
        ("cluster-b", FakeTypesenseClient(RuntimeError("b down"))),
    ]

    r = client.get("/search/products?q=test&query_by=name")

    assert r.status_code == 503
    assert "all" in r.json().get("detail", "").lower()


def test_search_rejects_unmergeable_sort_by(client):
    """Complex shard-local sorts are rejected until the gateway can merge them exactly."""
    client.app.state.federation_service.get_named_clients_for_search.return_value = [
        (
            "cluster-ok",
            FakeTypesenseClient({"found": 0, "hits": []}),
        ),
    ]

    r = client.get(
        "/search/products?q=test&query_by=name&sort_by=location(48.1,2.1):asc"
    )

    assert r.status_code == 400
    assert "simple" in r.json().get("detail", "").lower()
