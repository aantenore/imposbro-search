from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    KAFKA_BROKER_URL: str
    KAFKA_TOPIC_PREFIX: str
    REDIS_URL: str

    # Internal State Cluster
    INTERNAL_STATE_NODES: str
    INTERNAL_STATE_API_KEY: str

    # Default Federated Data Cluster
    DEFAULT_DATA_CLUSTER_NODES: str
    DEFAULT_DATA_CLUSTER_API_KEY: str

    # Secondary Federated Data Cluster (optional; leave empty to disable)
    DEFAULT_DATA2_CLUSTER_NODES: str = ""
    DEFAULT_DATA2_CLUSTER_API_KEY: str = ""

    INTERNAL_QUERY_API_URL: str

    # CORS: comma-separated origins (e.g. "http://localhost:3001,https://admin.example.com")
    # Empty string = CORS middleware not added (same-origin only)
    CORS_ORIGINS: str = ""

    # Optional API key for Admin API. If set, all /admin/* requests must include
    # header X-API-Key: <value> or Authorization: Bearer <value>
    ADMIN_API_KEY: str = ""

    # Local-development escape hatch. Keep false in shared, exposed, or production environments.
    ALLOW_UNAUTHENTICATED_ADMIN: bool = False

    # Optional API key for data-plane endpoints (/ingest and /search).
    # Keep separate from ADMIN_API_KEY so operators can grant least-privilege access.
    DATA_API_KEY: str = ""

    # Local-development escape hatch. Keep false in shared, exposed, or production environments.
    ALLOW_UNAUTHENTICATED_DATA: bool = False

    # Admin audit log. Records successful control-plane mutations to the
    # internal state cluster without storing raw API keys or cluster secrets.
    AUDIT_LOG_ENABLED: bool = True
    AUDIT_LOG_MAX_RESULTS: int = 100


settings = Settings()
