"""Stable HTTP API versioning and RFC 9457-compatible error responses."""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from http import HTTPStatus
from typing import Any, Dict

from fastapi import HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from constants import API_MAJOR_VERSION, API_VERSION_HEADER, API_VERSION_PREFIX, VERSION
from observability import get_request_id


logger = logging.getLogger(__name__)
PROBLEM_BASE_URL = "https://imposbro.dev/problems"
_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_failed",
    428: "precondition_required",
    429: "rate_limited",
    500: "internal_error",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "gateway_timeout",
}


def is_versioned_request(request: Request) -> bool:
    """Return whether a request uses the stable major-version URI contract."""
    path = request.scope.get("path", "")
    return path == API_VERSION_PREFIX or path.startswith(API_VERSION_PREFIX + "/")


def _title(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Request failed"


def _normalize_detail(detail: Any, status_code: int) -> tuple[str, str, Dict[str, Any]]:
    code = _STATUS_CODES.get(status_code, "request_failed")
    extensions: Dict[str, Any] = {}
    if isinstance(detail, dict):
        candidate = detail.get("code")
        if isinstance(candidate, str) and _CODE_PATTERN.fullmatch(candidate):
            code = candidate
        message = detail.get("message") or detail.get("detail") or _title(status_code)
        extensions = {
            key: value
            for key, value in detail.items()
            if key not in {"code", "message", "detail"}
        }
    elif isinstance(detail, str) and detail:
        message = detail
    else:
        message = _title(status_code)
        if detail is not None:
            extensions["context"] = detail
    return code, str(message), extensions


def problem_response(
    request: Request,
    *,
    status_code: int,
    detail: Any,
    headers: Dict[str, str] | None = None,
) -> JSONResponse:
    """Build the canonical versioned error envelope without leaking internals."""
    code, message, extensions = _normalize_detail(detail, status_code)
    body: Dict[str, Any] = {
        "type": f"{PROBLEM_BASE_URL}/{code}",
        "title": _title(status_code),
        "status": status_code,
        "detail": message,
        "instance": request.url.path,
        "code": code,
        "request_id": get_request_id(request),
        "api_version": str(API_MAJOR_VERSION),
    }
    body.update(extensions)
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers=headers,
        media_type="application/problem+json",
    )


async def http_problem_handler(
    request: Request,
    exc: StarletteHTTPException,
):
    """Preserve legacy errors while enforcing Problem Details on /api/v1."""
    if not is_versioned_request(request):
        return await http_exception_handler(request, exc)
    detail = exc.detail
    if exc.status_code == 500:
        detail = {
            "code": "internal_error",
            "message": "An unexpected internal error occurred",
        }
    return problem_response(
        request,
        status_code=exc.status_code,
        detail=detail,
        headers=dict(exc.headers or {}),
    )


async def validation_problem_handler(
    request: Request,
    exc: RequestValidationError,
):
    """Return stable validation diagnostics for the versioned API."""
    if not is_versioned_request(request):
        return await request_validation_exception_handler(request, exc)
    errors = []
    for error in exc.errors():
        errors.append(
            {
                "location": [str(item) for item in error.get("loc", ())],
                "message": str(error.get("msg", "Invalid value")),
                "code": str(error.get("type", "value_error")),
            }
        )
    return problem_response(
        request,
        status_code=422,
        detail={
            "code": "validation_failed",
            "message": "Request validation failed",
            "errors": errors,
        },
    )


async def unhandled_problem_handler(request: Request, exc: Exception):
    """Redact unexpected exceptions while retaining the request correlation id."""
    logger.exception(
        "Unhandled API error request_id=%s path=%s",
        get_request_id(request),
        request.url.path,
        exc_info=exc,
    )
    if is_versioned_request(request):
        return problem_response(
            request,
            status_code=500,
            detail={
                "code": "internal_error",
                "message": "An unexpected internal error occurred",
            },
        )
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


EXCEPTION_HANDLERS = {
    HTTPException: http_problem_handler,
    StarletteHTTPException: http_problem_handler,
    RequestValidationError: validation_problem_handler,
    Exception: unhandled_problem_handler,
}


def stable_operation_id(route: APIRoute) -> str:
    """Generate deterministic IDs that remain distinct for legacy aliases."""
    method = sorted(route.methods or {"GET"})[0].lower()
    path = re.sub(r"[^a-zA-Z0-9]+", "_", route.path_format).strip("_").lower()
    return f"{route.name}_{method}_{path}"


def _problem_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": True,
        "required": [
            "type",
            "title",
            "status",
            "detail",
            "instance",
            "code",
            "request_id",
            "api_version",
        ],
        "properties": {
            "type": {"type": "string", "format": "uri"},
            "title": {"type": "string"},
            "status": {"type": "integer", "minimum": 400, "maximum": 599},
            "detail": {"type": "string"},
            "instance": {"type": "string"},
            "code": {"type": "string", "pattern": _CODE_PATTERN.pattern},
            "request_id": {"type": "string"},
            "api_version": {"type": "string", "const": str(API_MAJOR_VERSION)},
            "errors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["location", "message", "code"],
                    "properties": {
                        "location": {"type": "array", "items": {"type": "string"}},
                        "message": {"type": "string"},
                        "code": {"type": "string"},
                    },
                },
            },
        },
    }


def build_openapi_schema(app, *, versioned_only: bool) -> Dict[str, Any]:
    """Generate the public contract and attach uniform response metadata."""
    schema = get_openapi(
        title=app.title,
        version=f"{API_MAJOR_VERSION}.0.0" if versioned_only else app.version,
        description=app.description,
        routes=app.routes,
    )
    components = schema.setdefault("components", {})
    components.setdefault("schemas", {})["ProblemDetail"] = _problem_schema()
    components.setdefault("headers", {})["RequestId"] = {
        "description": "Request correlation identifier.",
        "schema": {"type": "string"},
    }
    components["headers"]["ApiVersion"] = {
        "description": "Stable major API version.",
        "schema": {"type": "string", "const": str(API_MAJOR_VERSION)},
    }
    problem_response = {
        "description": "Request failed",
        "headers": {
            API_VERSION_HEADER: {"$ref": "#/components/headers/ApiVersion"},
            "X-Request-ID": {"$ref": "#/components/headers/RequestId"},
        },
        "content": {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/ProblemDetail"}
            }
        },
    }
    versioned_paths = {}
    for path, path_item in schema.get("paths", {}).items():
        if not (path == API_VERSION_PREFIX or path.startswith(API_VERSION_PREFIX + "/")):
            continue
        for method, operation in path_item.items():
            if method.lower() not in {
                "get",
                "put",
                "post",
                "delete",
                "options",
                "head",
                "patch",
                "trace",
            }:
                continue
            responses = operation.setdefault("responses", {})
            for status, response in responses.items():
                if str(status).startswith("2"):
                    headers = response.setdefault("headers", {})
                    headers.setdefault(
                        API_VERSION_HEADER,
                        {"$ref": "#/components/headers/ApiVersion"},
                    )
                    headers.setdefault(
                        "X-Request-ID",
                        {"$ref": "#/components/headers/RequestId"},
                    )
            responses["default"] = deepcopy(problem_response)
            if "422" in responses:
                responses["422"] = deepcopy(problem_response)
                responses["422"]["description"] = "Request validation failed"
        versioned_paths[path] = path_item
    schema["info"]["x-application-version"] = VERSION
    schema["info"]["x-api-major-version"] = API_MAJOR_VERSION
    if versioned_only:
        schema["paths"] = versioned_paths
        schema["servers"] = [{"url": API_VERSION_PREFIX}]
    return schema
