"""Request correlation helpers for API and async indexing flows."""

import re
import uuid
from typing import Optional

from starlette.requests import Request


REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


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
