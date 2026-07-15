"""Request correlation helpers for API and async indexing flows."""

import re
import secrets
import uuid
from typing import Optional

from starlette.requests import Request


REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
TRACEPARENT_PATTERN = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<parent_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)


def normalize_request_id(value: Optional[str]) -> str:
    """Return a safe request id from a trusted header or generate a new one."""
    if value:
        candidate = value.strip()
        if REQUEST_ID_PATTERN.fullmatch(candidate):
            return candidate
    return uuid.uuid4().hex


def get_request_id(request: Request) -> str:
    """Read the request id set by middleware, falling back defensively."""
    request_id = getattr(request.state, "request_id", None)
    return normalize_request_id(request_id)


def normalize_traceparent(value: Optional[str]) -> str:
    """Validate W3C trace context or start a new sampled server trace."""
    candidate = (value or "").strip().lower()
    match = TRACEPARENT_PATTERN.fullmatch(candidate)
    if (
        match
        and match.group("version") != "ff"
        and match.group("trace_id") != "0" * 32
        and match.group("parent_id") != "0" * 16
    ):
        return candidate
    return f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


def get_traceparent(request: Request) -> str:
    """Read the validated trace context attached by HTTP middleware."""
    return normalize_traceparent(getattr(request.state, "traceparent", None))
