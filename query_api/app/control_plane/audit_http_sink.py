"""Generic HTTPS adapter for the durable audit delivery port."""

from __future__ import annotations

import json
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .audit_delivery import (
    AuditDeliveryAck,
    AuditDeliveryBatch,
    AuditSinkError,
)


class HTTPAuditSink:
    """POST chain pages to any HTTPS service implementing the ack contract."""

    def __init__(
        self,
        endpoint: str,
        *,
        authorization: Optional[str] = None,
        timeout_seconds: float = 5.0,
        allow_insecure_http: bool = False,
        opener: Callable = urlopen,
    ):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ({"http", "https"} if allow_insecure_http else {"https"}):
            raise ValueError("audit sink endpoint must use HTTPS")
        if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("audit sink endpoint is invalid or contains credentials")
        if timeout_seconds < 0.1 or timeout_seconds > 60:
            raise ValueError("audit sink timeout must be between 0.1 and 60 seconds")
        self.endpoint = endpoint
        self.authorization = authorization
        self.timeout_seconds = timeout_seconds
        self.opener = opener

    def deliver(self, batch: AuditDeliveryBatch) -> AuditDeliveryAck:
        events = []
        for record in batch.records:
            event = dict(record)
            event["timestamp"] = record["timestamp"].isoformat()
            events.append(event)
        payload = json.dumps(
            {
                "schema": "imposbro.audit.delivery.v1",
                "destination_id": batch.destination_id,
                "after_sequence": batch.after_sequence,
                "previous_hash": batch.previous_hash,
                "head_sequence": batch.head_sequence,
                "head_hash": batch.head_hash,
                "events": events,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.authorization:
            headers["Authorization"] = self.authorization
        request = Request(self.endpoint, data=payload, headers=headers, method="POST")
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                if not 200 <= int(response.status) < 300:
                    raise AuditSinkError("sink_http_error")
                body = json.loads(response.read(65537).decode("utf-8"))
        except HTTPError as exc:
            code = "sink_http_5xx" if int(exc.code) >= 500 else "sink_http_4xx"
            raise AuditSinkError(code) from None
        except (URLError, TimeoutError, OSError):
            raise AuditSinkError("sink_transport_error") from None
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, ValueError):
            raise AuditSinkError("sink_invalid_ack") from None
        try:
            return AuditDeliveryAck(
                destination_id=str(body["destination_id"]),
                last_sequence=int(body["last_sequence"]),
                last_event_hash=str(body["last_event_hash"]),
            )
        except (KeyError, TypeError, ValueError):
            raise AuditSinkError("sink_invalid_ack") from None
