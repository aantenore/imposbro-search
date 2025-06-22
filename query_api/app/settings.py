from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    KAFKA_BROKER_URL: str
    KAFKA_TOPIC_PREFIX: str
    REDIS_URL: str

    # Internal State Cluster
    INTERNAL_STATE_NODES: str
    INTERNAL_STATE_API_KEY: str

    # Default Federated Data Cluster
    DEFAULT_DATA_CLUSTER_NODES: str
    DEFAULT_DATA_CLUSTER_API_KEY: str

    # Secondary Federated Data Cluster
    DEFAULT_DATA2_CLUSTER_NODES: str
    DEFAULT_DATA2_CLUSTER_API_KEY: str

    INTERNAL_QUERY_API_URL: str

    class Config:
        env_file = '.env'
        env_file_encoding = 'utf-8'


settings = Settings()
