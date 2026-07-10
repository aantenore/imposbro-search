"""Fail-fast configuration tests for production trust boundaries."""

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError


_app_dir = Path(__file__).resolve().parent.parent / "app"
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

from settings import Settings


def base_settings(**overrides):
    values = {
        "KAFKA_BROKER_URL": "kafka:9092",
        "KAFKA_TOPIC_PREFIX": "test",
        "REDIS_URL": "rediss://redis:6379",
        "INTERNAL_STATE_NODES": "state.example.com",
        "INTERNAL_STATE_API_KEY": "state-key",
        "DEFAULT_DATA_CLUSTER_NODES": "data.example.com",
        "DEFAULT_DATA_CLUSTER_API_KEY": "data-key",
        "INTERNAL_QUERY_API_URL": "https://query.example.com",
        "ALLOW_UNAUTHENTICATED_ADMIN": False,
        "ALLOW_UNAUTHENTICATED_DATA": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_policy_and_scoped_keys_are_validated_at_startup():
    with pytest.raises(ValidationError, match="SCOPED_API_KEYS"):
        base_settings(SCOPED_API_KEYS="not-json")
    with pytest.raises(ValidationError, match="collections object"):
        base_settings(AUTHZ_COLLECTION_POLICIES='{"wrong": {}}')


def test_oidc_rejects_insecure_runtime_urls_outside_local_escape_hatch():
    with pytest.raises(ValidationError, match="OIDC_ISSUER must use HTTPS"):
        base_settings(
            OIDC_ENABLED=True,
            OIDC_ISSUER="http://idp.example.com",
            OIDC_JWKS_URL="https://idp.example.com/jwks",
        )


def test_enterprise_profile_fails_closed_with_actionable_missing_controls():
    with pytest.raises(ValidationError) as exc_info:
        base_settings(
            DEPLOYMENT_PROFILE="enterprise",
            INTERNAL_STATE_API_KEY="",
            INTERNAL_STATE_API_KEY_REF="env:INTERNAL_STATE_API_KEY",
            DEFAULT_DATA_CLUSTER_API_KEY="",
            DEFAULT_DATA_CLUSTER_API_KEY_REF="env:DEFAULT_DATA_CLUSTER_API_KEY",
        )

    error = str(exc_info.value)
    assert "CONTROL_PLANE_STORE_BACKEND=postgres" in error
    assert "OIDC_ENABLED=true" in error
    assert "AUTHZ_API_KEY_TENANT_BYPASS=false" in error
    assert "AUTHZ_REQUIRE_COLLECTION_POLICY=true" in error
    assert "RATE_LIMIT_FAIL_CLOSED=true" in error
    assert "HTTPS for every Typesense cluster" in error
    assert "OTEL_SDK_DISABLED=false" in error
    assert "OTEL_TRACES_EXPORTER=otlp" in error


def test_enterprise_profile_accepts_complete_fail_closed_baseline():
    settings = base_settings(
        DEPLOYMENT_PROFILE="enterprise",
        INTERNAL_STATE_API_KEY="",
        INTERNAL_STATE_API_KEY_REF="env:INTERNAL_STATE_API_KEY",
        DEFAULT_DATA_CLUSTER_API_KEY="",
        DEFAULT_DATA_CLUSTER_API_KEY_REF="env:DEFAULT_DATA_CLUSTER_API_KEY",
        CONTROL_PLANE_STORE_BACKEND="postgres",
        INDEXING_EVENT_STORE_BACKEND="postgres",
        CONTROL_PLANE_DATABASE_URL=(
            "postgresql+psycopg://imposbro:secret@postgres/imposbro"
            "?sslmode=verify-full"
        ),
        INTERNAL_STATE_PROTOCOL="https",
        DEFAULT_DATA_CLUSTER_PROTOCOL="https",
        OIDC_ENABLED=True,
        OIDC_ISSUER="https://idp.example.com",
        OIDC_AUDIENCE="imposbro",
        OIDC_JWKS_URL="https://idp.example.com/jwks",
        AUTHZ_API_KEY_TENANT_BYPASS=False,
        AUTHZ_REQUIRE_COLLECTION_POLICY=True,
        AUTHZ_COLLECTION_POLICIES=(
            '{"collections":{"orders":{"mode":"required",'
            '"tenant_field":"tenant_id","tenant_claim":"tenant_id"}}}'
        ),
        RATE_LIMIT_ENABLED=True,
        RATE_LIMIT_BACKEND="redis",
        RATE_LIMIT_FAIL_CLOSED=True,
        AUDIT_LOG_ENABLED=True,
        LOG_FORMAT="json",
        OTEL_SDK_DISABLED=False,
        OTEL_TRACES_EXPORTER="otlp",
        OTEL_EXPORTER_OTLP_ENDPOINT="https://otel.example.com:4318",
        OTEL_DEPLOYMENT_ENVIRONMENT="enterprise",
        OTEL_BUILD_ID="build-42",
        OTEL_SERVICE_REVISION="abcdef123456",
        CORS_ORIGINS="https://admin.example.com",
        READINESS_POLICY="strict",
        KAFKA_SECURITY_PROTOCOL="SASL_SSL",
        KAFKA_SASL_USERNAME="imposbro",
        KAFKA_SASL_PASSWORD="secret",
    )

    assert settings.DEPLOYMENT_PROFILE == "enterprise"
    assert settings.CONTROL_PLANE_STORE_BACKEND == "postgres"


def test_non_development_profiles_reject_inline_typesense_credentials():
    with pytest.raises(ValidationError, match="allowed only in the development profile"):
        base_settings(DEPLOYMENT_PROFILE="production")


def test_typesense_cluster_credentials_require_exactly_one_source():
    with pytest.raises(ValidationError, match="exactly one"):
        base_settings(DEFAULT_DATA_CLUSTER_API_KEY_REF="env:DATA_KEY")
