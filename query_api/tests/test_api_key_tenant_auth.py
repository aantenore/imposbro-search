"""Tenant-bound machine principal tests for scoped API keys."""

import json
from unittest.mock import MagicMock


def configure_tenant_key(monkeypatch, *, expires_at="2099-01-01T00:00:00Z"):
    from settings import settings

    monkeypatch.setattr(settings, "OIDC_ENABLED", False)
    monkeypatch.setattr(settings, "DATA_API_KEY", "")
    monkeypatch.setattr(settings, "ALLOW_UNAUTHENTICATED_DATA", False)
    monkeypatch.setattr(settings, "AUTHZ_API_KEY_TENANT_BYPASS", False)
    monkeypatch.setattr(
        settings,
        "SCOPED_API_KEYS",
        json.dumps(
            [
                {
                    "name": "tenant-a-writer",
                    "key": "tenant-a-secret",
                    "scopes": ["ingest:products"],
                    "claims": {"tenant_id": ["tenant-a"]},
                    "expires_at": expires_at,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        settings,
        "AUTHZ_COLLECTION_POLICIES",
        json.dumps(
            {
                "collections": {
                    "products": {
                        "mode": "required",
                        "tenant_field": "tenant_id",
                        "tenant_claim": "tenant_id",
                    }
                }
            }
        ),
    )


def test_tenant_bound_api_key_can_write_only_its_claimed_tenant(client, monkeypatch):
    configure_tenant_key(monkeypatch)
    client.app.state.federation_service.get_targets_for_document = MagicMock(
        return_value=[(MagicMock(), "default-data-cluster")]
    )
    headers = {"X-API-Key": "tenant-a-secret"}

    allowed = client.post(
        "/ingest/products",
        headers=headers,
        json={"id": "doc-a", "tenant_id": "tenant-a"},
    )
    denied = client.post(
        "/ingest/products",
        headers=headers,
        json={"id": "doc-b", "tenant_id": "tenant-b"},
    )

    assert allowed.status_code == 200
    assert denied.status_code == 403
    assert client.app.state.kafka_service.publish_document.call_count == 1


def test_expired_scoped_api_key_is_not_an_authentication_candidate(client, monkeypatch):
    configure_tenant_key(monkeypatch, expires_at="2020-01-01T00:00:00Z")

    response = client.post(
        "/ingest/products",
        headers={"X-API-Key": "tenant-a-secret"},
        json={"id": "doc-a", "tenant_id": "tenant-a"},
    )

    assert response.status_code == 401


def test_enterprise_profile_denies_unmatched_collection_policy(client, monkeypatch):
    configure_tenant_key(monkeypatch)
    from settings import settings

    monkeypatch.setattr(settings, "DEPLOYMENT_PROFILE", "enterprise")
    monkeypatch.setattr(
        settings,
        "AUTHZ_COLLECTION_POLICIES",
        json.dumps(
            {
                "collections": {
                    "orders": {
                        "mode": "required",
                        "tenant_field": "tenant_id",
                        "tenant_claim": "tenant_id",
                    }
                }
            }
        ),
    )

    response = client.post(
        "/ingest/products",
        headers={"X-API-Key": "tenant-a-secret"},
        json={"id": "doc-a", "tenant_id": "tenant-a"},
    )

    assert response.status_code == 403
    assert "policy is required" in response.json()["detail"]


def test_explicit_policy_requirement_denies_unmatched_collection_in_development(
    client, monkeypatch
):
    configure_tenant_key(monkeypatch)
    from settings import settings

    monkeypatch.setattr(settings, "DEPLOYMENT_PROFILE", "development")
    monkeypatch.setattr(settings, "AUTHZ_REQUIRE_COLLECTION_POLICY", True)
    monkeypatch.setattr(
        settings,
        "AUTHZ_COLLECTION_POLICIES",
        json.dumps(
            {
                "collections": {
                    "orders": {
                        "mode": "required",
                        "tenant_field": "tenant_id",
                        "tenant_claim": "tenant_id",
                    }
                }
            }
        ),
    )

    response = client.post(
        "/ingest/products",
        headers={"X-API-Key": "tenant-a-secret"},
        json={"id": "doc-a", "tenant_id": "tenant-a"},
    )

    assert response.status_code == 403
    assert "policy is required" in response.json()["detail"]
