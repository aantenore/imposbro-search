"""
Dependency injection: read services from app.state (set in lifespan).
Allows tests and production to use the same code path without mutating router modules.
"""
import logging
import json
import hashlib
import fnmatch
import secrets
from typing import List, Optional, Set, Tuple

from fastapi import Request, Header, HTTPException, Path
from prometheus_client import Counter

from auth import authenticate_oidc_bearer, oidc_enabled, oidc_http_exception
from constants import NAME_PATTERN
from settings import settings
from services import (
    FederationService,
    StateManager,
    KafkaService,
    SyncConfigNotifier,
    FixedWindowRateLimiter,
    RateLimitBackendError,
    RateLimitConfigError,
)


logger = logging.getLogger(__name__)

RATE_LIMIT_CHECKS = Counter(
    "query_api_rate_limit_checks_total",
    "Total Query API data-plane rate-limit decisions.",
    ["action", "collection", "result"],
)
RATE_LIMIT_BACKEND_ERRORS = Counter(
    "query_api_rate_limit_backend_errors_total",
    "Total Query API data-plane rate-limit backend failures.",
    ["action", "backend", "mode"],
)


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


def _resource_scope_matches(scope: str, base_scope: str, resource_name: str) -> bool:
    prefix = f"{base_scope}:"
    if not scope.startswith(prefix):
        return False
    pattern = scope[len(prefix):].strip()
    return bool(pattern) and fnmatch.fnmatchcase(
        resource_name.casefold(),
        pattern.casefold(),
    )


def _scope_matches(
    scopes: Set[str],
    required_scope: str,
    resource_name: Optional[str] = None,
) -> bool:
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
    if required_scope.startswith("admin:") and (
        "admin" in scopes or "admin:*" in scopes
    ):
        return True
    if resource_name:
        if required_scope == "search":
            return any(
                _resource_scope_matches(scope, "search", resource_name)
                or _resource_scope_matches(scope, "data", resource_name)
                for scope in scopes
            )
        if required_scope == "ingest":
            return any(
                _resource_scope_matches(scope, "ingest", resource_name)
                or _resource_scope_matches(scope, "data", resource_name)
                for scope in scopes
            )
        if required_scope == "data":
            return any(
                _resource_scope_matches(scope, "data", resource_name)
                for scope in scopes
            )
    return False


def _candidate_keys_for_scope(
    required_scope: str,
    resource_name: Optional[str] = None,
) -> List[str]:
    candidates: List[str] = []
    if (
        (required_scope == "admin" or required_scope.startswith("admin:"))
        and settings.ADMIN_API_KEY
    ):
        candidates.append(settings.ADMIN_API_KEY)
    if required_scope in {"search", "ingest", "data"} and settings.DATA_API_KEY:
        candidates.append(settings.DATA_API_KEY)
    for key, scopes in _parse_scoped_api_keys():
        if _scope_matches(scopes, required_scope, resource_name):
            candidates.append(key)
    return candidates


def _require_api_key_for_scope(
    *,
    request: Request,
    required_scope: str,
    allow_unauthenticated: bool,
    missing_detail: str,
    x_api_key: Optional[str],
    authorization: Optional[str],
    resource_name: Optional[str] = None,
) -> None:
    candidates = _candidate_keys_for_scope(required_scope, resource_name)
    oidc_is_enabled = oidc_enabled()
    if not candidates and not oidc_is_enabled:
        if allow_unauthenticated:
            request.state.auth_actor = "unauthenticated-dev"
            request.state.auth_scheme = "none"
            return
        raise HTTPException(status_code=401, detail=missing_detail)

    provided = _extract_api_key(x_api_key, authorization)
    if provided and any(
        secrets.compare_digest(provided, candidate) for candidate in candidates
    ):
        request.state.auth_actor = _api_key_actor(provided)
        request.state.auth_scheme = "api_key"
        return
    if authorization and authorization.startswith("Bearer ") and oidc_is_enabled:
        token = authorization[7:].strip()
        try:
            auth_result = authenticate_oidc_bearer(
                token,
                required_scope,
                resource_name,
            )
        except Exception as exc:
            raise oidc_http_exception(exc) from exc
        if auth_result:
            request.state.auth_actor = auth_result["actor"]
            request.state.auth_scheme = "oidc"
            request.state.auth_claims = auth_result["claims"]
            return
    raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _api_key_actor(api_key: str) -> str:
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
    return f"api_key:{digest}"


def require_admin_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """Require admin scope unless the local-development bypass is enabled."""
    _require_api_key_for_scope(
        request=request,
        required_scope="admin",
        allow_unauthenticated=settings.ALLOW_UNAUTHENTICATED_ADMIN,
        missing_detail="Admin API key is required",
        x_api_key=x_api_key,
        authorization=authorization,
    )


def require_admin_read_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """Require permission to inspect non-sensitive admin state."""
    _require_admin_operation_key(
        request=request,
        required_scope="admin:read",
        x_api_key=x_api_key,
        authorization=authorization,
    )


def require_admin_write_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """Require permission to mutate clusters, collections, aliases, or routing."""
    _require_admin_operation_key(
        request=request,
        required_scope="admin:write",
        x_api_key=x_api_key,
        authorization=authorization,
    )


