import json
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from secret_resolver import SecretResolutionError, parse_secret_reference


TypesenseProtocol = Literal["http", "https"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    KAFKA_BROKER_URL: str
    KAFKA_TOPIC_PREFIX: str
    REDIS_URL: str
    KAFKA_SECURITY_PROTOCOL: Literal[
        "PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"
    ] = "PLAINTEXT"
    KAFKA_SASL_MECHANISM: Literal["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"] = (
        "PLAIN"
    )
    KAFKA_SASL_USERNAME: str = ""
    KAFKA_SASL_PASSWORD: str = ""
    KAFKA_SSL_CAFILE: str = ""
    KAFKA_SSL_CERTFILE: str = ""
    KAFKA_SSL_KEYFILE: str = ""
    KAFKA_SSL_CHECK_HOSTNAME: bool = True
    KAFKA_CLIENT_ID: str = "imposbro-query-api"
    INDEXING_KAFKA_CONSUMER_GROUP_ID: str = "imposbro_federated_indexing_group"
    INDEXING_KAFKA_DLQ_GROUP_ID: str = "imposbro_indexing_dlq_resolver"
    KAFKA_OPERATION_TIMEOUT_MS: int = Field(default=5000, ge=1000, le=60000)
    ROUTING_CUTOVER_MAX_KAFKA_LAG: int = Field(default=0, ge=0, le=1_000_000)

    # development keeps backward-compatible local defaults. enterprise enables
    # fail-closed validation for the production acceptance contract.
    DEPLOYMENT_PROFILE: Literal["development", "production", "enterprise"] = (
        "development"
    )
    LOG_FORMAT: Literal["text", "json"] = "text"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # OpenTelemetry traces use the stable OTLP/HTTP exporter. Development can
    # keep the SDK disabled; enterprise must provide a release-identifiable,
    # TLS-protected collector endpoint.
    OTEL_SDK_DISABLED: bool = True
    OTEL_TRACES_EXPORTER: Literal["none", "otlp"] = "none"
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: str = ""
    OTEL_EXPORTER_OTLP_TIMEOUT: int = Field(default=5000, ge=100, le=60000)
    OTEL_BSP_SCHEDULE_DELAY: int = Field(default=5000, ge=100, le=60000)
    OTEL_BSP_EXPORT_TIMEOUT: int = Field(default=5000, ge=100, le=60000)
    OTEL_BSP_MAX_QUEUE_SIZE: int = Field(default=2048, ge=1, le=65536)
    OTEL_BSP_MAX_EXPORT_BATCH_SIZE: int = Field(default=512, ge=1, le=65536)
    OTEL_TRACES_SAMPLER: Literal[
        "always_on",
        "always_off",
        "traceidratio",
        "parentbased_always_on",
        "parentbased_traceidratio",
    ] = "parentbased_always_on"
    OTEL_TRACES_SAMPLER_ARG: float = Field(default=1.0, ge=0.0, le=1.0)
    OTEL_SERVICE_NAME: str = Field(default="imposbro-query-api", min_length=1, max_length=128)
    OTEL_SERVICE_VERSION: str = Field(default="4.0.0", min_length=1, max_length=128)
    OTEL_DEPLOYMENT_ENVIRONMENT: str = Field(
        default="development", min_length=1, max_length=128
    )
    OTEL_BUILD_ID: str = Field(default="development", min_length=1, max_length=128)
    OTEL_SERVICE_REVISION: str = Field(
        default="development", min_length=1, max_length=128
    )

    # Authoritative control-plane persistence. Typesense is supported only for
    # legacy/local migration; enterprise deployments use PostgreSQL.
    CONTROL_PLANE_STORE_BACKEND: Literal["typesense", "postgres"] = "typesense"
    CONTROL_PLANE_DATABASE_URL: str = ""
    CONTROL_PLANE_RECONCILE_SECONDS: float = Field(default=5.0, gt=0, le=300)
    CONTROL_PLANE_OUTBOX_POLL_SECONDS: float = Field(default=1.0, gt=0, le=300)
    CONTROL_PLANE_OUTBOX_BATCH_SIZE: int = Field(default=100, ge=1, le=1000)
    CONTROL_PLANE_OUTBOX_MAX_PENDING: int = Field(default=1000, ge=0, le=1_000_000)
    CONTROL_PLANE_ADMIN_MUTATIONS_FROZEN: bool = False
    INDEXING_EVENT_STORE_BACKEND: Literal["disabled", "memory", "postgres"] = (
        "disabled"
    )
    INDEXING_OUTBOX_POLL_SECONDS: float = Field(default=1.0, gt=0, le=300)
    INDEXING_OUTBOX_BATCH_SIZE: int = Field(default=100, ge=1, le=1000)

    # Optional source id for Redis config sync. When omitted, the app uses
    # hostname:pid so replicas ignore only their own notifications.
    CONFIG_SYNC_SOURCE_ID: str = ""

    # Internal State Cluster
    INTERNAL_STATE_NODES: str
    INTERNAL_STATE_API_KEY: str = ""
    INTERNAL_STATE_API_KEY_REF: str = ""
    INTERNAL_STATE_PROTOCOL: TypesenseProtocol = "http"

    # Default Federated Data Cluster
    DEFAULT_DATA_CLUSTER_NODES: str
    DEFAULT_DATA_CLUSTER_API_KEY: str = ""
    DEFAULT_DATA_CLUSTER_API_KEY_REF: str = ""
    DEFAULT_DATA_CLUSTER_PROTOCOL: TypesenseProtocol = "http"

    # Secondary Federated Data Cluster (optional; leave empty to disable)
    DEFAULT_DATA2_CLUSTER_NODES: str = ""
    DEFAULT_DATA2_CLUSTER_API_KEY: str = ""
    DEFAULT_DATA2_CLUSTER_API_KEY_REF: str = ""
    DEFAULT_DATA2_CLUSTER_PROTOCOL: TypesenseProtocol = "http"

    # Root for file: references. Mounted Kubernetes/Docker secrets remain
    # outside persisted control-plane state and can rotate independently.
    TYPESENSE_SECRET_FILE_ROOT: str = "/run/secrets/imposbro"

    INTERNAL_QUERY_API_URL: str

    # CORS: comma-separated origins (e.g. "http://localhost:3001,https://admin.example.com")
    # Empty string = CORS middleware not added (same-origin only)
    CORS_ORIGINS: str = ""

    # Header used for support diagnostics across HTTP requests and Kafka ingest
    # messages. Gateways may override this to match their tracing convention.
    REQUEST_ID_HEADER: str = Field(default="X-Request-ID", min_length=1, max_length=64)
    IDEMPOTENCY_KEY_HEADER: str = Field(
        default="Idempotency-Key", min_length=1, max_length=64
    )
    DOCUMENT_VERSION_HEADER: str = Field(
        default="X-Document-Version", min_length=1, max_length=64
    )
    EVENT_SEQUENCE_HEADER: str = Field(
        default="X-Event-Sequence", min_length=1, max_length=64
    )

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

    @field_validator(
        "REQUEST_ID_HEADER",
        "IDEMPOTENCY_KEY_HEADER",
        "DOCUMENT_VERSION_HEADER",
        "EVENT_SEQUENCE_HEADER",
    )
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
    ALLOW_INSECURE_OIDC_URLS: bool = False

    # Optional per-collection tenant policy JSON. Example:
    # {"collections":{"orders":{"mode":"required","tenant_field":"tenant_id","tenant_claim":"tenant_id"}}}
    # mode=required injects a tenant filter into search and rejects cross-tenant ingest.
    # mode=inject also writes a missing tenant field during ingest when the token has one tenant.
    AUTHZ_COLLECTION_POLICIES: str = ""
    AUTHZ_API_KEY_TENANT_BYPASS: bool = True
    AUTHZ_REQUIRE_COLLECTION_POLICY: bool = False

    # Admin audit log. Records successful control-plane mutations to the
    # internal state cluster without storing raw API keys or cluster secrets.
    AUDIT_LOG_ENABLED: bool = True
    AUDIT_LOG_MAX_RESULTS: int = 100
    AUDIT_EXPORT_MAX_RESULTS: int = Field(default=1000, ge=1, le=10000)
    AUDIT_RETENTION_DAYS: int = Field(default=365, ge=1, le=3650)

    @field_validator("SCOPED_API_KEYS")
    @classmethod
    def validate_scoped_api_keys_json(cls, value: str) -> str:
        raw = value.strip()
        if not raw:
            return ""
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("SCOPED_API_KEYS must be a valid JSON array") from exc
        if not isinstance(entries, list):
            raise ValueError("SCOPED_API_KEYS must be a JSON array")
        for entry in entries:
            if not isinstance(entry, dict) or not str(entry.get("key", "")).strip():
                raise ValueError("Every SCOPED_API_KEYS entry requires a key")
            scopes = entry.get("scopes")
            if not isinstance(scopes, (str, list)) or not scopes:
                raise ValueError("Every SCOPED_API_KEYS entry requires scopes")
            if "claims" in entry and not isinstance(entry["claims"], dict):
                raise ValueError("SCOPED_API_KEYS claims must be an object")
            raw_expiry = str(entry.get("expires_at", "")).strip()
            if raw_expiry:
                try:
                    expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError(
                        "SCOPED_API_KEYS expires_at must be ISO-8601"
                    ) from exc
                if expiry.tzinfo is None:
                    raise ValueError(
                        "SCOPED_API_KEYS expires_at requires a timezone"
                    )
        return raw

    @field_validator("AUTHZ_COLLECTION_POLICIES")
    @classmethod
    def validate_collection_policies_json(cls, value: str) -> str:
        raw = value.strip()
        if not raw:
            return ""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("AUTHZ_COLLECTION_POLICIES must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("AUTHZ_COLLECTION_POLICIES must be an object")
        if "mode" in payload or "tenant_field" in payload:
            policies = {"*": payload}
        else:
            if not isinstance(payload.get("collections"), dict):
                raise ValueError(
                    "AUTHZ_COLLECTION_POLICIES must contain a collections object"
                )
            policies = dict(payload["collections"])
            if "default" in payload:
                policies["default"] = payload["default"]
        for collection, policy in policies.items():
            if not collection or not isinstance(policy, dict):
                raise ValueError("Every collection policy must be an object")
            if policy.get("mode") not in {"required", "inject"}:
                raise ValueError("Collection policy mode must be required or inject")
            if not policy.get("tenant_field") or not policy.get("tenant_claim"):
                raise ValueError(
                    "Collection policies require tenant_field and tenant_claim"
                )
        return raw

    @model_validator(mode="after")
    def validate_typesense_secret_sources(self) -> "Settings":
        cluster_sources = (
            (
                "INTERNAL_STATE",
                self.INTERNAL_STATE_NODES,
                self.INTERNAL_STATE_API_KEY,
                self.INTERNAL_STATE_API_KEY_REF,
                False,
            ),
            (
                "DEFAULT_DATA_CLUSTER",
                self.DEFAULT_DATA_CLUSTER_NODES,
                self.DEFAULT_DATA_CLUSTER_API_KEY,
                self.DEFAULT_DATA_CLUSTER_API_KEY_REF,
                False,
            ),
            (
                "DEFAULT_DATA2_CLUSTER",
                self.DEFAULT_DATA2_CLUSTER_NODES,
                self.DEFAULT_DATA2_CLUSTER_API_KEY,
                self.DEFAULT_DATA2_CLUSTER_API_KEY_REF,
                True,
            ),
        )
        for name, nodes, api_key, api_key_ref, optional in cluster_sources:
            configured = bool(nodes.strip())
            has_inline = bool(api_key)
            has_reference = bool(api_key_ref)
            if optional and not configured:
                if has_inline or has_reference:
                    raise ValueError(
                        f"{name} credentials require {name}_NODES"
                    )
                continue
            if has_inline == has_reference:
                raise ValueError(
                    f"{name} requires exactly one of API_KEY_REF or API_KEY"
                )
            if has_reference:
                try:
                    scheme, _ = parse_secret_reference(api_key_ref)
                except SecretResolutionError as exc:
                    raise ValueError(f"{name}_API_KEY_REF is invalid") from exc
                if scheme not in {"env", "file"}:
                    raise ValueError(
                        f"{name}_API_KEY_REF provider must be env or file"
                    )
            if has_inline and self.DEPLOYMENT_PROFILE != "development":
                raise ValueError(
                    f"{name}_API_KEY is allowed only in the development profile; "
                    f"configure {name}_API_KEY_REF"
                )
        if not self.TYPESENSE_SECRET_FILE_ROOT.strip():
            raise ValueError("TYPESENSE_SECRET_FILE_ROOT must be non-empty")
        return self

    @model_validator(mode="after")
    def validate_security_profile(self) -> "Settings":
        if self.OIDC_ENABLED and not self.ALLOW_INSECURE_OIDC_URLS:
            for name, value in (
                ("OIDC_ISSUER", self.OIDC_ISSUER),
                ("OIDC_JWKS_URL", self.OIDC_JWKS_URL),
            ):
                if value and urlparse(value).scheme.lower() != "https":
                    raise ValueError(f"{name} must use HTTPS")

        if self.DEPLOYMENT_PROFILE != "enterprise":
            return self

        failures = []
        if self.CONTROL_PLANE_STORE_BACKEND != "postgres":
            failures.append("CONTROL_PLANE_STORE_BACKEND=postgres")
        if self.INDEXING_EVENT_STORE_BACKEND != "postgres":
            failures.append("INDEXING_EVENT_STORE_BACKEND=postgres")
        if not self.CONTROL_PLANE_DATABASE_URL.startswith(
            ("postgresql://", "postgresql+psycopg://")
        ):
            failures.append("a PostgreSQL CONTROL_PLANE_DATABASE_URL")
        if self.ALLOW_UNAUTHENTICATED_ADMIN or self.ALLOW_UNAUTHENTICATED_DATA:
            failures.append("unauthenticated access disabled")
        if not self.OIDC_ENABLED:
            failures.append("OIDC_ENABLED=true")
        if not self.OIDC_ISSUER or not self.OIDC_AUDIENCE:
            failures.append("OIDC issuer and audience")
        if bool(self.OIDC_JWKS_URL) == bool(self.OIDC_PUBLIC_KEY):
            failures.append("exactly one OIDC JWKS URL or public key")
        if self.AUTHZ_API_KEY_TENANT_BYPASS:
            failures.append("AUTHZ_API_KEY_TENANT_BYPASS=false")
        if not self.AUTHZ_REQUIRE_COLLECTION_POLICY:
            failures.append("AUTHZ_REQUIRE_COLLECTION_POLICY=true")
        if not self.AUTHZ_COLLECTION_POLICIES:
            failures.append("deny-by-default AUTHZ_COLLECTION_POLICIES")
        if not self.RATE_LIMIT_ENABLED or self.RATE_LIMIT_BACKEND != "redis":
            failures.append("Redis rate limiting enabled")
        if not self.RATE_LIMIT_FAIL_CLOSED:
            failures.append("RATE_LIMIT_FAIL_CLOSED=true")
        if not self.AUDIT_LOG_ENABLED:
            failures.append("AUDIT_LOG_ENABLED=true")
        if self.LOG_FORMAT != "json":
            failures.append("LOG_FORMAT=json")
        otlp_endpoint = (
            self.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
            or self.OTEL_EXPORTER_OTLP_ENDPOINT
        )
        parsed_otlp_endpoint = urlparse(otlp_endpoint)
        if self.OTEL_SDK_DISABLED:
            failures.append("OTEL_SDK_DISABLED=false")
        if self.OTEL_TRACES_EXPORTER != "otlp":
            failures.append("OTEL_TRACES_EXPORTER=otlp")
        if (
            parsed_otlp_endpoint.scheme.lower() != "https"
            or not parsed_otlp_endpoint.hostname
            or parsed_otlp_endpoint.username
            or parsed_otlp_endpoint.password
        ):
            failures.append("an HTTPS OTLP trace endpoint without URL credentials")
        if self.OTEL_BSP_MAX_EXPORT_BATCH_SIZE > self.OTEL_BSP_MAX_QUEUE_SIZE:
            failures.append("OTEL_BSP_MAX_EXPORT_BATCH_SIZE <= OTEL_BSP_MAX_QUEUE_SIZE")
        if self.OTEL_TRACES_SAMPLER == "always_off" or self.OTEL_TRACES_SAMPLER_ARG <= 0:
            failures.append("a non-zero OpenTelemetry sampling policy")
        if self.OTEL_BUILD_ID.lower() in {"unknown", "development"}:
            failures.append("a release OTEL_BUILD_ID")
        if self.OTEL_SERVICE_REVISION.lower() in {"unknown", "development"}:
            failures.append("a release OTEL_SERVICE_REVISION")
        if self.OTEL_DEPLOYMENT_ENVIRONMENT.lower() in {"unknown", "development"}:
            failures.append("a release OTEL_DEPLOYMENT_ENVIRONMENT")
        required_typesense_protocols = [self.DEFAULT_DATA_CLUSTER_PROTOCOL]
        if self.DEFAULT_DATA2_CLUSTER_NODES:
            required_typesense_protocols.append(self.DEFAULT_DATA2_CLUSTER_PROTOCOL)
        if any(protocol != "https" for protocol in required_typesense_protocols):
            failures.append("HTTPS for every Typesense cluster")
        if self.READINESS_POLICY != "strict":
            failures.append("READINESS_POLICY=strict")
        if self.KAFKA_SECURITY_PROTOCOL not in {"SSL", "SASL_SSL"}:
            failures.append("Kafka TLS enabled")
        if self.KAFKA_SECURITY_PROTOCOL == "SASL_SSL" and not (
            self.KAFKA_SASL_USERNAME and self.KAFKA_SASL_PASSWORD
        ):
            failures.append("Kafka SASL credentials")
        if bool(self.KAFKA_SSL_CERTFILE) != bool(self.KAFKA_SSL_KEYFILE):
            failures.append("both Kafka mTLS certificate and key")
        if not self.KAFKA_SSL_CHECK_HOSTNAME:
            failures.append("Kafka TLS hostname verification")
        if urlparse(self.REDIS_URL).scheme.lower() != "rediss":
            failures.append("Redis TLS via rediss://")
        database_query = urlparse(self.CONTROL_PLANE_DATABASE_URL).query.lower()
        if "sslmode=verify-full" not in database_query:
            failures.append("PostgreSQL sslmode=verify-full")
        if "*" in {origin.strip() for origin in self.CORS_ORIGINS.split(",")}:
            failures.append("an explicit CORS origin allowlist")
        if failures:
            raise ValueError(
                "Enterprise deployment profile requires: " + ", ".join(failures)
            )
        return self


settings = Settings()
