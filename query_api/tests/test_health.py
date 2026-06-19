"""Tests for health and root endpoints."""
import pytest
import typesense


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
