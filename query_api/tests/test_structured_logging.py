"""Structured logging contract and credential redaction tests."""

import json
import logging

from structured_logging import (
    RedactingJsonFormatter,
    bind_log_context,
    redact_text,
    reset_log_context,
)


def test_redaction_covers_tokens_urls_passwords_and_api_keys():
    value = (
        "Authorization=Bearer abc.def.ghi "
        "database=postgresql://user:super-secret@db/app "
        "password=hunter2 api_key='key-123' access_token=token-123"
    )

    redacted = redact_text(value)

    for secret in ("abc.def.ghi", "super-secret", "hunter2", "key-123", "token-123"):
        assert secret not in redacted
    assert redacted.count("[REDACTED]") == 5


def test_redaction_neutralizes_log_forging_line_breaks():
    redacted = redact_text("safe\r\nlevel=ERROR forged\nnext\rlast")

    assert redacted == r"safe\nlevel=ERROR forged\nnext\rlast"
    assert "\n" not in redacted
    assert "\r" not in redacted


def test_json_formatter_emits_build_and_request_context_without_secret():
    formatter = RedactingJsonFormatter(service="query-api", version="4.0.0")
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="request failed password=%s",
        args=("secret-value",),
        exc_info=None,
    )
    tokens = bind_log_context(
        request_id="request-1",
        traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        actor="oidc:user-1",
    )
    try:
        payload = json.loads(formatter.format(record))
    finally:
        reset_log_context(tokens)

    assert payload["service"] == "query-api"
    assert payload["version"] == "4.0.0"
    assert payload["request_id"] == "request-1"
    assert payload["actor"] == "oidc:user-1"
    assert "secret-value" not in payload["message"]
