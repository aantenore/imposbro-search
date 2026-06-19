"""Tests for optional data-plane rate limiting."""
from unittest.mock import MagicMock


def _enable_memory_rate_limit(monkeypatch, *, search_limit=1, ingest_limit=1):
    from settings import settings

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_BACKEND", "memory")
    monkeypatch.setattr(settings, "RATE_LIMIT_WINDOW_SECONDS", 60)
    monkeypatch.setattr(settings, "RATE_LIMIT_SEARCH_REQUESTS", search_limit)
    monkeypatch.setattr(settings, "RATE_LIMIT_INGEST_REQUESTS", ingest_limit)
    monkeypatch.setattr(settings, "RATE_LIMIT_FAIL_CLOSED", False)


def test_search_rate_limit_returns_429_after_configured_budget(client, monkeypatch):
    """Search endpoints enforce the configured per-identity window."""
    _enable_memory_rate_limit(monkeypatch, search_limit=1)

    first = client.get("/search/products?q=test&query_by=name")
    second = client.get("/search/products?q=test&query_by=name")

    assert first.status_code == 404
    assert second.status_code == 429
    assert second.json()["detail"] == "Rate limit exceeded"
    assert second.headers["x-ratelimit-limit"] == "1"
    assert second.headers["x-ratelimit-remaining"] == "0"
    assert int(second.headers["retry-after"]) >= 1


def test_ingest_rate_limit_returns_429_after_configured_budget(client, monkeypatch):
    """Ingest endpoints are limited independently from search."""
    _enable_memory_rate_limit(monkeypatch, ingest_limit=1)
    client.app.state.federation_service.get_targets_for_document = MagicMock(
        return_value=[(MagicMock(), "default-data-cluster")]
    )

    first = client.post(
        "/ingest/products",
        json={"id": "doc-1", "name": "Product"},
    )
    second = client.post(
        "/ingest/products",
        json={"id": "doc-2", "name": "Product"},
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"] == "Rate limit exceeded"


def test_rate_limit_uses_authenticated_actor_buckets(client, monkeypatch):
    """Different API-key actors receive separate buckets."""
    from settings import settings

    _enable_memory_rate_limit(monkeypatch, search_limit=1)
    monkeypatch.setattr(settings, "ALLOW_UNAUTHENTICATED_DATA", False)
    monkeypatch.setattr(
        settings,
        "SCOPED_API_KEYS",
        (
            '[{"name":"reader-a","key":"search-a","scopes":["search"]},'
            '{"name":"reader-b","key":"search-b","scopes":["search"]}]'
        ),
    )

    first = client.get(
        "/search/products?q=test&query_by=name",
        headers={"X-API-Key": "search-a"},
    )
    second_same_actor = client.get(
        "/search/products?q=test&query_by=name",
        headers={"X-API-Key": "search-a"},
    )
    first_other_actor = client.get(
        "/search/products?q=test&query_by=name",
        headers={"X-API-Key": "search-b"},
    )

    assert first.status_code == 404
    assert second_same_actor.status_code == 429
    assert first_other_actor.status_code == 404


def test_rate_limit_fail_open_when_backend_unavailable(client, monkeypatch):
    """Backend outages do not break data-plane traffic unless fail-closed is enabled."""
    _enable_memory_rate_limit(monkeypatch, search_limit=1)
    from settings import settings
    from services import FixedWindowRateLimiter

    monkeypatch.setattr(settings, "RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setattr(settings, "RATE_LIMIT_FAIL_CLOSED", False)
    client.app.state.rate_limiter = FixedWindowRateLimiter("redis://localhost:1")

    r = client.get("/search/products?q=test&query_by=name")

    assert r.status_code == 404


def test_rate_limit_can_fail_closed_when_backend_unavailable(client, monkeypatch):
    """Operators can choose fail-closed behavior for stricter environments."""
    _enable_memory_rate_limit(monkeypatch, search_limit=1)
    from settings import settings
    from services import FixedWindowRateLimiter

    monkeypatch.setattr(settings, "RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setattr(settings, "RATE_LIMIT_FAIL_CLOSED", True)
    client.app.state.rate_limiter = FixedWindowRateLimiter("redis://localhost:1")

    r = client.get("/search/products?q=test&query_by=name")

    assert r.status_code == 503
    assert r.json()["detail"] == "Rate-limit backend unavailable"
