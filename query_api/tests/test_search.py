"""Tests for search endpoint."""
import os

os.environ.setdefault("TESTING", "1")


class FakeDocuments:
    def __init__(self, result):
        self.result = result
        self.params = None

    def search(self, params):
        self.params = params
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeMultiSearch:
    def __init__(self, result):
        self.result = result
        self.payload = None

    def perform(self, payload):
        self.payload = payload
        if isinstance(self.result, Exception):
            raise self.result
        return {"results": [self.result]}


class FakeCollection:
    def __init__(self, documents):
        self.documents = documents


class FakeCollections:
    def __init__(self, documents):
        self.documents = documents

    def __getitem__(self, collection_name):
        return FakeCollection(self.documents)


class FakeTypesenseClient:
    def __init__(self, result):
        self.documents = FakeDocuments(result)
        self.multi_search = FakeMultiSearch(result)
        self.collections = FakeCollections(self.documents)


class PaginatedDocuments:
    def __init__(self, hits):
        self.hits = hits
        self.params = None

    def search(self, params):
        self.params = params
        per_page = int(params.get("per_page", 10))
        return {
            "found": len(self.hits),
            "hits": self.hits[:per_page],
        }


class PaginatedTypesenseClient:
    def __init__(self, hits):
        self.documents = PaginatedDocuments(hits)
        self.collections = FakeCollections(self.documents)

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
    data = r.json()
    hits = data["hits"]
    assert len(hits) == 1
    assert hits[0]["document"]["price"] == 5
    assert data["found"] == 1
    assert data["raw_found"] == 2
    assert data["deduplicated_found"] == 1


def test_offset_search_fetches_extra_hit_for_next_offset(client):
    """Cursor pagination must ask shards for one extra hit to detect has_more."""
    hits = [
        {"document": {"id": "doc-1", "name": "First"}, "text_match": 100},
        {"document": {"id": "doc-2", "name": "Second"}, "text_match": 90},
    ]
    cluster = PaginatedTypesenseClient(hits)
    client.app.state.federation_service.get_named_clients_for_search.return_value = [
        ("cluster-a", cluster),
    ]

    r = client.get("/search/products?q=test&query_by=name&offset=0&limit=1")

    assert r.status_code == 200
    data = r.json()
    assert [hit["document"]["id"] for hit in data["hits"]] == ["doc-1"]
    assert data["has_more"] is True
    assert data["next_offset"] == 1
    assert cluster.documents.params["per_page"] == 2


def test_get_search_passes_advanced_params(client):
    """GET search exposes the same scalar tuning knobs as JSON-body search."""
    cluster = PaginatedTypesenseClient([
        {"document": {"id": "doc-1", "name": "First"}, "text_match": 100},
    ])
    client.app.state.federation_service.get_named_clients_for_search.return_value = [
        ("cluster-a", cluster),
    ]

    r = client.get(
        "/search/products"
        "?q=test&query_by=name"
        "&highlight_start_tag=<mark>"
        "&highlight_end_tag=</mark>"
        "&limit_hits=25"
        "&search_cutoff_ms=50"
        "&max_candidates=100"
        "&exhaustive_search=true"
    )

    assert r.status_code == 200
    params = cluster.documents.params
    assert params["highlight_start_tag"] == "<mark>"
    assert params["highlight_end_tag"] == "</mark>"
    assert params["limit_hits"] == 25
    assert params["search_cutoff_ms"] == 50
    assert params["max_candidates"] == 100
    assert params["exhaustive_search"] is True


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


def test_search_requires_query_by_or_vector_query(client):
    """Keyword searches still require query_by when vector_query is absent."""
    client.app.state.federation_service.get_named_clients_for_search.return_value = [
        (
            "cluster-ok",
            FakeTypesenseClient({"found": 0, "hits": []}),
        ),
    ]

    r = client.get("/search/products?q=test")

    assert r.status_code == 400
    assert "query_by" in r.json().get("detail", "")


def test_post_search_passes_vector_and_embedding_params(client):
    """POST /search supports long vector/hybrid parameters without query strings."""
    cluster = FakeTypesenseClient(
        {
            "found": 1,
            "hits": [
                {
                    "document": {"id": "vec-1", "name": "Vector One"},
                    "vector_distance": 0.12,
                }
            ],
        }
    )
    client.app.state.federation_service.get_named_clients_for_search.return_value = [
        ("cluster-vector", cluster),
    ]

    r = client.post(
        "/search/products",
        json={
            "q": "*",
            "vector_query": "embedding:([0.1,0.2,0.3], k:10, alpha: 0.8)",
            "exclude_fields": "embedding",
            "remote_embedding_timeout_ms": 2500,
            "limit": 10,
            "offset": 0,
        },
    )

    assert r.status_code == 200
    assert r.json()["hits"][0]["document"]["id"] == "vec-1"
    vector_payload = cluster.multi_search.payload["searches"][0]
    assert vector_payload["collection"] == "products"
    assert vector_payload["vector_query"] == (
        "embedding:([0.1,0.2,0.3], k:10, alpha: 0.8)"
    )
    assert vector_payload["exclude_fields"] == "embedding"
    assert vector_payload["remote_embedding_timeout_ms"] == 2500
    assert "query_by" not in vector_payload
    assert cluster.documents.params is None


def test_vector_search_merges_by_vector_distance_when_text_match_missing(client):
    """Vector-only federated results preserve nearest-neighbor ordering."""
    client.app.state.federation_service.get_named_clients_for_search.return_value = [
        (
            "cluster-far",
            FakeTypesenseClient(
                {
                    "found": 1,
                    "hits": [
                        {
                            "document": {"id": "far", "name": "Far"},
                            "vector_distance": 0.8,
                        },
                    ],
                }
            ),
        ),
        (
            "cluster-near",
            FakeTypesenseClient(
                {
                    "found": 1,
                    "hits": [
                        {
                            "document": {"id": "near", "name": "Near"},
                            "vector_distance": 0.1,
                        },
                    ],
                }
            ),
        ),
    ]

    r = client.post(
        "/search/products",
        json={
            "q": "*",
            "vector_query": "embedding:([0.1,0.2,0.3], k:10)",
            "limit": 10,
            "offset": 0,
        },
    )

    assert r.status_code == 200
    hits = r.json()["hits"]
    assert [hit["document"]["id"] for hit in hits] == ["near", "far"]
