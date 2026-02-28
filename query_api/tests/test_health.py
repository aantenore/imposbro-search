"""Tests for health and root endpoints."""
import pytest


def test_root_returns_200(client):
    r = client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert data.get("service") == "IMPOSBRO Federated Search API"
    assert data.get("status") == "healthy"
    assert "version" in data


def test_health_returns_200(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert "status" in data
    assert "clusters" in data
    assert "collections" in data
    assert data.get("config_sync") == "enabled"
