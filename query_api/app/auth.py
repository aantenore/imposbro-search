"""Authentication helpers for API-key and OIDC/JWT authorization."""
import fnmatch
import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Set

import jwt
from fastapi import HTTPException, Request
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError

from settings import settings


VALID_INTERNAL_SCOPES = {"admin", "search", "ingest", "data"}
DEFAULT_OIDC_SCOPE_MAPPING: Dict[str, List[str]] = {
    "admin": ["imposbro:admin", "imposbro:*"],
    "search": ["imposbro:search", "imposbro:data", "imposbro:*"],
    "ingest": ["imposbro:ingest", "imposbro:data", "imposbro:*"],
    "data": ["imposbro:data", "imposbro:*"],
}
TENANT_FIELD_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
TENANT_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
VALID_TENANT_MODES = {"off", "required", "inject"}


class OidcConfigError(ValueError):
    """Raised when OIDC is enabled but configuration is invalid."""


class OidcTokenError(ValueError):
    """Raised when a bearer token is invalid or lacks required authorization."""


def oidc_enabled() -> bool:
    return bool(settings.OIDC_ENABLED)


def _split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _oidc_algorithms() -> List[str]:
    algorithms = _split_csv(settings.OIDC_ALGORITHMS)
    if not algorithms:
        raise OidcConfigError("OIDC_ALGORITHMS must contain at least one algorithm")
    if any(algorithm.upper().startswith("HS") for algorithm in algorithms):
        raise OidcConfigError("OIDC_ALGORITHMS must use asymmetric algorithms")
    return algorithms


def _parse_scope_mapping() -> Dict[str, Set[str]]:
    raw = settings.OIDC_SCOPE_MAPPING.strip()
    if not raw:
        return {
            scope: {claim.casefold() for claim in claims}
            for scope, claims in DEFAULT_OIDC_SCOPE_MAPPING.items()
        }
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OidcConfigError("OIDC_SCOPE_MAPPING must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise OidcConfigError("OIDC_SCOPE_MAPPING must be a JSON object")

    mapping: Dict[str, Set[str]] = {}
    for internal_scope, values in parsed.items():
        scope = str(internal_scope).strip().lower()
        if scope not in VALID_INTERNAL_SCOPES:
            raise OidcConfigError(
                f"OIDC_SCOPE_MAPPING contains unsupported internal scope '{scope}'"
            )
        if isinstance(values, str):
            claims = _split_csv(values)
        elif isinstance(values, list):
            claims = [str(value).strip() for value in values if str(value).strip()]
        else:
            raise OidcConfigError(
                f"OIDC_SCOPE_MAPPING value for '{scope}' must be a string or list"
            )
        if not claims:
            raise OidcConfigError(
                f"OIDC_SCOPE_MAPPING value for '{scope}' must not be empty"
            )
        mapping[scope] = {claim.casefold() for claim in claims}
    return mapping


def _get_claim_path(claims: Dict[str, Any], path: str) -> Any:
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _flatten_claim_values(value: Any) -> Iterable[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item for item in value.replace(",", " ").split() if item]
    if isinstance(value, (list, tuple, set)):
        flattened: List[str] = []
        for item in value:
            flattened.extend(_flatten_claim_values(item))
        return flattened
    return [str(value)]


def _token_grants(claims: Dict[str, Any]) -> Set[str]:
    grants: Set[str] = set()
    for claim_path in _split_csv(settings.OIDC_SCOPE_CLAIMS):
        for value in _flatten_claim_values(_get_claim_path(claims, claim_path)):
            grants.add(value.casefold())
    return grants


def _has_required_scope(claims: Dict[str, Any], required_scope: str) -> bool:
    mapping = _parse_scope_mapping()
    allowed_claims = mapping.get(required_scope, set())
    token_claims = _token_grants(claims)
    return bool(allowed_claims.intersection(token_claims))


def _static_public_key() -> str:
    return settings.OIDC_PUBLIC_KEY.replace("\\n", "\n").strip()


def _decode_oidc_token(token: str) -> Dict[str, Any]:
    issuer = settings.OIDC_ISSUER.strip()
    audience = settings.OIDC_AUDIENCE.strip()
    jwks_url = settings.OIDC_JWKS_URL.strip()
    public_key = _static_public_key()

    if not issuer:
        raise OidcConfigError("OIDC_ISSUER is required when OIDC is enabled")
    if not audience:
        raise OidcConfigError("OIDC_AUDIENCE is required when OIDC is enabled")
    if bool(jwks_url) == bool(public_key):
        raise OidcConfigError(
            "Configure exactly one of OIDC_JWKS_URL or OIDC_PUBLIC_KEY"
        )

    algorithms = _oidc_algorithms()
    key: Any = public_key
    if jwks_url:
        try:
            key = PyJWKClient(jwks_url).get_signing_key_from_jwt(token).key
        except PyJWKClientError as exc:
            raise OidcTokenError("Unable to resolve OIDC signing key") from exc

    try:
        return jwt.decode(
            token,
            key=key,
            algorithms=algorithms,
            audience=audience,
            issuer=issuer,
            leeway=settings.OIDC_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "sub"]},
        )
    except InvalidTokenError as exc:
        raise OidcTokenError("Invalid OIDC bearer token") from exc


