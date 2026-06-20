"""Tests for health and root endpoints."""
import pytest
import typesense

from services import FederationService


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


def test_health_returns_200(client, monkeypatch):
    import main

    monkeypatch.setattr(main, "_check_redis_ok", lambda: True)
    monkeypatch.setattr(main, "_check_kafka_ok", lambda: True)

    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "healthy"
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


def test_ready_returns_503_when_data_cluster_is_not_ready(client, monkeypatch):
    import main

    monkeypatch.setattr(main, "_check_redis_ok", lambda: True)
    monkeypatch.setattr(main, "_check_kafka_ok", lambda: True)
    main.federation_service.clients = {"cluster-down": FailingTypesenseClient()}

    r = client.get("/ready")

    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "degraded"
    assert data["data_clusters"] == {"cluster-down": "error"}


def test_ready_returns_503_when_any_declared_cluster_node_is_not_ready(
    client, monkeypatch
):
    import main

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
        lambda self, cluster_name: [
            {"host": "node-a", "status": "ok"},
            {"host": "node-b", "status": "error", "error": "not ready"},
        ],
    )

    r = client.get("/ready")

    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "degraded"
    assert data["data_clusters"] == {"cluster-a": "error"}
    assert data["data_cluster_nodes"]["cluster-a"] == [
        {"host": "node-a", "status": "ok"},
        {"host": "node-b", "status": "error", "error": "not ready"},
    ]
