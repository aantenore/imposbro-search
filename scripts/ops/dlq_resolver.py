#!/usr/bin/env python3
"""Single-record, fail-closed resolver for IMPOSBRO indexing DLQ records.

The CLI never subscribes, scans, resets offsets, or commits by default. It uses
an explicitly assigned topic/partition/offset and the fixed operational group
``imposbro_indexing_dlq_resolver``. Kafka credentials are read only from the
environment and are never included in logs or evidence.

Runtime dependency: kafka-python-ng (import package ``kafka``).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence


RESOLVER_GROUP_ID = "imposbro_indexing_dlq_resolver"
EVIDENCE_SCHEMA = "imposbro.dlq-resolution-evidence.v1"
RECOVERY_PROTOCOL = "dlq-replay-v1"
SUPPORTED_ACTIONS = {"inspect", "dry-run", "replay", "disposition"}
SECURITY_PROTOCOLS = {"PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"}
SASL_MECHANISMS = {"PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"}
TOPIC_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,249}$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
COLLECTION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
DOCUMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,256}$")
BROKER_PATTERN = re.compile(r"^[A-Za-z0-9._:\[\]-]{1,253}$")
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
MAX_ERROR_MESSAGE_BYTES = 16_384


class ResolverError(RuntimeError):
    """Expected, sanitized resolver failure safe for stderr and evidence."""

    def __init__(
        self,
        code: str,
        public_message: str,
        *,
        partial_evidence: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.partial_evidence = partial_evidence


class _DuplicateJsonKey(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ResolverError(
            "invalid_json_value",
            "DLQ message contains a value that cannot be represented as strict JSON",
        ) from exc


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJsonKey(key)
            result[key] = value
        return result

    try:
        decoded = raw.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey, ValueError) as exc:
        raise ResolverError(
            "malformed_dlq_json",
            "Selected DLQ record is not a strict UTF-8 JSON object",
        ) from exc
    if not isinstance(value, dict):
        raise ResolverError(
            "malformed_dlq_wrapper",
            "Selected DLQ record must contain a JSON object wrapper",
        )
    return value


def _strict_bool_env(
    environ: Mapping[str, str], name: str, *, default: Optional[bool] = None
) -> bool:
    raw = environ.get(name, "").strip().lower()
    if not raw and default is not None:
        return default
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    raise ResolverError("invalid_transport_config", f"{name} must be a boolean")


def _bounded_int_env(
    environ: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ResolverError(
            "invalid_transport_config", f"{name} must be an integer"
        ) from exc
    if value < minimum or value > maximum:
        raise ResolverError(
            "invalid_transport_config",
            f"{name} must be between {minimum} and {maximum}",
        )
    return value


def _required_env(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ResolverError("missing_transport_config", f"{name} must be set")
    return value


def _validate_readable_file(name: str, value: str) -> str:
    if not value or not os.path.isfile(value) or not os.access(value, os.R_OK):
        raise ResolverError(
            "invalid_transport_config", f"{name} must reference a readable file"
        )
    return value


@dataclass(frozen=True)
class ResolverConfig:
    bootstrap_servers: tuple[str, ...]
    security_protocol: str
    security_kwargs: Mapping[str, Any]
    client_id: str = "imposbro-dlq-resolver"
    timeout_ms: int = 30_000
    max_record_bytes: int = 1_048_576

    @property
    def timeout_seconds(self) -> float:
        return self.timeout_ms / 1000.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "ResolverConfig":
        bootstrap_raw = _required_env(environ, "KAFKA_BOOTSTRAP_SERVERS")
        brokers = tuple(part.strip() for part in bootstrap_raw.split(","))
        if not brokers or any(
            not broker
            or not BROKER_PATTERN.fullmatch(broker)
            or "://" in broker
            or "@" in broker
            for broker in brokers
        ):
            raise ResolverError(
                "invalid_transport_config",
                "KAFKA_BOOTSTRAP_SERVERS must contain comma-separated "
                "host:port endpoints without credentials",
            )

        protocol = _required_env(environ, "KAFKA_SECURITY_PROTOCOL").upper()
        if protocol not in SECURITY_PROTOCOLS:
            raise ResolverError(
                "invalid_transport_config",
                "KAFKA_SECURITY_PROTOCOL must be PLAINTEXT, SSL, SASL_PLAINTEXT, or SASL_SSL",
            )
        if protocol in {"PLAINTEXT", "SASL_PLAINTEXT"} and not _strict_bool_env(
            environ, "DLQ_RESOLVER_ALLOW_INSECURE_KAFKA", default=False
        ):
            raise ResolverError(
                "insecure_transport_not_approved",
                "Plaintext Kafka requires DLQ_RESOLVER_ALLOW_INSECURE_KAFKA=true",
            )

        uses_sasl = protocol in {"SASL_PLAINTEXT", "SASL_SSL"}
        uses_tls = protocol in {"SSL", "SASL_SSL"}
        mechanism = environ.get("KAFKA_SASL_MECHANISM", "").strip().upper()
        username = environ.get("KAFKA_SASL_USERNAME", "")
        password = environ.get("KAFKA_SASL_PASSWORD", "")
        cafile = environ.get("KAFKA_SSL_CAFILE", "").strip()
        certfile = environ.get("KAFKA_SSL_CERTFILE", "").strip()
        keyfile = environ.get("KAFKA_SSL_KEYFILE", "").strip()
        security: dict[str, Any] = {"security_protocol": protocol}

        if uses_sasl:
            if mechanism not in SASL_MECHANISMS:
                raise ResolverError(
                    "invalid_transport_config",
                    "KAFKA_SASL_MECHANISM must be PLAIN, SCRAM-SHA-256, or SCRAM-SHA-512",
                )
            if not username or not password:
                raise ResolverError(
                    "missing_transport_config",
                    "KAFKA_SASL_USERNAME and KAFKA_SASL_PASSWORD are required for SASL",
                )
            security.update(
                sasl_mechanism=mechanism,
                sasl_plain_username=username,
                sasl_plain_password=password,
            )
        elif mechanism or username or password:
            raise ResolverError(
                "invalid_transport_config",
                "SASL settings require a SASL security protocol",
            )

        if uses_tls:
            _validate_readable_file("KAFKA_SSL_CAFILE", cafile)
            if bool(certfile) != bool(keyfile):
                raise ResolverError(
                    "invalid_transport_config",
                    "KAFKA_SSL_CERTFILE and KAFKA_SSL_KEYFILE must be configured together",
                )
            if not _strict_bool_env(
                environ, "KAFKA_SSL_CHECK_HOSTNAME", default=True
            ):
                raise ResolverError(
                    "invalid_transport_config",
                    "KAFKA_SSL_CHECK_HOSTNAME cannot be disabled for DLQ resolution",
                )
            security.update(ssl_cafile=cafile, ssl_check_hostname=True)
            if certfile:
                security.update(
                    ssl_certfile=_validate_readable_file(
                        "KAFKA_SSL_CERTFILE", certfile
                    ),
                    ssl_keyfile=_validate_readable_file("KAFKA_SSL_KEYFILE", keyfile),
                )
        elif cafile or certfile or keyfile:
            raise ResolverError(
                "invalid_transport_config",
                "Kafka TLS files require SSL or SASL_SSL",
            )

        client_id = environ.get("DLQ_RESOLVER_CLIENT_ID", "imposbro-dlq-resolver").strip()
        if not IDENTIFIER_PATTERN.fullmatch(client_id):
            raise ResolverError(
                "invalid_transport_config",
                "DLQ_RESOLVER_CLIENT_ID contains unsupported characters",
            )
        timeout_ms = _bounded_int_env(
            environ,
            "DLQ_RESOLVER_TIMEOUT_MS",
            default=30_000,
            minimum=11_000,
            maximum=120_000,
        )
        max_record_bytes = _bounded_int_env(
            environ,
            "DLQ_RESOLVER_MAX_RECORD_BYTES",
            default=1_048_576,
            minimum=1_024,
            maximum=10_485_760,
        )
        return cls(
            bootstrap_servers=brokers,
            security_protocol=protocol,
            security_kwargs=security,
            client_id=client_id,
            timeout_ms=timeout_ms,
            max_record_bytes=max_record_bytes,
        )


@dataclass(frozen=True)
class KafkaBindings:
    Consumer: Any
    Producer: Any
    TopicPartition: Any
    OffsetAndMetadata: Any

    @classmethod
    def load(cls) -> "KafkaBindings":
        try:
            from kafka import KafkaConsumer, KafkaProducer
            from kafka.structs import OffsetAndMetadata, TopicPartition
        except ImportError as exc:
            raise ResolverError(
                "missing_dependency",
                "kafka-python-ng is required; install the repository's pinned Kafka dependency",
            ) from exc
        return cls(
            Consumer=KafkaConsumer,
            Producer=KafkaProducer,
            TopicPartition=TopicPartition,
            OffsetAndMetadata=OffsetAndMetadata,
        )


@dataclass(frozen=True)
class Selection:
    dlq_topic: str
    partition: int
    offset: int
    expected_source_topic: str

    def validate(self) -> None:
        for name, value in (
            ("--dlq-topic", self.dlq_topic),
            ("--expected-source-topic", self.expected_source_topic),
        ):
            if not TOPIC_PATTERN.fullmatch(value):
                raise ResolverError(
                    "invalid_selector", f"{name} is not a valid Kafka topic"
                )
        if self.dlq_topic == self.expected_source_topic:
            raise ResolverError(
                "invalid_selector", "DLQ and source topics must be different"
            )
        if self.partition < 0 or self.offset < 0:
            raise ResolverError(
                "invalid_selector", "partition and offset must be non-negative"
            )

    @property
    def token(self) -> str:
        return f"{self.dlq_topic}:{self.partition}:{self.offset}"


@dataclass(frozen=True)
class ParsedDlqEnvelope:
    source_topic: str
    source_partition: int
    source_offset: int
    error_class: str
    error_message_sha256: str
    message: Any
    message_sha256: str
    message_key: Optional[bytes]
    message_key_sha256: Optional[str]
    wrapper_sha256: str
    wrapper_size_bytes: int


@dataclass(frozen=True)
class ReplayMetadata:
    event_id_sha256: str
    identity_sha256: str
    envelope_version: int
    operation: str
    sequence: int
    target_count: int


@dataclass(frozen=True)
class FetchResult:
    consumer: Any
    topic_partition: Any
    record: Any
    beginning_offset: int
    end_offset: int
    committed_offset: Optional[int]

    @property
    def expected_next_offset(self) -> int:
        return (
            self.beginning_offset
            if self.committed_offset is None
            else self.committed_offset
        )

    @property
    def resolution_order_eligible(self) -> bool:
        if self.committed_offset is not None and self.committed_offset < self.beginning_offset:
            return False
        return self.record.offset == self.expected_next_offset


def parse_dlq_envelope(
    raw_value: Any,
    *,
    expected_source_topic: str,
    max_record_bytes: int,
) -> ParsedDlqEnvelope:
    if not isinstance(raw_value, bytes):
        raise ResolverError(
            "malformed_dlq_value", "Selected DLQ record value must be raw bytes"
        )
    if not raw_value or len(raw_value) > max_record_bytes:
        raise ResolverError(
            "invalid_record_size", "Selected DLQ record size is outside the approved bound"
        )
    wrapper = _strict_json_object(raw_value)
    required = {
        "source_topic",
        "source_partition",
        "source_offset",
        "error",
        "error_message",
        "message",
    }
    allowed = required | {"message_key", "message_key_encoding"}
    missing = sorted(required - set(wrapper))
    unknown = sorted(set(wrapper) - allowed)
    if missing or unknown:
        raise ResolverError(
            "malformed_dlq_wrapper",
            "DLQ wrapper fields do not match the supported contract",
        )

    source_topic = wrapper["source_topic"]
    if not isinstance(source_topic, str) or not TOPIC_PATTERN.fullmatch(source_topic):
        raise ResolverError(
            "malformed_dlq_wrapper", "DLQ source_topic is invalid"
        )
    if source_topic != expected_source_topic:
        raise ResolverError(
            "source_topic_mismatch",
            "DLQ source_topic does not match --expected-source-topic",
        )

    def strict_nonnegative(value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ResolverError(
                "malformed_dlq_wrapper", f"DLQ {field} must be a non-negative integer"
            )
        return value

    source_partition = strict_nonnegative(wrapper["source_partition"], "source_partition")
    source_offset = strict_nonnegative(wrapper["source_offset"], "source_offset")
    error_class = wrapper["error"]
    error_message = wrapper["error_message"]
    if not isinstance(error_class, str) or not IDENTIFIER_PATTERN.fullmatch(error_class):
        raise ResolverError(
            "malformed_dlq_wrapper", "DLQ error class is invalid"
        )
    if (
        not isinstance(error_message, str)
        or len(error_message.encode("utf-8")) > MAX_ERROR_MESSAGE_BYTES
    ):
        raise ResolverError(
            "malformed_dlq_wrapper", "DLQ error message is invalid or too large"
        )

    message_key_value = wrapper.get("message_key")
    message_key_encoding = wrapper.get("message_key_encoding")
    message_key: Optional[bytes] = None
    if message_key_value is None:
        if message_key_encoding is not None:
            raise ResolverError(
                "malformed_dlq_wrapper",
                "message_key_encoding is not allowed without message_key",
            )
    else:
        if not isinstance(message_key_value, str) or message_key_encoding != "base64":
            raise ResolverError(
                "malformed_dlq_wrapper",
                "DLQ message_key must use explicit base64 encoding",
            )
        try:
            message_key = base64.b64decode(message_key_value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ResolverError(
                "malformed_dlq_wrapper", "DLQ message_key is not valid base64"
            ) from exc
        if not message_key or len(message_key) > 2_048:
            raise ResolverError(
                "malformed_dlq_wrapper", "DLQ message_key size is invalid"
            )

    message_bytes = _canonical_json(wrapper["message"])
    return ParsedDlqEnvelope(
        source_topic=source_topic,
        source_partition=source_partition,
        source_offset=source_offset,
        error_class=error_class,
        error_message_sha256=_sha256(error_message.encode("utf-8")),
        message=wrapper["message"],
        message_sha256=_sha256(message_bytes),
        message_key=message_key,
        message_key_sha256=_sha256(message_key) if message_key else None,
        wrapper_sha256=_sha256(raw_value),
        wrapper_size_bytes=len(raw_value),
    )


def validate_replay_payload(
    envelope: ParsedDlqEnvelope,
    *,
    dlq_topic: Optional[str] = None,
) -> ReplayMetadata:
    payload = envelope.message
    if not isinstance(payload, dict):
        raise ResolverError(
            "replay_payload_invalid",
            "Only a validated indexing envelope v2 object can be replayed",
        )
    if payload.get("envelope_version") != 2:
        raise ResolverError(
            "replay_payload_invalid", "Replay requires envelope_version=2"
        )
    event_id = payload.get("event_id")
    identity = payload.get("identity")
    operation = payload.get("operation")
    sequence = payload.get("sequence")
    document_version = payload.get("document_version")
    routing_revision = payload.get("routing_revision")
    targets = payload.get("target_clusters")
    if not isinstance(event_id, str) or not EVENT_ID_PATTERN.fullmatch(event_id):
        raise ResolverError("replay_payload_invalid", "Replay event_id is invalid")
    if not isinstance(identity, dict) or set(identity) != {
        "tenant_id",
        "collection",
        "document_id",
    }:
        raise ResolverError("replay_payload_invalid", "Replay identity is invalid")
    tenant_id = identity.get("tenant_id")
    collection = identity.get("collection")
    document_id = identity.get("document_id")
    if (
        not isinstance(tenant_id, str)
        or not tenant_id
        or len(tenant_id) > 256
        or tenant_id.strip() != tenant_id
        or any(not character.isprintable() for character in tenant_id)
        or "\x1f" in tenant_id
        or not isinstance(collection, str)
        or not COLLECTION_PATTERN.fullmatch(collection)
        or not isinstance(document_id, str)
        or not DOCUMENT_ID_PATTERN.fullmatch(document_id)
    ):
        raise ResolverError("replay_payload_invalid", "Replay identity values are invalid")
    for name, value in (
        ("sequence", sequence),
        ("document_version", document_version),
        ("routing_revision", routing_revision),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ResolverError(
                "replay_payload_invalid", f"Replay {name} must be a positive integer"
            )
    if operation not in {"upsert", "delete", "tombstone"}:
        raise ResolverError("replay_payload_invalid", "Replay operation is invalid")
    if (
        not isinstance(targets, list)
        or not targets
        or any(
            not isinstance(target, str) or not COLLECTION_PATTERN.fullmatch(target)
            for target in targets
        )
        or len(set(targets)) != len(targets)
    ):
        raise ResolverError(
            "replay_payload_invalid", "Replay target_clusters are invalid"
        )
    document = payload.get("document")
    if operation == "upsert":
        if not isinstance(document, dict) or document.get("id") != document_id:
            raise ResolverError(
                "replay_payload_invalid", "Replay upsert document identity is invalid"
            )
        if payload.get("delete_filter") is not None:
            raise ResolverError(
                "replay_payload_invalid", "Replay upsert cannot contain delete_filter"
            )
    elif document is not None:
        raise ResolverError(
            "replay_payload_invalid", "Replay delete/tombstone cannot contain document"
        )

    logical_key = f"{tenant_id}\x1f{collection}\x1f{document_id}".encode("utf-8")
    if envelope.message_key != logical_key:
        raise ResolverError(
            "replay_key_mismatch",
            "DLQ message_key does not match the envelope v2 logical identity",
        )
    if not envelope.source_topic.endswith(f"_{collection}"):
        raise ResolverError(
            "replay_source_topic_mismatch",
            "DLQ source_topic does not end with the envelope collection",
        )
    source_prefix = envelope.source_topic[: -(len(collection) + 1)]
    if dlq_topic is not None and dlq_topic != f"{source_prefix}_dlq":
        raise ResolverError(
            "replay_dlq_topic_mismatch",
            "Selected DLQ topic does not match the verified source-topic prefix",
        )
    return ReplayMetadata(
        event_id_sha256=_sha256(event_id.encode("utf-8")),
        identity_sha256=_sha256(logical_key),
        envelope_version=2,
        operation=operation,
        sequence=sequence,
        target_count=len(targets),
    )


class DlqResolver:
    def __init__(
        self,
        config: ResolverConfig,
        bindings: KafkaBindings,
        *,
        now: Callable[[], str] = _utc_now,
    ) -> None:
        self.config = config
        self.bindings = bindings
        self.now = now

    def _consumer(self) -> Any:
        return self.bindings.Consumer(
            bootstrap_servers=list(self.config.bootstrap_servers),
            client_id=self.config.client_id,
            group_id=RESOLVER_GROUP_ID,
            enable_auto_commit=False,
            auto_offset_reset="none",
            max_poll_records=1,
            consumer_timeout_ms=self.config.timeout_ms,
            request_timeout_ms=self.config.timeout_ms,
            api_version_auto_timeout_ms=self.config.timeout_ms,
            **dict(self.config.security_kwargs),
        )

    def _producer(self) -> Any:
        return self.bindings.Producer(
            bootstrap_servers=list(self.config.bootstrap_servers),
            client_id=f"{self.config.client_id}-replay",
            acks="all",
            retries=5,
            max_in_flight_requests_per_connection=1,
            value_serializer=lambda value: _canonical_json(value),
            **dict(self.config.security_kwargs),
        )

    def _fetch_exact(self, selection: Selection) -> FetchResult:
        consumer = None
        try:
            consumer = self._consumer()
            partitions = consumer.partitions_for_topic(selection.dlq_topic)
            if partitions is None or selection.partition not in partitions:
                raise ResolverError(
                    "partition_not_found", "Selected DLQ partition does not exist"
                )
            topic_partition = self.bindings.TopicPartition(
                selection.dlq_topic, selection.partition
            )
            beginning = int(consumer.beginning_offsets([topic_partition])[topic_partition])
            end = int(consumer.end_offsets([topic_partition])[topic_partition])
            if selection.offset < beginning:
                raise ResolverError(
                    "offset_expired",
                    "Selected DLQ offset is below the retained partition beginning",
                )
            if selection.offset >= end:
                raise ResolverError(
                    "offset_not_available",
                    "Selected DLQ offset is not present below the partition end",
                )
            committed_raw = consumer.committed(topic_partition)
            if committed_raw is None:
                committed = None
            else:
                committed = int(getattr(committed_raw, "offset", committed_raw))
            consumer.assign([topic_partition])
            consumer.seek(topic_partition, selection.offset)
            polled = consumer.poll(
                timeout_ms=self.config.timeout_ms,
                max_records=1,
            )
            records = [record for batch in polled.values() for record in batch]
            if len(records) != 1:
                raise ResolverError(
                    "exact_record_not_returned",
                    "Kafka did not return exactly one record for the explicit selector",
                )
            record = records[0]
            if (
                record.topic != selection.dlq_topic
                or int(record.partition) != selection.partition
                or int(record.offset) != selection.offset
            ):
                raise ResolverError(
                    "exact_record_mismatch",
                    "Kafka returned a record different from the explicit selector",
                )
            return FetchResult(
                consumer=consumer,
                topic_partition=topic_partition,
                record=record,
                beginning_offset=beginning,
                end_offset=end,
                committed_offset=committed,
            )
        except ResolverError:
            if consumer is not None:
                self._safe_close(consumer)
            raise
        except Exception as exc:
            if consumer is not None:
                self._safe_close(consumer)
            raise ResolverError(
                "kafka_fetch_failed",
                "Kafka failed while fetching the explicit DLQ record; "
                "inspect restricted platform logs",
            ) from exc

    @staticmethod
    def _safe_close(client: Any) -> None:
        try:
            client.close()
        except Exception:
            pass

    @staticmethod
    def _require_resolution_order(fetch: FetchResult) -> None:
        if (
            fetch.committed_offset is not None
            and fetch.committed_offset < fetch.beginning_offset
        ):
            raise ResolverError(
                "committed_offset_expired",
                "Resolver-group commit is behind retained data; do not skip "
                "expired evidence with this tool",
            )
        if fetch.record.offset != fetch.expected_next_offset:
            raise ResolverError(
                "partition_order_violation",
                "Selected offset is not the next unresolved offset for this partition",
            )

    def _commit_exact(
        self,
        fetch: FetchResult,
        *,
        operation_id: str,
    ) -> int:
        next_offset = int(fetch.record.offset) + 1
        metadata = self.bindings.OffsetAndMetadata(
            next_offset,
            f"imposbro-dlq-resolver:{operation_id}",
        )
        try:
            current_raw = fetch.consumer.committed(fetch.topic_partition)
            current = (
                None
                if current_raw is None
                else int(getattr(current_raw, "offset", current_raw))
            )
            if current != int(fetch.record.offset):
                raise ResolverError(
                    "concurrent_offset_change",
                    "Resolver-group offset changed after fetch; refusing a stale commit",
                )
            fetch.consumer.commit({fetch.topic_partition: metadata})
            committed_raw = fetch.consumer.committed(fetch.topic_partition)
            committed = int(getattr(committed_raw, "offset", committed_raw))
        except ResolverError:
            raise
        except Exception as exc:
            raise ResolverError(
                "offset_commit_failed",
                "Kafka did not confirm the exact resolver-group offset commit",
            ) from exc
        if committed != next_offset:
            raise ResolverError(
                "offset_commit_mismatch",
                "Kafka committed offset does not equal selected offset plus one",
            )
        return committed

    @staticmethod
    def _recovery_headers(
        selection: Selection,
        envelope: ParsedDlqEnvelope,
        operation_id: str,
    ) -> list[tuple[str, bytes]]:
        return [
            ("imposbro-recovery-protocol", RECOVERY_PROTOCOL.encode("ascii")),
            ("imposbro-recovery-id", operation_id.encode("ascii")),
            ("imposbro-dlq-topic", selection.dlq_topic.encode("utf-8")),
            ("imposbro-dlq-partition", str(selection.partition).encode("ascii")),
            ("imposbro-dlq-offset", str(selection.offset).encode("ascii")),
            ("imposbro-source-partition", str(envelope.source_partition).encode("ascii")),
            ("imposbro-source-offset", str(envelope.source_offset).encode("ascii")),
        ]

    def resolve(
        self,
        *,
        action: str,
        selection: Selection,
        operation_id: str,
        commit: bool = False,
        confirm: str = "",
        reason: str = "",
        approver: str = "",
    ) -> dict[str, Any]:
        if action not in SUPPORTED_ACTIONS:
            raise ResolverError("invalid_action", "Unsupported resolver action")
        selection.validate()
        if not IDENTIFIER_PATTERN.fullmatch(operation_id):
            raise ResolverError("invalid_operation_id", "Operation ID is invalid")

        fetch: Optional[FetchResult] = None
        producer: Any = None
        evidence: Optional[dict[str, Any]] = None
        try:
            fetch = self._fetch_exact(selection)
            envelope = parse_dlq_envelope(
                fetch.record.value,
                expected_source_topic=selection.expected_source_topic,
                max_record_bytes=self.config.max_record_bytes,
            )
            try:
                replay_metadata = validate_replay_payload(
                    envelope,
                    dlq_topic=selection.dlq_topic,
                )
                replay_eligible = True
                replay_validation_code = "valid_v2_identity_and_key"
            except ResolverError as replay_error:
                replay_metadata = None
                replay_eligible = False
                replay_validation_code = replay_error.code

            record_key = getattr(fetch.record, "key", None)
            record_key_sha256 = (
                _sha256(record_key) if isinstance(record_key, bytes) else None
            )
            evidence = {
                "schema": EVIDENCE_SCHEMA,
                "operation_id": operation_id,
                "action": action,
                "outcome": "started",
                "started_at": self.now(),
                "consumer_group": RESOLVER_GROUP_ID,
                "selector": {
                    "dlq_topic": selection.dlq_topic,
                    "partition": selection.partition,
                    "offset": selection.offset,
                    "expected_source_topic": selection.expected_source_topic,
                },
                "transport": {"security_protocol": self.config.security_protocol},
                "offset_state": {
                    "beginning": fetch.beginning_offset,
                    "end": fetch.end_offset,
                    "committed": fetch.committed_offset,
                    "expected_next": fetch.expected_next_offset,
                    "resolution_order_eligible": fetch.resolution_order_eligible,
                },
                "record": {
                    "wrapper_sha256": envelope.wrapper_sha256,
                    "wrapper_size_bytes": envelope.wrapper_size_bytes,
                    "dlq_record_key_sha256": record_key_sha256,
                    "source_topic": envelope.source_topic,
                    "source_partition": envelope.source_partition,
                    "source_offset": envelope.source_offset,
                    "error_class": envelope.error_class,
                    "error_message_sha256": envelope.error_message_sha256,
                    "message_sha256": envelope.message_sha256,
                    "message_key_sha256": envelope.message_key_sha256,
                },
                "replay_validation": {
                    "eligible": replay_eligible,
                    "code": replay_validation_code,
                },
                "redaction": {
                    "payload_included": False,
                    "message_key_included": False,
                    "error_message_included": False,
                    "credentials_included": False,
                },
                "commit": {
                    "requested": commit,
                    "performed": False,
                    "offset": None,
                },
            }
            if replay_metadata is not None:
                evidence["event"] = {
                    "event_id_sha256": replay_metadata.event_id_sha256,
                    "identity_sha256": replay_metadata.identity_sha256,
                    "envelope_version": replay_metadata.envelope_version,
                    "operation": replay_metadata.operation,
                    "sequence": replay_metadata.sequence,
                    "target_count": replay_metadata.target_count,
                }

            if action == "inspect":
                if commit:
                    raise ResolverError(
                        "commit_not_allowed", "inspect never accepts a commit request"
                    )
                evidence["outcome"] = "inspected"

            elif action == "dry-run":
                if commit:
                    raise ResolverError(
                        "commit_not_allowed", "dry-run never accepts a commit request"
                    )
                self._require_resolution_order(fetch)
                if replay_metadata is None:
                    raise ResolverError(
                        replay_validation_code,
                        "Selected DLQ record is not eligible for replay",
                    )
                evidence["outcome"] = "dry_run"
                evidence["planned_replay"] = {
                    "source_topic": envelope.source_topic,
                    "preserve_payload": True,
                    "preserve_message_key": True,
                    "recovery_header_names": [
                        name
                        for name, _value in self._recovery_headers(
                            selection, envelope, operation_id
                        )
                    ],
                }

            elif action == "replay":
                self._require_resolution_order(fetch)
                if replay_metadata is None:
                    raise ResolverError(
                        replay_validation_code,
                        "Selected DLQ record is not eligible for replay",
                    )
                confirmation_prefix = "REPLAY-AND-COMMIT" if commit else "REPLAY"
                expected_confirmation = f"{confirmation_prefix}:{selection.token}"
                if confirm != expected_confirmation:
                    raise ResolverError(
                        "confirmation_mismatch",
                        "Replay confirmation must equal "
                        f"{confirmation_prefix}:<dlq-topic>:<partition>:<offset>",
                    )
                original_message_sha256 = _sha256(_canonical_json(envelope.message))
                headers = self._recovery_headers(selection, envelope, operation_id)
                try:
                    producer = self._producer()
                    future = producer.send(
                        envelope.source_topic,
                        key=envelope.message_key,
                        value=envelope.message,
                        headers=headers,
                    )
                    record_metadata = future.get(timeout=self.config.timeout_seconds)
                    producer.flush(timeout=self.config.timeout_seconds)
                except Exception as exc:
                    raise ResolverError(
                        "replay_ack_failed",
                        "Kafka did not acknowledge and flush the replay; offset was not committed",
                    ) from exc
                if _sha256(_canonical_json(envelope.message)) != original_message_sha256:
                    raise ResolverError(
                        "payload_mutation_detected",
                        "Replay payload changed in memory; offset was not committed",
                    )
                acknowledged_topic = getattr(record_metadata, "topic", envelope.source_topic)
                if acknowledged_topic != envelope.source_topic:
                    raise ResolverError(
                        "replay_ack_mismatch",
                        "Kafka acknowledged a topic different from the verified source topic",
                    )
                evidence["replay"] = {
                    "acknowledged": True,
                    "flushed": True,
                    "source_topic": envelope.source_topic,
                    "broker_partition": getattr(record_metadata, "partition", None),
                    "broker_offset": getattr(record_metadata, "offset", None),
                    "payload_preserved": True,
                    "message_key_preserved": True,
                    "recovery_header_names": [name for name, _value in headers],
                }
                if commit:
                    evidence["commit"]["attempted_after_ack_and_flush"] = True
                    committed_offset = self._commit_exact(
                        fetch, operation_id=operation_id
                    )
                    evidence["commit"].update(
                        performed=True,
                        offset=committed_offset,
                    )
                    evidence["outcome"] = "replayed_and_committed"
                else:
                    evidence["outcome"] = "replayed_uncommitted"

            else:
                self._require_resolution_order(fetch)
                if not commit:
                    raise ResolverError(
                        "explicit_commit_required",
                        "disposition requires --commit; no offset was changed",
                    )
                if not IDENTIFIER_PATTERN.fullmatch(reason):
                    raise ResolverError(
                        "invalid_disposition",
                        "Disposition reason must be a 1-128 character audit-safe code",
                    )
                if not IDENTIFIER_PATTERN.fullmatch(approver):
                    raise ResolverError(
                        "invalid_disposition",
                        "Disposition approver must be a 1-128 character audit-safe identity",
                    )
                expected_confirmation = (
                    f"DISPOSITION-AND-COMMIT:{selection.token}"
                )
                if confirm != expected_confirmation:
                    raise ResolverError(
                        "confirmation_mismatch",
                        "Disposition confirmation must equal "
                        "DISPOSITION-AND-COMMIT:<dlq-topic>:<partition>:<offset>",
                    )
                evidence["disposition"] = {
                    "reason": reason,
                    "approver": approver,
                    "replayed": False,
                }
                committed_offset = self._commit_exact(fetch, operation_id=operation_id)
                evidence["commit"].update(performed=True, offset=committed_offset)
                evidence["outcome"] = "disposition_committed"

            evidence["completed_at"] = self.now()
            return evidence

        except ResolverError as exc:
            if evidence is not None and exc.partial_evidence is None:
                evidence["outcome"] = "failed"
                evidence["completed_at"] = self.now()
                exc.partial_evidence = evidence
            raise
        except Exception as exc:
            if evidence is not None:
                evidence["outcome"] = "failed"
                evidence["completed_at"] = self.now()
            raise ResolverError(
                "unexpected_resolver_failure",
                "DLQ resolution failed closed; inspect restricted platform logs",
                partial_evidence=evidence,
            ) from exc
        finally:
            if producer is not None:
                self._safe_close(producer)
            if fetch is not None:
                self._safe_close(fetch.consumer)


class EvidenceWriter:
    """Reserve a new evidence path before Kafka side effects, then replace it safely."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._reserved = False

    def reserve(self, initial: Mapping[str, Any]) -> None:
        if self.path.suffix != ".json":
            raise ResolverError(
                "invalid_evidence_path", "--evidence must end in .json"
            )
        if not self.path.parent.is_dir():
            raise ResolverError(
                "invalid_evidence_path", "Evidence parent directory does not exist"
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(self.path, flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(initial, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise ResolverError(
                "evidence_exists", "Evidence path already exists; refusing to overwrite"
            ) from exc
        except OSError as exc:
            raise ResolverError(
                "evidence_reservation_failed", "Could not reserve the evidence path"
            ) from exc
        self._reserved = True

    def finalize(self, evidence: Mapping[str, Any]) -> None:
        if not self._reserved:
            raise ResolverError(
                "evidence_not_reserved", "Evidence path was not reserved before action"
            )
        temp_path: Optional[Path] = None
        try:
            fd, raw_path = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temp_path = Path(raw_path)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(evidence, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
        except (OSError, TypeError, ValueError) as exc:
            raise ResolverError(
                "evidence_write_failed", "Could not finalize redacted JSON evidence"
            ) from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve exactly one IMPOSBRO indexing DLQ record without implicit "
            "scan, reset, replay, or commit behavior."
        )
    )
    selector = argparse.ArgumentParser(add_help=False)
    selector.add_argument("--dlq-topic", required=True)
    selector.add_argument("--partition", required=True, type=_nonnegative_int)
    selector.add_argument("--offset", required=True, type=_nonnegative_int)
    selector.add_argument("--expected-source-topic", required=True)
    selector.add_argument("--evidence", required=True, type=Path)

    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser(
        "inspect",
        parents=[selector],
        help="Validate and record redacted metadata; never replay or commit.",
    )
    subparsers.add_parser(
        "dry-run",
        parents=[selector],
        help="Validate replay and partition-order eligibility; never replay or commit.",
    )
    replay = subparsers.add_parser(
        "replay",
        parents=[selector],
        help="Replay the unchanged inner v2 envelope; commit only with --commit.",
    )
    replay.add_argument("--commit", action="store_true")
    replay.add_argument("--confirm", required=True)

    disposition = subparsers.add_parser(
        "disposition",
        parents=[selector],
        help="Commit without replay after an approved, evidenced disposition.",
    )
    disposition.add_argument("--commit", action="store_true")
    disposition.add_argument("--reason", required=True)
    disposition.add_argument("--approver", required=True)
    disposition.add_argument("--confirm", required=True)
    return parser


def _failure_evidence(
    *,
    args: argparse.Namespace,
    operation_id: str,
    started_at: str,
    error: ResolverError,
) -> dict[str, Any]:
    evidence = error.partial_evidence or {
        "schema": EVIDENCE_SCHEMA,
        "operation_id": operation_id,
        "action": args.action,
        "outcome": "failed",
        "started_at": started_at,
        "consumer_group": RESOLVER_GROUP_ID,
        "selector": {
            "dlq_topic": args.dlq_topic,
            "partition": args.partition,
            "offset": args.offset,
            "expected_source_topic": args.expected_source_topic,
        },
        "redaction": {
            "payload_included": False,
            "message_key_included": False,
            "error_message_included": False,
            "credentials_included": False,
        },
    }
    evidence["outcome"] = "failed"
    evidence["completed_at"] = _utc_now()
    evidence["error"] = {
        "code": error.code,
        "message": error.public_message,
    }
    return evidence


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    bindings: Optional[KafkaBindings] = None,
) -> int:
    args = build_parser().parse_args(argv)
    operation_id = f"dlq-{uuid.uuid4().hex}"
    started_at = _utc_now()
    selection = Selection(
        dlq_topic=args.dlq_topic,
        partition=args.partition,
        offset=args.offset,
        expected_source_topic=args.expected_source_topic,
    )
    try:
        selection.validate()
    except ResolverError as error:
        print(f"ERROR [{error.code}]: {error.public_message}", file=sys.stderr)
        return 2
    writer = EvidenceWriter(args.evidence)
    initial = {
        "schema": EVIDENCE_SCHEMA,
        "operation_id": operation_id,
        "action": args.action,
        "outcome": "started",
        "started_at": started_at,
        "consumer_group": RESOLVER_GROUP_ID,
        "selector": {
            "dlq_topic": args.dlq_topic,
            "partition": args.partition,
            "offset": args.offset,
            "expected_source_topic": args.expected_source_topic,
        },
        "redaction": {
            "payload_included": False,
            "message_key_included": False,
            "error_message_included": False,
            "credentials_included": False,
        },
    }
    try:
        writer.reserve(initial)
        config = ResolverConfig.from_env(os.environ if environ is None else environ)
        active_bindings = KafkaBindings.load() if bindings is None else bindings
        resolver = DlqResolver(config, active_bindings)
        evidence = resolver.resolve(
            action=args.action,
            selection=selection,
            operation_id=operation_id,
            commit=bool(getattr(args, "commit", False)),
            confirm=str(getattr(args, "confirm", "")),
            reason=str(getattr(args, "reason", "")),
            approver=str(getattr(args, "approver", "")),
        )
        writer.finalize(evidence)
    except ResolverError as error:
        if writer._reserved:
            try:
                writer.finalize(
                    _failure_evidence(
                        args=args,
                        operation_id=operation_id,
                        started_at=started_at,
                        error=error,
                    )
                )
            except ResolverError:
                pass
        print(f"ERROR [{error.code}]: {error.public_message}", file=sys.stderr)
        return 2

    print(
        f"DLQ action {args.action} completed; redacted evidence: {args.evidence}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
