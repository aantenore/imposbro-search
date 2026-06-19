"""
Dependency injection: read services from app.state (set in lifespan).
Allows tests and production to use the same code path without mutating router modules.
"""
import json
import secrets
from typing import List, Optional, Set, Tuple

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


def _parse_scoped_api_keys() -> List[Tuple[str, Set[str]]]:
    """Parse SCOPED_API_KEYS as a JSON array of {key, scopes} entries."""
    raw = settings.SCOPED_API_KEYS.strip()
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail="Invalid SCOPED_API_KEYS configuration: expected JSON array",
        ) from exc
    if not isinstance(entries, list):
        raise HTTPException(
            status_code=500,
            detail="Invalid SCOPED_API_KEYS configuration: expected JSON array",
        )

    parsed: List[Tuple[str, Set[str]]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=500,
                detail="Invalid SCOPED_API_KEYS entry: expected object",
            )
        key = str(entry.get("key", "")).strip()
        scopes_value = entry.get("scopes", [])
        if isinstance(scopes_value, str):
            scopes = {scope.strip().lower() for scope in scopes_value.split(",")}
        elif isinstance(scopes_value, list):
            scopes = {str(scope).strip().lower() for scope in scopes_value}
        else:
            scopes = set()
        scopes.discard("")
        if not key or not scopes:
            raise HTTPException(
                status_code=500,
                detail="Invalid SCOPED_API_KEYS entry: key and scopes are required",
            )
        parsed.append((key, scopes))
    return parsed


def _scope_matches(scopes: Set[str], required_scope: str) -> bool:
    if "*" in scopes:
        return True
    if required_scope in scopes:
        return True
    if required_scope in {"search", "ingest"} and (
        "data" in scopes or "data:*" in scopes
    ):
        return True
    if required_scope == "data" and ("data" in scopes or "data:*" in scopes):
        return True
    if required_scope == "admin" and "admin:*" in scopes:
        return True
    return False


def _candidate_keys_for_scope(required_scope: str) -> List[str]:
    candidates: List[str] = []
    if required_scope == "admin" and settings.ADMIN_API_KEY:
        candidates.append(settings.ADMIN_API_KEY)
    if required_scope in {"search", "ingest", "data"} and settings.DATA_API_KEY:
        candidates.append(settings.DATA_API_KEY)
    for key, scopes in _parse_scoped_api_keys():
        if _scope_matches(scopes, required_scope):
            candidates.append(key)
    return candidates


def _require_api_key_for_scope(
    *,
    required_scope: str,
    allow_unauthenticated: bool,
    missing_detail: str,
    x_api_key: Optional[str],
    authorization: Optional[str],
) -> None:
    candidates = _candidate_keys_for_scope(required_scope)
    if not candidates:
        if allow_unauthenticated:
            return
        raise HTTPException(status_code=401, detail=missing_detail)

    provided = _extract_api_key(x_api_key, authorization)
    if provided and any(
        secrets.compare_digest(provided, candidate) for candidate in candidates
    ):
        return
    raise HTTPException(status_code=401, detail="Invalid or missing API key")


def require_admin_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """Require admin scope unless the local-development bypass is enabled."""
    _require_api_key_for_scope(
        required_scope="admin",
        allow_unauthenticated=settings.ALLOW_UNAUTHENTICATED_ADMIN,
        missing_detail="Admin API key is required",
        x_api_key=x_api_key,
        authorization=authorization,
    )


def require_data_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """Backward-compatible data-plane guard for search and ingestion."""
    _require_api_key_for_scope(
        required_scope="data",
        allow_unauthenticated=settings.ALLOW_UNAUTHENTICATED_DATA,
        missing_detail="Data API key is required",
        x_api_key=x_api_key,
        authorization=authorization,
    )


def require_search_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """Require search scope unless the local-development bypass is enabled."""
    _require_api_key_for_scope(
        required_scope="search",
        allow_unauthenticated=settings.ALLOW_UNAUTHENTICATED_DATA,
        missing_detail="Search API key is required",
        x_api_key=x_api_key,
        authorization=authorization,
    )


def require_ingest_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """Require ingest scope unless the local-development bypass is enabled."""
    _require_api_key_for_scope(
        required_scope="ingest",
        allow_unauthenticated=settings.ALLOW_UNAUTHENTICATED_DATA,
        missing_detail="Ingest API key is required",
        x_api_key=x_api_key,
        authorization=authorization,
    )


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
