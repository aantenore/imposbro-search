"""Tests for health and root endpoints."""
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
        lambda self, cluster_name: [
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
