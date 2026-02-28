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


settings = Settings()
