"""
Kafka Producer Service for IMPOSBRO Search.

This module provides a managed Kafka producer for publishing document
ingestion messages to the message queue.
"""

import json
import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone
from kafka import KafkaProducer
from typing import Any, Dict, Iterable, Optional

from indexing_events import (
    IndexingEventDraft,
    IndexingEventStore,
    LatestRepairDraft,
    PreparedIndexingEvent,
)
from telemetry import current_trace_carrier, producer_span

logger = logging.getLogger(__name__)
INT64_MAX = (1 << 63) - 1


class KafkaService:
    """
    Manages Kafka producer connections and message publishing.

    The KafkaService provides a reliable interface for publishing documents
    to Kafka topics for asynchronous indexing. It handles connection retries
    and ensures proper serialization.

    Attributes:
        broker_url: Kafka broker connection string
        topic_prefix: Prefix for topic names (e.g., 'imposbro_search_sharded')
        producer: The Kafka producer instance
    """

    def __init__(
        self,
        broker_url: str,
        topic_prefix: str,
        *,
        security_protocol: str = "PLAINTEXT",
        sasl_mechanism: str = "PLAIN",
        sasl_username: str = "",
        sasl_password: str = "",
        ssl_cafile: str = "",
        ssl_certfile: str = "",
        ssl_keyfile: str = "",
        ssl_check_hostname: bool = True,
        client_id: str = "imposbro-query-api",
        event_store: Optional[IndexingEventStore] = None,
    ):
        """
        Initialize the Kafka service.

        Args:
            broker_url: Kafka broker URL (e.g., 'kafka:29092')
            topic_prefix: Prefix for Kafka topic names
        """
        self.broker_url = broker_url
        self.topic_prefix = topic_prefix
        self.security_protocol = security_protocol
        self.sasl_mechanism = sasl_mechanism
        self.sasl_username = sasl_username
        self.sasl_password = sasl_password
        self.ssl_cafile = ssl_cafile
        self.ssl_certfile = ssl_certfile
        self.ssl_keyfile = ssl_keyfile
        self.ssl_check_hostname = ssl_check_hostname
        self.client_id = client_id
        self.event_store = event_store
        self._producer: Optional[KafkaProducer] = None

    @property
    def durable_events(self) -> bool:
        return self.event_store is not None

    def _connection_options(self) -> Dict[str, Any]:
        protocol = self.security_protocol.strip().upper()
        if protocol not in {"PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"}:
            raise ValueError("Unsupported Kafka security protocol")
        options: Dict[str, Any] = {
            "security_protocol": protocol,
            "client_id": self.client_id,
        }
        if protocol.startswith("SASL_"):
            if not self.sasl_username or not self.sasl_password:
                raise ValueError("Kafka SASL username and password are required")
            options.update(
                {
                    "sasl_mechanism": self.sasl_mechanism,
                    "sasl_plain_username": self.sasl_username,
                    "sasl_plain_password": self.sasl_password,
                }
            )
        if protocol in {"SSL", "SASL_SSL"}:
            options["ssl_check_hostname"] = self.ssl_check_hostname
            if self.ssl_cafile:
                options["ssl_cafile"] = self.ssl_cafile
            if bool(self.ssl_certfile) != bool(self.ssl_keyfile):
                raise ValueError("Kafka mTLS requires both certificate and key files")
            if self.ssl_certfile:
                options["ssl_certfile"] = self.ssl_certfile
                options["ssl_keyfile"] = self.ssl_keyfile
        return options

    def operational_evidence(
        self,
        *,
        collection_name: str,
        consumer_group_id: str,
        dlq_group_id: str,
        timeout_ms: int,
    ):
        from services.kafka_operations import KafkaOperationalProbe

        probe = KafkaOperationalProbe(
            broker_url=self.broker_url,
            connection_options=self._connection_options(),
            consumer_group_id=consumer_group_id,
            dlq_group_id=dlq_group_id,
            timeout_ms=timeout_ms,
        )
        return probe.measure(
            topic=f"{self.topic_prefix}_{collection_name}",
            dlq_topic=f"{self.topic_prefix}_dlq",
        )

    @property
    def producer(self) -> KafkaProducer:
        """
        Get the Kafka producer, creating it if necessary.

        Returns:
            Connected KafkaProducer instance
        """
        if self._producer is None:
            self._connect()
        return self._producer

    def _connect(self, max_retries: int = 5, retry_delay: float = 5.0) -> None:
        """
        Establish connection to Kafka broker.

        Args:
            max_retries: Maximum connection attempts
            retry_delay: Seconds to wait between retries
        """
        attempt = 0
        while attempt < max_retries:
            try:
                self._producer = KafkaProducer(
                    bootstrap_servers=self.broker_url,
                    value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                    acks="all",
                    retries=3,
                    max_in_flight_requests_per_connection=1,
                    **self._connection_options(),
                )
                logger.info("Kafka Producer connected successfully.")
                return
            except Exception as e:
                attempt += 1
                logger.warning(
                    f"Failed to connect to Kafka (attempt {attempt}/{max_retries}): {e}"
                )
                if attempt < max_retries:
                    time.sleep(retry_delay)

        raise ConnectionError(
            f"Failed to connect to Kafka after {max_retries} attempts"
        )

    @staticmethod
    def _targets(target_clusters: Iterable[str]) -> list[str]:
        targets = list(dict.fromkeys(str(item).strip() for item in target_clusters))
        if not targets or any(not item for item in targets):
            raise ValueError("At least one non-empty target cluster is required")
        return targets

    @staticmethod
    def _event_id(
        *,
        idempotency_key: str,
        tenant_id: str,
        collection_name: str,
        document_id: str,
        operation: str,
        document_version: int,
        sequence: int,
    ) -> str:
        if not idempotency_key:
            idempotency_key = uuid.uuid4().hex
        material = "\x1f".join(
            (
                idempotency_key,
                tenant_id,
                collection_name,
                document_id,
                operation,
                str(document_version),
                str(sequence),
            )
        )
        return "evt:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _positive(value: int, name: str) -> int:
        normalized = int(value)
        if isinstance(value, bool) or normalized < 1 or normalized > INT64_MAX:
            raise ValueError(f"{name} must be an integer between 1 and INT64_MAX")
        return normalized

    @staticmethod
    def _trace_payload(
        *,
        request_id: Optional[str],
        traceparent: Optional[str],
    ) -> Dict[str, str]:
        """Capture only W3C trace context plus the existing support request id."""
        fallback = {"traceparent": traceparent or ""}
        return {
            "request_id": request_id or "",
            "traceparent": traceparent or "",
            **current_trace_carrier(fallback),
        }

    def _send_with_trace(
        self,
        *,
        topic_name: str,
        key: bytes,
        message: Dict[str, Any],
    ) -> None:
        """Publish under a producer span without mutating the durable payload."""
        parent_trace = dict(message.get("trace") or {})
        with producer_span(parent_trace) as propagated:
            outbound = dict(message)
            outbound["trace"] = {**parent_trace, **propagated}
            self.producer.send(topic_name, key=key, value=outbound)
            self.producer.flush()

    def publish_document(
        self,
        collection_name: str,
        document: Dict[str, Any],
        target_clusters: Iterable[str],
        *,
        tenant_id: str,
        document_version: int,
        sequence: int,
        routing_revision: int,
        idempotency_key: str,
        rollout_id: Optional[str] = None,
        request_id: Optional[str] = None,
        traceparent: Optional[str] = None,
    ) -> str:
        """
        Publish a document for indexing.

        Args:
            collection_name: Target collection name
            document: Document to index
            target_clusters: Every physical target for this logical event
            request_id: Optional support correlation id from the HTTP request

        Raises:
            Exception: If publish fails
        """
        topic_name = f"{self.topic_prefix}_{collection_name}"
        doc_id = str(document.get("id", ""))
        revision = self._positive(routing_revision, "routing_revision")
        targets = self._targets(target_clusters)
        trace_payload = self._trace_payload(
            request_id=request_id,
            traceparent=traceparent,
        )
        if self.event_store is not None:
            prepared = self.event_store.prepare(
                IndexingEventDraft(
                    tenant_id=tenant_id,
                    collection=collection_name,
                    document_id=doc_id,
                    operation="upsert",
                    target_clusters=tuple(targets),
                    routing_revision=revision,
                    rollout_id=rollout_id,
                    idempotency_key=idempotency_key,
                    trace=trace_payload,
                    document=document,
                )
            )
            if prepared.published_at is None:
                self._publish_prepared(prepared)
            return prepared.event_id

        version = self._positive(document_version, "document_version")
        event_sequence = self._positive(sequence, "sequence")
        message = {
            "envelope_version": 2,
            "event_id": self._event_id(
                idempotency_key=idempotency_key,
                tenant_id=tenant_id,
                collection_name=collection_name,
                document_id=doc_id,
                operation="upsert",
                document_version=version,
                sequence=event_sequence,
            ),
            "identity": {
                "tenant_id": tenant_id,
                "collection": collection_name,
                "document_id": doc_id,
            },
            "document_version": version,
            "sequence": event_sequence,
            "operation": "upsert",
            "routing_revision": revision,
            "rollout_id": rollout_id,
            "target_clusters": targets,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "trace": trace_payload,
            "document": document,
        }
        key = "\x1f".join((tenant_id, collection_name, doc_id)).encode("utf-8")
        self._send_with_trace(topic_name=topic_name, key=key, message=message)

        logger.debug(
            "Published document %s to topic %s request_id=%s",
            doc_id,
            topic_name,
            request_id or "-",
        )
        return str(message["event_id"])

    def publish_delete_document(
        self,
        collection_name: str,
        document_id: str,
        target_clusters: Iterable[str],
        *,
        tenant_id: str,
        document_version: int,
        sequence: int,
        routing_revision: int,
        idempotency_key: str,
        rollout_id: Optional[str] = None,
        request_id: Optional[str] = None,
        traceparent: Optional[str] = None,
        filter_by: Optional[str] = None,
    ) -> str:
        """
        Publish a document deletion request.

        Args:
            collection_name: Target collection name
            document_id: Document id to delete
            target_clusters: Every physical target for this logical event
            request_id: Optional support correlation id from the HTTP request
            filter_by: Optional tenant-safe Typesense delete filter

        Raises:
            Exception: If publish fails
        """
        topic_name = f"{self.topic_prefix}_{collection_name}"
        revision = self._positive(routing_revision, "routing_revision")
        targets = self._targets(target_clusters)
        trace_payload = self._trace_payload(
            request_id=request_id,
            traceparent=traceparent,
        )
        if self.event_store is not None:
            prepared = self.event_store.prepare(
                IndexingEventDraft(
                    tenant_id=tenant_id,
                    collection=collection_name,
                    document_id=document_id,
                    operation="delete",
                    target_clusters=tuple(targets),
                    routing_revision=revision,
                    rollout_id=rollout_id,
                    idempotency_key=idempotency_key,
                    trace=trace_payload,
                    delete_filter=filter_by,
                )
            )
            if prepared.published_at is None:
                self._publish_prepared(prepared)
            return prepared.event_id

        version = self._positive(document_version, "document_version")
        event_sequence = self._positive(sequence, "sequence")
        message = {
            "envelope_version": 2,
            "event_id": self._event_id(
                idempotency_key=idempotency_key,
                tenant_id=tenant_id,
                collection_name=collection_name,
                document_id=document_id,
                operation="delete",
                document_version=version,
                sequence=event_sequence,
            ),
            "identity": {
                "tenant_id": tenant_id,
                "collection": collection_name,
                "document_id": document_id,
            },
            "document_version": version,
            "sequence": event_sequence,
            "operation": "delete",
            "routing_revision": revision,
            "rollout_id": rollout_id,
            "target_clusters": targets,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "trace": trace_payload,
        }
        if filter_by:
            message["delete_filter"] = filter_by

        key = "\x1f".join((tenant_id, collection_name, document_id)).encode("utf-8")
        self._send_with_trace(topic_name=topic_name, key=key, message=message)

        logger.debug(
            "Published delete for document %s to topic %s request_id=%s",
            document_id,
            topic_name,
            request_id or "-",
        )
        return str(message["event_id"])

    def _publish_prepared(self, event: PreparedIndexingEvent) -> None:
        """Publish one persisted envelope and close its durable outbox record."""
        identity = event.payload["identity"]
        topic_name = f"{self.topic_prefix}_{identity['collection']}"
        try:
            self._send_with_trace(
                topic_name=topic_name,
                key=event.kafka_key,
                message=event.payload,
            )
            if self.event_store is not None:
                self.event_store.mark_published(event.event_id)
        except Exception as exc:
            if self.event_store is not None:
                self.event_store.record_failure(event.event_id, str(exc))
            raise

    def publish_pending(self, *, limit: int = 100) -> int:
        """Replay unpublished envelopes after producer or process failure."""
        if self.event_store is None:
            return 0
        published = 0
        for event in self.event_store.list_pending(limit=limit):
            self._publish_prepared(event)
            published += 1
        return published

    def high_water_mark(self) -> int:
        if self.event_store is None:
            raise RuntimeError("Durable indexing event storage is not configured")
        return self.event_store.high_water_mark()

    def latest_events_by_identity(
        self,
        *,
        after_position: int,
        through_position: int,
        limit: int,
    ) -> list[PreparedIndexingEvent]:
        if self.event_store is None:
            raise RuntimeError("Durable indexing event storage is not configured")
        return self.event_store.list_latest_by_identity(
            after_position=after_position,
            through_position=through_position,
            limit=limit,
        )

    def publish_latest_repair(self, repair: LatestRepairDraft) -> PreparedIndexingEvent:
        """Atomically clone the newest mutation and publish its higher sequence."""
        if self.event_store is None:
            raise RuntimeError("Durable indexing event storage is not configured")
        prepared = self.event_store.prepare_latest_repair(repair)
        if prepared.published_at is None:
            self._publish_prepared(prepared)
        return prepared

    def close(self) -> None:
        """Close the Kafka producer connection."""
        if self._producer:
            self._producer.close()
            self._producer = None
            logger.info("Kafka Producer closed.")
