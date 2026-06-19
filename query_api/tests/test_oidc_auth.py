"""Tests for OIDC/JWT bearer-token authorization."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


ISSUER = "https://idp.example.com/"
AUDIENCE = "imposbro-api"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _PRIVATE_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)
_PUBLIC_PEM = _PRIVATE_KEY.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode("utf-8")


def _configure_oidc(monkeypatch, *, mapping=""):
    from settings import settings

    monkeypatch.setattr(settings, "ADMIN_API_KEY", "")
    monkeypatch.setattr(settings, "DATA_API_KEY", "")
    monkeypatch.setattr(settings, "SCOPED_API_KEYS", "")
    monkeypatch.setattr(settings, "ALLOW_UNAUTHENTICATED_ADMIN", False)
    monkeypatch.setattr(settings, "ALLOW_UNAUTHENTICATED_DATA", False)
    monkeypatch.setattr(settings, "OIDC_ENABLED", True)
    monkeypatch.setattr(settings, "OIDC_ISSUER", ISSUER)
    monkeypatch.setattr(settings, "OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setattr(settings, "OIDC_JWKS_URL", "")
    monkeypatch.setattr(settings, "OIDC_PUBLIC_KEY", _PUBLIC_PEM)
    monkeypatch.setattr(settings, "OIDC_ALGORITHMS", "RS256")
    monkeypatch.setattr(settings, "OIDC_LEEWAY_SECONDS", 30)
    monkeypatch.setattr(settings, "OIDC_SCOPE_CLAIMS", "scope,scp,roles,groups,realm_access.roles")
    monkeypatch.setattr(settings, "OIDC_SCOPE_MAPPING", mapping)
    monkeypatch.setattr(settings, "OIDC_SUBJECT_CLAIM", "sub")
    monkeypatch.setattr(settings, "AUTHZ_COLLECTION_POLICIES", "")
    monkeypatch.setattr(settings, "AUTHZ_API_KEY_TENANT_BYPASS", True)


def _token(*, scope="", audience=AUDIENCE, issuer=ISSUER, extra_claims=None):
    now = datetime.now(timezone.utc)
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": "user-123",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    if scope:
        claims["scope"] = scope
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, _PRIVATE_PEM, algorithm="RS256")


def test_oidc_admin_scope_grants_admin_access(client, monkeypatch):
    _configure_oidc(monkeypatch)

    r = client.get(
        "/admin/stats",
        headers={"Authorization": f"Bearer {_token(scope='imposbro:admin')}"},
    )

    assert r.status_code == 200


def test_oidc_admin_operation_scopes_are_least_privilege(client, monkeypatch):
    _configure_oidc(monkeypatch)
    read_headers = {
        "Authorization": f"Bearer {_token(scope='imposbro:admin:read')}"
    }
    write_headers = {
        "Authorization": f"Bearer {_token(scope='imposbro:admin:write')}"
    }

    read_allowed = client.get("/admin/stats", headers=read_headers)
    read_denied_write = client.post(
        "/admin/federation/clusters",
        headers=read_headers,
        json={
            "name": "oidc-read-denied",
            "host": "typesense-oidc",
            "port": 8108,
            "api_key": "raw-cluster-secret",
        },
    )
    write_allowed = client.post(
        "/admin/federation/clusters",
        headers=write_headers,
        json={
            "name": "oidc-write-cluster",
            "host": "typesense-oidc",
            "port": 8108,
            "api_key": "raw-cluster-secret",
        },
    )
    write_denied_backup = client.get("/admin/state/export", headers=write_headers)

    assert read_allowed.status_code == 200
    assert read_denied_write.status_code == 401
    assert write_allowed.status_code == 201
    assert write_denied_backup.status_code == 401


def test_oidc_search_scope_cannot_ingest(client, monkeypatch):
    _configure_oidc(monkeypatch)
    headers = {"Authorization": f"Bearer {_token(scope='imposbro:search')}"}

    search = client.get("/search/products?q=test&query_by=name", headers=headers)
    ingest = client.post(
        "/ingest/products",
        headers=headers,
        json={"id": "doc-1", "name": "Product"},
    )

    assert search.status_code == 404
    assert ingest.status_code == 401


def test_oidc_collection_scoped_claim_only_grants_matching_collection(client, monkeypatch):
    _configure_oidc(monkeypatch)
    headers = {
        "Authorization": f"Bearer {_token(scope='imposbro:search:products_*')}"
    }

    matching = client.get(
        "/search/products_2026?q=test&query_by=name",
        headers=headers,
    )
    denied = client.get(
        "/search/orders_2026?q=test&query_by=name",
        headers=headers,
    )

    assert matching.status_code == 404
    assert denied.status_code == 401


def test_oidc_collection_scoped_data_claim_grants_matching_ingest(client, monkeypatch):
    _configure_oidc(monkeypatch)
    client.app.state.federation_service.get_targets_for_document = MagicMock(
        return_value=[(MagicMock(), "default-data-cluster")]
    )
    headers = {
        "Authorization": f"Bearer {_token(scope='imposbro:data:events_*')}"
    }

    matching = client.post(
        "/ingest/events_2026",
        headers=headers,
        json={"id": "doc-1", "name": "Event"},
    )
    denied = client.post(
        "/ingest/orders_2026",
        headers=headers,
        json={"id": "doc-2", "name": "Order"},
    )

    assert matching.status_code == 200
    assert denied.status_code == 401


def test_oidc_nested_role_mapping_can_grant_admin_access(client, monkeypatch):
    _configure_oidc(
        monkeypatch,
        mapping=json.dumps({"admin": ["platform-admin"]}),
    )
    token = _token(
        extra_claims={"realm_access": {"roles": ["platform-admin"]}},
    )

    r = client.get("/admin/stats", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200


def test_oidc_rejects_wrong_audience(client, monkeypatch):
    _configure_oidc(monkeypatch)
    token = _token(scope="imposbro:admin", audience="other-api")

    r = client.get("/admin/stats", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 401
    assert "invalid" in r.json().get("detail", "").lower()


def test_oidc_incomplete_configuration_fails_closed(client, monkeypatch):
    _configure_oidc(monkeypatch)
    from settings import settings

    monkeypatch.setattr(settings, "OIDC_PUBLIC_KEY", "")

    r = client.get(
        "/admin/stats",
        headers={"Authorization": f"Bearer {_token(scope='imposbro:admin')}"},
    )

    assert r.status_code == 500
    assert "OIDC" in r.json().get("detail", "")


def test_bearer_api_key_still_works_when_oidc_enabled(client, monkeypatch):
    _configure_oidc(monkeypatch)
    from settings import settings

    monkeypatch.setattr(settings, "ADMIN_API_KEY", "admin-secret")

    r = client.get(
        "/admin/stats",
        headers={"Authorization": "Bearer admin-secret"},
    )

    assert r.status_code == 200


class _CaptureDocuments:
    def __init__(self):
        self.params = None

    def search(self, params):
        self.params = params
        return {
            "found": 1,
            "hits": [
                {
                    "document": {"id": "doc-1", "tenant_id": "tenant-a"},
                    "text_match": 100,
                }
            ],
        }


class _CaptureCollection:
    def __init__(self, documents):
        self.documents = documents


class _CaptureCollections:
    def __init__(self, documents):
        self.documents = documents

    def __getitem__(self, collection_name):
        return _CaptureCollection(self.documents)


class _CaptureClient:
    def __init__(self):
        self.documents = _CaptureDocuments()
        self.collections = _CaptureCollections(self.documents)


def test_oidc_tenant_policy_adds_search_filter(client, monkeypatch):
    _configure_oidc(monkeypatch)
    from settings import settings

    monkeypatch.setattr(
        settings,
        "AUTHZ_COLLECTION_POLICIES",
        json.dumps({
            "collections": {
                "products": {
                    "mode": "required",
                    "tenant_field": "tenant_id",
                    "tenant_claim": "tenant_id",
                }
            }
        }),
    )
    search_client = _CaptureClient()
    client.app.state.federation_service.get_named_clients_for_search.return_value = [
        ("cluster-a", search_client)
    ]
    token = _token(
        scope="imposbro:search",
        extra_claims={"tenant_id": "tenant-a"},
    )

    r = client.get(
        "/search/products?q=test&query_by=name&filter_by=brand:=acme",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 200
    assert search_client.documents.params["filter_by"] == (
        "(brand:=acme) && tenant_id:=tenant-a"
    )


def test_oidc_tenant_policy_injects_missing_ingest_tenant(client, monkeypatch):
    _configure_oidc(monkeypatch)
    from settings import settings

    monkeypatch.setattr(
        settings,
        "AUTHZ_COLLECTION_POLICIES",
        json.dumps({
            "collections": {
                "products": {
                    "mode": "inject",
                    "tenant_field": "tenant_id",
                    "tenant_claim": "tenant_id",
                }
            }
        }),
    )
    client.app.state.federation_service.get_targets_for_document = MagicMock(
        return_value=[(MagicMock(), "default-data-cluster")]
    )
    token = _token(
        scope="imposbro:ingest",
        extra_claims={"tenant_id": "tenant-a"},
    )

    r = client.post(
        "/ingest/products",
        headers={"Authorization": f"Bearer {token}"},
        json={"id": "doc-1", "name": "Product"},
    )

    assert r.status_code == 200
    published = client.app.state.kafka_service.publish_document.call_args.kwargs
    assert published["document"]["tenant_id"] == "tenant-a"


def test_oidc_tenant_policy_rejects_cross_tenant_ingest(client, monkeypatch):
    _configure_oidc(monkeypatch)
    from settings import settings

    monkeypatch.setattr(
        settings,
        "AUTHZ_COLLECTION_POLICIES",
        json.dumps({
            "collections": {
                "products": {
                    "mode": "required",
                    "tenant_field": "tenant_id",
                    "tenant_claim": "tenant_id",
                }
            }
        }),
    )
    token = _token(
        scope="imposbro:ingest",
        extra_claims={"tenant_id": "tenant-a"},
    )

    r = client.post(
        "/ingest/products",
        headers={"Authorization": f"Bearer {token}"},
        json={"id": "doc-1", "tenant_id": "tenant-b"},
    )

    assert r.status_code == 403


def test_oidc_audit_actor_does_not_store_raw_subject(client, monkeypatch):
    _configure_oidc(monkeypatch)
    token = _token(
        scope="imposbro:admin",
        extra_claims={"sub": "user@example.com"},
    )

    r = client.post(
        "/admin/federation/clusters",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "oidc-cluster",
            "host": "typesense-oidc",
            "port": 8108,
            "api_key": "raw-cluster-secret",
        },
    )

    assert r.status_code == 201
    actor = client.app.state.state_manager.record_admin_audit.call_args.kwargs["actor"]
    assert actor.startswith("oidc:")
    assert "user@example.com" not in actor
    assert ISSUER not in actor
