"""Structured, context-aware logging with conservative secret redaction."""

from __future__ import annotations

import contextvars
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional, Tuple


_request_id = contextvars.ContextVar("log_request_id", default="")
_traceparent = contextvars.ContextVar("log_traceparent", default="")
_actor = contextvars.ContextVar("log_actor", default="")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+"),
    re.compile(r"(?i)(://[^\s:/@]+:)[^\s@/]+(@)"),
    re.compile(
        r"(?i)((?:api[_-]?key|password|secret|access[_-]?token|refresh[_-]?token)"
        r"\s*[=:]\s*[\"']?)[^\s,\"'}]+"
    ),
)


def redact_text(value: object) -> str:
    """Redact common credential forms from an untrusted log string."""
    text = str(value)
    text = _SECRET_PATTERNS[0].sub("Bearer [REDACTED]", text)
    text = _SECRET_PATTERNS[1].sub(r"\1[REDACTED]\2", text)
    text = _SECRET_PATTERNS[2].sub(r"\1[REDACTED]", text)
    return text


class RedactingJsonFormatter(logging.Formatter):
    """Emit one JSON object per record with bounded diagnostic context."""

    def __init__(self, *, service: str, version: str):
        super().__init__()
        self.service = service
        self.version = version

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "version": self.version,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        request_id = _request_id.get()
        traceparent = _traceparent.get()
        actor = _actor.get()
        if request_id:
            payload["request_id"] = request_id
        if traceparent:
            payload["traceparent"] = traceparent
        if actor:
            payload["actor"] = actor
        if record.exc_info:
            payload["exception"] = redact_text(
                self.formatException(record.exc_info)
            )[:16_384]
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def configure_logging(*, service: str, version: str, level: str, json_logs: bool) -> None:
    """Replace root handlers deterministically during process startup."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler()
    if json_logs:
        handler.setFormatter(RedactingJsonFormatter(service=service, version=version))
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
        )
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def bind_log_context(
    *,
    request_id: str,
    traceparent: str,
    actor: str = "",
) -> Tuple[contextvars.Token, contextvars.Token, contextvars.Token]:
    return (
        _request_id.set(request_id),
        _traceparent.set(traceparent),
        _actor.set(actor),
    )


def reset_log_context(
    tokens: Tuple[contextvars.Token, contextvars.Token, contextvars.Token]
) -> None:
    _request_id.reset(tokens[0])
    _traceparent.reset(tokens[1])
    _actor.reset(tokens[2])