def require_admin_backup_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """Require permission to export control-plane backup material."""
    _require_admin_operation_key(
        request=request,
        required_scope="admin:backup",
        x_api_key=x_api_key,
        authorization=authorization,
    )


def require_admin_restore_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """Require permission to validate or apply control-plane restore material."""
    _require_admin_operation_key(
        request=request,
        required_scope="admin:restore",
        x_api_key=x_api_key,
        authorization=authorization,
    )


def require_admin_internal_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """Require permission to read raw internal service configuration."""
    _require_admin_operation_key(
        request=request,
        required_scope="admin:internal",
        x_api_key=x_api_key,
        authorization=authorization,
    )


def _require_admin_operation_key(
    *,
    request: Request,
    required_scope: str,
    x_api_key: Optional[str],
    authorization: Optional[str],
) -> None:
    _require_api_key_for_scope(
        request=request,
        required_scope=required_scope,
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
        request=request,
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
        request=request,
        required_scope="search",
        allow_unauthenticated=settings.ALLOW_UNAUTHENTICATED_DATA,
        missing_detail="Search API key is required",
        x_api_key=x_api_key,
        authorization=authorization,
    )
    _enforce_data_plane_rate_limit(request, "search")


def require_ingest_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """Require ingest scope unless the local-development bypass is enabled."""
    _require_api_key_for_scope(
        request=request,
        required_scope="ingest",
        allow_unauthenticated=settings.ALLOW_UNAUTHENTICATED_DATA,
        missing_detail="Ingest API key is required",
        x_api_key=x_api_key,
        authorization=authorization,
    )
    _enforce_data_plane_rate_limit(request, "ingest")


def require_search_collection_api_key(
    request: Request,
    collection_name: str = Path(..., pattern=NAME_PATTERN),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """Require search scope for the requested collection."""
    _require_api_key_for_scope(
        request=request,
        required_scope="search",
        resource_name=collection_name,
        allow_unauthenticated=settings.ALLOW_UNAUTHENTICATED_DATA,
        missing_detail="Search API key is required",
        x_api_key=x_api_key,
        authorization=authorization,
    )
    _enforce_data_plane_rate_limit(request, "search", collection_name)


def require_ingest_collection_api_key(
    request: Request,
    collection_name: str = Path(..., pattern=NAME_PATTERN),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """Require ingest scope for the requested collection."""
    _require_api_key_for_scope(
        request=request,
        required_scope="ingest",
        resource_name=collection_name,
        allow_unauthenticated=settings.ALLOW_UNAUTHENTICATED_DATA,
        missing_detail="Ingest API key is required",
        x_api_key=x_api_key,
        authorization=authorization,
    )
    _enforce_data_plane_rate_limit(request, "ingest", collection_name)


def _get_rate_limiter(request: Request) -> FixedWindowRateLimiter:
    if not hasattr(request.app.state, "rate_limiter"):
        request.app.state.rate_limiter = FixedWindowRateLimiter(settings.REDIS_URL)
    return request.app.state.rate_limiter


def _rate_limit_identity(request: Request) -> str:
    actor = getattr(request.state, "auth_actor", "")
    if actor:
        return str(actor)
    if request.client and request.client.host:
        return f"ip:{request.client.host}"
    return "unknown"


def _data_plane_limit_for_action(action: str) -> int:
    if action == "search":
        return settings.RATE_LIMIT_SEARCH_REQUESTS
    if action == "ingest":
        return settings.RATE_LIMIT_INGEST_REQUESTS
    raise RateLimitConfigError(f"Unsupported rate-limit action '{action}'")


def _enforce_data_plane_rate_limit(
    request: Request,
    action: str,
    collection_name: str = "*",
) -> None:
    if not settings.RATE_LIMIT_ENABLED:
        return

    try:
        result = _get_rate_limiter(request).check(
            identity=_rate_limit_identity(request),
            action=action,
            collection=collection_name,
            limit=_data_plane_limit_for_action(action),
            window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
            backend=settings.RATE_LIMIT_BACKEND,
            key_prefix=settings.RATE_LIMIT_REDIS_PREFIX,
        )
    except RateLimitConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except RateLimitBackendError as exc:
        mode = "fail_closed" if settings.RATE_LIMIT_FAIL_CLOSED else "fail_open"
        RATE_LIMIT_BACKEND_ERRORS.labels(
            action=action,
            backend=settings.RATE_LIMIT_BACKEND,
            mode=mode,
        ).inc()
        if settings.RATE_LIMIT_FAIL_CLOSED:
            raise HTTPException(
                status_code=503,
                detail="Rate-limit backend unavailable",
            ) from exc
        logger.warning("Rate-limit backend unavailable; allowing request: %s", exc)
        return

    RATE_LIMIT_CHECKS.labels(
        action=action,
        collection=collection_name,
        result="allowed" if result.allowed else "blocked",
    ).inc()
    if not result.allowed:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={
                "Retry-After": str(result.reset_seconds),
                "X-RateLimit-Limit": str(result.limit),
                "X-RateLimit-Remaining": str(result.remaining),
                "X-RateLimit-Reset": str(result.reset_seconds),
            },
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
