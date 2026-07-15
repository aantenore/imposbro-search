"""Tests for health and root endpoints."""
import threading
import time

import pytest
import typesense
from pydantic import ValidationError

from services import FederationService
from settings import Settings


class FailingCollections:
    def retrieve(self):
        raise typesense.exceptions.ServiceUnavailable("not ready")


class FailingTypesenseClient:
    collections = FailingCollections()


def test_root_returns_200(client):
    r = client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert data.get("service") == "IMPOSBRO Federated Search API"
    assert data.get("status") == "healthy"
    assert "version" in data


def test_state_client_uses_configured_https_protocol(monkeypatch):
    import main

    captured = {}
    monkeypatch.setattr(main.settings, "INTERNAL_STATE_PROTOCOL", "https")
    monkeypatch.setattr(
        main.typesense,
        "Client",
        lambda config: captured.update(config) or object(),
    )

    main.create_state_client()

    assert captured["nodes"]
    assert {node["protocol"] for node in captured["nodes"]} == {"https"}


def test_request_id_header_is_generated(client):
    r = client.get("/")

    assert r.status_code == 200
    assert len(r.headers["x-request-id"]) == 32


def test_request_id_header_preserves_valid_inbound_value(client):
    r = client.get("/", headers={"X-Request-ID": "trace-123"})

    assert r.status_code == 200
    assert r.headers["x-request-id"] == "trace-123"


def test_request_id_header_replaces_invalid_inbound_value(client):
    r = client.get("/", headers={"X-Request-ID": "invalid value"})

    assert r.status_code == 200
    assert r.headers["x-request-id"] != "invalid value"
    assert len(r.headers["x-request-id"]) == 32


def test_traceparent_is_preserved_or_safely_regenerated(client):
    valid = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    preserved = client.get("/", headers={"traceparent": valid})
    generated = client.get("/", headers={"traceparent": "malformed"})

    assert preserved.headers["traceparent"] == valid
    replacement = generated.headers["traceparent"]
    assert replacement != "malformed"
    assert replacement.startswith("00-")
    assert len(replacement) == 55


def test_health_returns_200(client, monkeypatch):
    import main

    monkeypatch.setattr(main, "_check_redis_ok", lambda: True)
    monkeypatch.setattr(main, "_check_kafka_ok", lambda: True)

    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "healthy"
    assert data["ready"] is True
    assert data["readiness_policy"] == "serving"
    assert "clusters" in data
    assert "collections" in data
    assert data.get("config_sync") == "enabled"
    assert data["redis"] == "ok"
    assert data["kafka"] == "ok"
    assert data["data_clusters"] == {"default-data-cluster": "ok"}


def test_prometheus_http_metrics_are_exposed(client, monkeypatch):
    import main

    monkeypatch.setattr(main, "_check_redis_ok", lambda: True)
    monkeypatch.setattr(main, "_check_kafka_ok", lambda: True)

    assert client.get("/health").status_code == 200
    metrics = client.get("/metrics").text

    assert 'http_requests_total{handler="/health",method="GET",status="2xx"}' in metrics
    assert 'http_request_duration_seconds_bucket{handler="/health"' in metrics
    assert 'method="GET"}' in metrics


def test_readiness_policy_rejects_unsupported_value():
    with pytest.raises(ValidationError):
        Settings(READINESS_POLICY="unknown")


def test_typesense_bootstrap_protocol_rejects_unsupported_value():
    with pytest.raises(ValidationError):
        Settings(INTERNAL_STATE_PROTOCOL="ftp")


def test_ready_strict_policy_returns_200_when_dependencies_are_healthy(
    client, monkeypatch
):
    import main

    monkeypatch.setattr(main.settings, "READINESS_POLICY", "strict")
    monkeypatch.setattr(main, "_check_redis_ok", lambda: True)
    monkeypatch.setattr(main, "_check_kafka_ok", lambda: True)

    r = client.get("/ready")

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "healthy"
    assert data["ready"] is True
    assert data["readiness_policy"] == "strict"
    assert data["control_plane"]["status"] == "ok"
    assert data["control_plane"]["converged"] is True


