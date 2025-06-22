from pydantic_settings import BaseSettings
class Settings(BaseSettings):
    KAFKA_BROKER_URL: str
    KAFKA_TOPIC_PREFIX: str
    TYPESENSE_HOST: str
    TYPESENSE_PORT: int
    TYPESENSE_API_KEY: str
    REDIS_URL: str = "redis://localhost:6379"
    
    class Config:
        env_file = '.env'
        env_file_encoding = 'utf-8'

settings = Settings()
