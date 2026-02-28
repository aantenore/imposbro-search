"""
Dependency injection: read services from app.state (set in lifespan).
Allows tests and production to use the same code path without mutating router modules.
"""
from typing import Optional

from fastapi import Request, Header, HTTPException

from settings import settings
from services import FederationService, StateManager, KafkaService, SyncConfigNotifier


def require_admin_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """If ADMIN_API_KEY is set, require X-API-Key or Authorization: Bearer to match."""
    if not settings.ADMIN_API_KEY:
        return
    provided = x_api_key
    if not provided and authorization and authorization.startswith("Bearer "):
        provided = authorization[7:].strip()
    if not provided or provided != settings.ADMIN_API_KEY:
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