def test_ready_strict_policy_fails_when_control_plane_revision_is_stale(
    client, monkeypatch
):
    import main

    monkeypatch.setattr(main.settings, "READINESS_POLICY", "strict")
    monkeypatch.setattr(main, "_check_redis_ok", lambda: True)
    monkeypatch.setattr(main, "_check_kafka_ok", lambda: True)
    monkeypatch.setattr(
        main,
        "_check_control_plane",
        lambda: {
            "status": "stale",
            "backend": "postgres",
            "authoritative_revision": 12,
            "applied_revision": 11,
            "state_digest": "abc",
            "converged": False,
        },
    )
    main._reset_dependency_health_cache()

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert response.json()["control_plane"]["authoritative_revision"] == 12


def test_ready_serving_policy_keeps_pod_ready_when_data_cluster_is_not_ready(
    client, monkeypatch
):
    import main

    monkeypatch.setattr(main.settings, "READINESS_POLICY", "serving")
    monkeypatch.setattr(main, "_check_redis_ok", lambda: True)
    monkeypatch.setattr(main, "_check_kafka_ok", lambda: True)
    main.federation_service.clients = {"cluster-down": FailingTypesenseClient()}

    r = client.get("/ready")

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "degraded"
    assert data["ready"] is True
    assert data["readiness_policy"] == "serving"
    assert data["data_clusters"] == {"cluster-down": "error"}


def test_ready_strict_policy_returns_503_when_data_cluster_is_not_ready(
    client, monkeypatch
):
    import main

    monkeypatch.setattr(main.settings, "READINESS_POLICY", "strict")
    monkeypatch.setattr(main, "_check_redis_ok", lambda: True)
    monkeypatch.setattr(main, "_check_kafka_ok", lambda: True)
    main.federation_service.clients = {"cluster-down": FailingTypesenseClient()}

    r = client.get("/ready")

    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "degraded"
    assert data["ready"] is False
    assert data["readiness_policy"] == "strict"


def test_ready_serving_policy_reports_degraded_declared_cluster_node(
    client, monkeypatch
):
    import main

    monkeypatch.setattr(main.settings, "READINESS_POLICY", "serving")
    monkeypatch.setattr(main, "_check_redis_ok", lambda: True)
    monkeypatch.setattr(main, "_check_kafka_ok", lambda: True)

    federation = FederationService()
    federation.clients = {"cluster-a": object()}
    federation.clusters_config = {
        "cluster-a": {
            "host": "node-a,node-b",
            "port": 8108,
            "api_key": "test-key",
        }
    }
    monkeypatch.setattr(main, "federation_service", federation)
    monkeypatch.setattr(
        FederationService,
        "cluster_node_statuses",
        lambda self, cluster_name, **_kwargs: [
            {"host": "node-a", "status": "ok"},
            {"host": "node-b", "status": "error", "error": "not ready"},
        ],
    )

    r = client.get("/ready")

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "degraded"
    assert data["ready"] is True
    assert data["data_clusters"] == {"cluster-a": "error"}
    assert data["data_cluster_nodes"]["cluster-a"] == [
        {"host": "node-a", "status": "ok"},
        {"host": "node-b", "status": "error", "error": "not ready"},
    ]


def test_ready_returns_503_when_core_services_are_not_initialized(client, monkeypatch):
    import main

    monkeypatch.setattr(main.settings, "READINESS_POLICY", "serving")
    monkeypatch.setattr(main, "federation_service", None)
    monkeypatch.setattr(main, "state_manager", None)
    monkeypatch.setattr(main, "kafka_service", None)

    r = client.get("/ready")

    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "degraded"
    assert data["ready"] is False
    assert data["readiness_policy"] == "serving"


def test_dependency_health_is_cached_between_probe_requests(client, monkeypatch):
    import main

    calls = 0

    def check_redis():
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(main, "_check_redis_ok", check_redis)
    monkeypatch.setattr(main, "_check_kafka_ok", lambda: True)
    main._reset_dependency_health_cache()

    first = client.get("/health")
    second = client.get("/ready")

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls == 1
    assert second.json()["health_cache_stale"] is False


