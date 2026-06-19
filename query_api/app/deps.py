"""
Dependency injection: read services from app.state (set in lifespan).
Allows tests and production to use the same code path without mutating router modules.
"""
from typing import Optional

from fastapi import Request, Header, HTTPException

from settings import settings
from services import FederationService, StateManager, KafkaService, SyncConfigNotifier


def _extract_api_key(
    x_api_key: Optional[str],
    authorization: Optional[str],
) -> Optional[str]:
    """Read API keys from X-API-Key or Authorization: Bearer."""
    if x_api_key:
        return x_api_key
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:].strip()
    return None


def require_admin_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """If ADMIN_API_KEY is set, require X-API-Key or Authorization: Bearer to match."""
    if not settings.ADMIN_API_KEY:
        if settings.ALLOW_UNAUTHENTICATED_ADMIN:
            return
        raise HTTPException(status_code=401, detail="Admin API key is required")
    provided = _extract_api_key(x_api_key, authorization)
    if not provided or provided != settings.ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def require_data_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """Require a data-plane API key for search and ingestion unless dev bypass is enabled."""
    if not settings.DATA_API_KEY:
        if settings.ALLOW_UNAUTHENTICATED_DATA:
            return
        raise HTTPException(status_code=401, detail="Data API key is required")
    provided = _extract_api_key(x_api_key, authorization)
    if not provided or provided != settings.DATA_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def get_federation_service(request: Request) -> FederationService:
    if not hasattr(request.app.state, "federation_service"):
        raise NotImplementedError("Federation service not initialized")
    return request.app.state.federation_service


def get_state_manager(request: Request) -> StateManager:
    if not hasattr(request.app.state, "state_manager"):
        raise NotImplementedError("State manager not initialized")
    return request.app.state.state_manager


def get_config_notifier(request: Request) -> SyncConfigNotifier:
    if not hasattr(request.app.state, "config_notifier"):
        raise NotImplementedError("Config notifier not initialized")
    return request.app.state.config_notifier


def get_kafka_service(request: Request) -> KafkaService:
    if not hasattr(request.app.state, "kafka_service"):
        raise NotImplementedError("Kafka service not initialized")
    return request.app.state.kafka_service