def authenticate_oidc_bearer(token: str, required_scope: str) -> Optional[Dict[str, Any]]:
    """Validate an OIDC bearer token and return actor metadata."""
    if not oidc_enabled():
        return None
    claims = _decode_oidc_token(token)
    if not _has_required_scope(claims, required_scope):
        raise OidcTokenError("OIDC bearer token lacks required scope")

    subject_claim = settings.OIDC_SUBJECT_CLAIM.strip() or "sub"
    subject = str(_get_claim_path(claims, subject_claim) or claims.get("sub", "unknown"))
    issuer = str(claims.get("iss", "unknown"))
    digest = hashlib.sha256(f"{issuer}\0{subject}".encode("utf-8")).hexdigest()[:16]
    return {
        "actor": f"oidc:{digest}",
        "claims": claims,
    }


def _authz_policies() -> Dict[str, Any]:
    raw = settings.AUTHZ_COLLECTION_POLICIES.strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail="AUTHZ_COLLECTION_POLICIES must be a JSON object",
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=500,
            detail="AUTHZ_COLLECTION_POLICIES must be a JSON object",
        )
    return parsed


def _collection_policy(collection_name: str) -> Dict[str, Any]:
    policies = _authz_policies()
    if not policies:
        return {"mode": "off"}
    if "mode" in policies or "tenant_field" in policies:
        return dict(policies)

    default_policy = dict(policies.get("default", {}))
    for pattern, policy in dict(policies.get("collections", {})).items():
        if fnmatch.fnmatchcase(collection_name, str(pattern)):
            if not isinstance(policy, dict):
                raise HTTPException(
                    status_code=500,
                    detail=f"AUTHZ_COLLECTION_POLICIES entry '{pattern}' must be an object",
                )
            merged = dict(default_policy)
            merged.update(policy)
            return merged
    return default_policy or {"mode": "off"}


def _policy_mode(policy: Dict[str, Any]) -> str:
    mode = str(policy.get("mode", "off")).strip().lower()
    if mode not in VALID_TENANT_MODES:
        raise HTTPException(
            status_code=500,
            detail=f"Unsupported AUTHZ collection policy mode '{mode}'",
        )
    return mode


def _tenant_field(policy: Dict[str, Any]) -> str:
    field = str(policy.get("tenant_field", "tenant_id")).strip()
    if not TENANT_FIELD_PATTERN.fullmatch(field):
        raise HTTPException(
            status_code=500,
            detail="AUTHZ tenant_field must be a safe Typesense field path",
        )
    return field


def _tenant_values_from_request(
    request: Request,
    policy: Dict[str, Any],
) -> Optional[List[str]]:
    if getattr(request.state, "auth_scheme", "") == "api_key" and (
        settings.AUTHZ_API_KEY_TENANT_BYPASS
    ):
        return None

    claims = getattr(request.state, "auth_claims", None)
    if not isinstance(claims, dict):
        raise HTTPException(status_code=403, detail="Tenant claim is required")

    claim_path = str(policy.get("tenant_claim", "tenant_id")).strip() or "tenant_id"
    values = [
        value
        for value in _flatten_claim_values(_get_claim_path(claims, claim_path))
        if value
    ]
    if not values:
        raise HTTPException(status_code=403, detail="Tenant claim is required")

    normalized: List[str] = []
    for value in values:
        if not TENANT_VALUE_PATTERN.fullmatch(value):
            raise HTTPException(status_code=403, detail="Tenant claim is not allowed")
        if value not in normalized:
            normalized.append(value)
    return normalized


def _tenant_filter(field: str, tenants: List[str]) -> str:
    if len(tenants) == 1:
        return f"{field}:={tenants[0]}"
    return f"{field}:=[{','.join(tenants)}]"


def authorize_search_request(request: Request, collection_name: str, search_request: Any) -> Any:
    """Apply configured tenant policy to a search request."""
    policy = _collection_policy(collection_name)
    if _policy_mode(policy) == "off":
        return search_request

    tenants = _tenant_values_from_request(request, policy)
    if tenants is None:
        return search_request

    tenant_filter = _tenant_filter(_tenant_field(policy), tenants)
    current_filter = (search_request.filter_by or "").strip()
    combined_filter = (
        f"({current_filter}) && {tenant_filter}"
        if current_filter
        else tenant_filter
    )
    return search_request.model_copy(update={"filter_by": combined_filter})


def authorize_ingest_document(
    request: Request,
    collection_name: str,
    document: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate or inject tenant ownership for an ingested document."""
    policy = _collection_policy(collection_name)
    mode = _policy_mode(policy)
    if mode == "off":
        return document

    tenants = _tenant_values_from_request(request, policy)
    if tenants is None:
        return document

    field = _tenant_field(policy)
    current_value = document.get(field)
    if current_value in (None, "") and mode == "inject":
        if len(tenants) != 1:
            raise HTTPException(
                status_code=403,
                detail="Cannot inject tenant when token has multiple tenants",
            )
        document = dict(document)
        document[field] = tenants[0]
        return document

    if str(current_value) not in tenants:
        raise HTTPException(status_code=403, detail="Document tenant is not allowed")
    return document


def oidc_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, OidcConfigError):
        return HTTPException(status_code=500, detail=str(exc))
    return HTTPException(status_code=401, detail=str(exc))
