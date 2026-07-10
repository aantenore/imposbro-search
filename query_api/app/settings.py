from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


TypesenseProtocol = Literal["http", "https"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    KAFKA_BROKER_URL: str
    KAFKA_TOPIC_PREFIX: str
    REDIS_URL: str

    # Optional source id for Redis config sync. When omitted, the app uses
    # hostname:pid so replicas ignore only their own notifications.
    CONFIG_SYNC_SOURCE_ID: str = ""

    # Internal State Cluster
    INTERNAL_STATE_NODES: str
    INTERNAL_STATE_API_KEY: str
    INTERNAL_STATE_PROTOCOL: TypesenseProtocol = "http"

    # Default Federated Data Cluster
    DEFAULT_DATA_CLUSTER_NODES: str
    DEFAULT_DATA_CLUSTER_API_KEY: str
    DEFAULT_DATA_CLUSTER_PROTOCOL: TypesenseProtocol = "http"

    # Secondary Federated Data Cluster (optional; leave empty to disable)
    DEFAULT_DATA2_CLUSTER_NODES: str = ""
    DEFAULT_DATA2_CLUSTER_API_KEY: str = ""
    DEFAULT_DATA2_CLUSTER_PROTOCOL: TypesenseProtocol = "http"

    INTERNAL_QUERY_API_URL: str

    # CORS: comma-separated origins (e.g. "http://localhost:3001,https://admin.example.com")
    # Empty string = CORS middleware not added (same-origin only)
    CORS_ORIGINS: str = ""

    # Header used for support diagnostics across HTTP requests and Kafka ingest
    # messages. Gateways may override this to match their tracing convention.
    REQUEST_ID_HEADER: str = Field(default="X-Request-ID", min_length=1, max_length=64)

    # Kubernetes serving readiness defaults to keeping initialized API pods in
    # rotation while downstream dependencies are degraded. Use strict when the
    # orchestrator should require every dependency and data node to be healthy.
    READINESS_POLICY: Literal["serving", "strict"] = "serving"

    # Optional API key for Admin API. If set, all /admin/* requests must include
    # header X-API-Key: <value> or Authorization: Bearer <value>
    ADMIN_API_KEY: str = ""

    # Optional JSON array of scoped API keys:
    # [{"name":"ops","key":"secret","scopes":["admin","search","ingest"]}]
    SCOPED_API_KEYS: str = ""

    # Local-development escape hatch. Keep false in shared, exposed, or production environments.
    ALLOW_UNAUTHENTICATED_ADMIN: bool = False

    # Optional API key for data-plane endpoints (/ingest and /search).
    # Keep separate from ADMIN_API_KEY so operators can grant least-privilege access.
    DATA_API_KEY: str = ""

    # Local-development escape hatch. Keep false in shared, exposed, or production environments.
    ALLOW_UNAUTHENTICATED_DATA: bool = False

    # Maximum documents accepted by one /ingest/{collection}/batch request.
    INGEST_BATCH_MAX_DOCUMENTS: int = Field(default=100, ge=1)

    # Optional fixed-window data-plane rate limiting for /search/* and /ingest/*.
    # Use Redis backend for multi-replica deployments; memory is for tests or
    # single-process local runs only.
    RATE_LIMIT_ENABLED: bool = False
    RATE_LIMIT_BACKEND: str = "redis"
    RATE_LIMIT_WINDOW_SECONDS: int = Field(default=60, ge=1)
    RATE_LIMIT_SEARCH_REQUESTS: int = Field(default=120, ge=1)
    RATE_LIMIT_INGEST_REQUESTS: int = Field(default=60, ge=1)
    RATE_LIMIT_FAIL_CLOSED: bool = False
    RATE_LIMIT_REDIS_PREFIX: str = "imposbro:rate_limit"

    @field_validator("RATE_LIMIT_BACKEND")
    @classmethod
    def validate_rate_limit_backend(cls, value: str) -> str:
        backend = value.strip().lower()
        if backend not in {"redis", "memory"}:
            raise ValueError("RATE_LIMIT_BACKEND must be either 'redis' or 'memory'")
        return backend

    @field_validator("REQUEST_ID_HEADER")
    @classmethod
    def validate_request_id_header(cls, value: str) -> str:
        header = value.strip()
        if not header or any(ch in header for ch in "\r\n:"):
            raise ValueError("REQUEST_ID_HEADER must be a valid HTTP header name")
        return header

    # Optional OIDC/JWT bearer-token authentication for enterprise gateways.
    # Tokens are accepted in Authorization: Bearer after API-key checks fail.
    OIDC_ENABLED: bool = False
    OIDC_ISSUER: str = ""
    OIDC_AUDIENCE: str = ""
    OIDC_JWKS_URL: str = ""
    OIDC_PUBLIC_KEY: str = ""
    OIDC_ALGORITHMS: str = "RS256"
    OIDC_LEEWAY_SECONDS: int = 30
    OIDC_SCOPE_CLAIMS: str = "scope,scp,roles,groups,realm_access.roles"
    OIDC_SCOPE_MAPPING: str = ""
    OIDC_SUBJECT_CLAIM: str = "sub"

    # Optional per-collection tenant policy JSON. Example:
    # {"collections":{"orders":{"mode":"required","tenant_field":"tenant_id","tenant_claim":"tenant_id"}}}
    # mode=required injects a tenant filter into search and rejects cross-tenant ingest.
    # mode=inject also writes a missing tenant field during ingest when the token has one tenant.
    AUTHZ_COLLECTION_POLICIES: str = ""
    AUTHZ_API_KEY_TENANT_BYPASS: bool = True

    # Admin audit log. Records successful control-plane mutations to the
    # internal state cluster without storing raw API keys or cluster secrets.
    AUDIT_LOG_ENABLED: bool = True
    AUDIT_LOG_MAX_RESULTS: int = 100


settings = Settings()