def test_strict_readiness_fails_closed_on_stale_health_snapshot(client, monkeypatch):
    import main

    monkeypatch.setattr(main.settings, "READINESS_POLICY", "strict")
    monkeypatch.setattr(
        main,
        "_cached_dependency_health",
        lambda: {
            "status": "healthy",
            "clusters": 1,
            "collections": 0,
            "redis": "ok",
            "kafka": "ok",
            "data_clusters": {"cluster-a": "ok"},
            "data_cluster_nodes": {
                "cluster-a": [{"host": "node-a", "status": "ok"}]
            },
            "checks_timed_out": [],
            "dependency_errors": {},
            "checked_at_unix_ms": 1,
            "health_cache_age_seconds": 10.0,
            "health_cache_stale": True,
        },
    )

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["ready"] is False


def test_dependency_health_checks_clusters_in_parallel(client, monkeypatch):
    import main

    barrier = threading.Barrier(2)
    federation = FederationService()
    federation.clients = {"cluster-a": object(), "cluster-b": object()}
    federation.clusters_config = {
        "cluster-a": {"host": "node-a", "port": 8108, "api_key": "a"},
        "cluster-b": {"host": "node-b", "port": 8108, "api_key": "b"},
    }
    monkeypatch.setattr(main, "federation_service", federation)
    monkeypatch.setattr(main, "_check_redis_ok", lambda: True)
    monkeypatch.setattr(main, "_check_kafka_ok", lambda: True)

    def synchronized_status(_self, cluster_name, **_kwargs):
        barrier.wait(timeout=0.5)
        return [{"host": cluster_name, "status": "ok"}]

    monkeypatch.setattr(FederationService, "cluster_node_statuses", synchronized_status)
    monkeypatch.setattr(main, "QUERY_API_HEALTH_CHECK_BUDGET_SECONDS", 0.75)
    main._reset_dependency_health_cache()

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["checks_timed_out"] == []


@pytest.mark.parametrize(
    ("policy", "expected_status", "expected_ready"),
    [("serving", 200, True), ("strict", 503, False)],
)
def test_readiness_blackhole_is_bounded_and_policy_specific(
    client,
    monkeypatch,
    policy,
    expected_status,
    expected_ready,
):
    import main

    federation = FederationService()
    federation.clients = {"cluster-blackhole": object()}
    federation.clusters_config = {
        "cluster-blackhole": {
            "host": "blackhole.invalid",
            "port": 8108,
            "api_key": "test-key",
        }
    }
    monkeypatch.setattr(main, "federation_service", federation)
    monkeypatch.setattr(main.settings, "READINESS_POLICY", policy)
    monkeypatch.setattr(main, "_check_redis_ok", lambda: True)
    monkeypatch.setattr(main, "_check_kafka_ok", lambda: True)
    monkeypatch.setattr(main, "QUERY_API_HEALTH_CHECK_BUDGET_SECONDS", 0.05)

    release_blackhole = threading.Event()

    def blackholed_status(_self, _cluster_name, **_kwargs):
        # A controlled block makes the assertion independent of host load: an
        # implementation that waits for its dependency takes ~2s, while the
        # bounded implementation returns after the configured 50ms budget.
        release_blackhole.wait(timeout=2.0)
        return [{"host": "blackhole.invalid", "status": "error"}]

    monkeypatch.setattr(FederationService, "cluster_node_statuses", blackholed_status)
    main._reset_dependency_health_cache()

    started_at = time.monotonic()
    try:
        response = client.get("/ready")
        elapsed = time.monotonic() - started_at
    finally:
        release_blackhole.set()

    assert elapsed < 0.75
    assert response.status_code == expected_status
    payload = response.json()
    assert payload["ready"] is expected_ready
    assert payload["status"] == "degraded"
    assert payload["checks_timed_out"] == [
        "data_cluster:cluster-blackhole"
    ]
