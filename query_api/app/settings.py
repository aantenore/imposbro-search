# query_api/app/settings.py
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Kafka Configuration
    KAFKA_BROKER_URL: str
    KAFKA_TOPIC_PREFIX: str

    # Typesense HA Cluster Configuration
    TYPESENSE_NODES: str  # Comma-separated list of nodes, e.g., "typesense-1,typesense-2"
    TYPESENSE_API_KEY: str

    # Redis Configuration
    REDIS_URL: str

    # Internal Service URL (used by other services like the indexer)
    INTERNAL_QUERY_API_URL: str

    class Config:
        env_file = '.env'
        env_file_encoding = 'utf-8'


settings = Settings()
