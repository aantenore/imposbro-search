"""
Dependency injection: read services from app.state (set in lifespan).
Allows tests and production to use the same code path without mutating router modules.
"""
from fastapi import Request

from services import FederationService, StateManager, KafkaService, SyncConfigNotifier


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
