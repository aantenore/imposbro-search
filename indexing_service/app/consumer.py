"""
IMPOSBRO Search - Indexing Service Consumer

This module consumes document ingestion messages from Kafka and indexes
them into the appropriate Typesense clusters.

Architecture Decision: SMART PRODUCER PATTERN
The Producer (Query API) determines the target cluster and includes it in the
Kafka message. The Consumer trusts this decision and executes it directly.
This ensures consistency and avoids routing logic duplication.
"""

import base64
import json
import os
import re
import signal
import logging
import sys
from typing import Dict, Optional
from kafka import KafkaConsumer, KafkaProducer
from kafka.structs import OffsetAndMetadata, TopicPartition
import time
import typesense

from checkpoint_store import (
    CheckpointStore,
    CheckpointStoreError,
    CheckpointLockTimeoutError,
    EventCheckpointSession,
    EventOrderingError,
    build_checkpoint_store_from_env,
    checkpoint_from_event,
    classify_event,
)
from event_envelope import (
    EventEnvelopeError,
    IndexingEventV2,
    LegacyIndexingEvent,
    legacy_events_enabled,
    parse_indexing_event,
)
import metrics
from telemetry import consumer_span, typesense_span

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Graceful shutdown flag
shutdown_requested = False
DEFAULT_METADATA_MAX_AGE_MS = 5000
DEFAULT_MAX_PROCESSING_ATTEMPTS = 3
DEFAULT_MAX_POLL_INTERVAL_MS = 300000
DEFAULT_MAX_POLL_RECORDS = 1
DLQ_TOPIC_SUFFIX = "_dlq"
KAFKA_SECURITY_PROTOCOLS = {
    "PLAINTEXT",
    "SSL",
    "SASL_PLAINTEXT",
    "SASL_SSL",
}
KAFKA_SASL_MECHANISMS = {"PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"}
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
KAFKA_RESOURCE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,249}$")


class MissingTargetClusterError(RuntimeError):
    """Raised when a message targets a cluster the consumer has not loaded."""


class InvalidKafkaMessageError(ValueError):
    """Raised when a Kafka record cannot be decoded into an indexing payload."""


class KafkaConfigurationError(RuntimeError):
    """Raised when Kafka transport security is incomplete or contradictory."""


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    global shutdown_requested
    logger.info(f"Received signal {signum}. Initiating graceful shutdown...")
    shutdown_requested = True


def get_int_env(name: str, default: int) -> int:
    """Read an integer environment variable with a safe fallback."""
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        logger.warning("Invalid %s=%r. Falling back to %s.", name, raw_value, default)
        return default
    if value < 1:
        logger.warning("Invalid %s=%r. Falling back to %s.", name, raw_value, default)
        return default
    return value


def _strict_bool_env(name: str, default: bool) -> bool:
    raw_value = os.environ.get(name, "").strip().lower()
    if not raw_value:
        return default
    if raw_value in TRUE_VALUES:
        return True
    if raw_value in FALSE_VALUES:
        return False
    raise KafkaConfigurationError(f"{name} must be a boolean")


def _kafka_resource_env(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if not KAFKA_RESOURCE_PATTERN.fullmatch(value):
        raise KafkaConfigurationError(f"{name} has an invalid Kafka resource name")
    return value


def kafka_security_kwargs() -> Dict:
    """Build kafka-python TLS/SASL arguments without logging credentials."""
    protocol = os.environ.get("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT").strip().upper()
    if protocol not in KAFKA_SECURITY_PROTOCOLS:
        raise KafkaConfigurationError(
            "KAFKA_SECURITY_PROTOCOL must be PLAINTEXT, SSL, "
            "SASL_PLAINTEXT, or SASL_SSL"
        )

    kwargs = {"security_protocol": protocol}
    mechanism = os.environ.get("KAFKA_SASL_MECHANISM", "").strip().upper()
    username_value = os.environ.get("KAFKA_SASL_USERNAME")
    password_value = os.environ.get("KAFKA_SASL_PASSWORD")
    uses_sasl = protocol in {"SASL_PLAINTEXT", "SASL_SSL"}
    if uses_sasl:
        if mechanism not in KAFKA_SASL_MECHANISMS:
            raise KafkaConfigurationError(
                "KAFKA_SASL_MECHANISM must be PLAIN, SCRAM-SHA-256, "
                "or SCRAM-SHA-512 for a SASL transport"
            )
        if not username_value or not password_value:
            raise KafkaConfigurationError(
                "KAFKA_SASL_USERNAME and KAFKA_SASL_PASSWORD are required "
                "for a SASL transport"
            )
        kwargs.update(
            sasl_mechanism=mechanism,
            sasl_plain_username=username_value,
            sasl_plain_password=password_value,
        )
    elif mechanism or username_value or password_value:
        raise KafkaConfigurationError(
            "SASL credentials or mechanism require a SASL security protocol"
        )

    cafile = os.environ.get("KAFKA_SSL_CAFILE", "").strip()
    certfile = os.environ.get("KAFKA_SSL_CERTFILE", "").strip()
    keyfile = os.environ.get("KAFKA_SSL_KEYFILE", "").strip()
    uses_tls = protocol in {"SSL", "SASL_SSL"}
    if uses_tls:
        if bool(certfile) != bool(keyfile):
            raise KafkaConfigurationError(
                "KAFKA_SSL_CERTFILE and KAFKA_SSL_KEYFILE must be configured together"
            )
        for env_name, path in (
            ("KAFKA_SSL_CAFILE", cafile),
            ("KAFKA_SSL_CERTFILE", certfile),
            ("KAFKA_SSL_KEYFILE", keyfile),
        ):
            if path and not os.path.isfile(path):
                raise KafkaConfigurationError(f"{env_name} does not reference a file")
        kwargs["ssl_check_hostname"] = _strict_bool_env(
            "KAFKA_SSL_CHECK_HOSTNAME",
            True,
        )
        if cafile:
            kwargs["ssl_cafile"] = cafile
        if certfile:
            kwargs["ssl_certfile"] = certfile
            kwargs["ssl_keyfile"] = keyfile
    elif cafile or certfile or keyfile:
        raise KafkaConfigurationError(
            "Kafka TLS files require SSL or SASL_SSL security protocol"
        )
    return kwargs


def create_dlq_producer(kafka_broker_url: str) -> KafkaProducer:
    """Create a producer for quarantining poison messages."""
    return KafkaProducer(
        bootstrap_servers=kafka_broker_url,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        client_id=_kafka_resource_env(
            "INDEXING_KAFKA_DLQ_CLIENT_ID",
            "imposbro-indexing-dlq",
        ),
        acks="all",
        retries=3,
        **kafka_security_kwargs(),
    )


def create_kafka_consumer(kafka_broker_url: str, topic_prefix: str) -> KafkaConsumer:
    """Create a consumer tuned to discover dynamically created collection topics."""
    metadata_max_age_ms = get_int_env(
        "KAFKA_METADATA_MAX_AGE_MS", DEFAULT_METADATA_MAX_AGE_MS
    )
    max_poll_interval_ms = get_int_env(
        "KAFKA_MAX_POLL_INTERVAL_MS",
        DEFAULT_MAX_POLL_INTERVAL_MS,
    )
    max_poll_records = get_int_env(
        "KAFKA_MAX_POLL_RECORDS",
        DEFAULT_MAX_POLL_RECORDS,
    )
    consumer = KafkaConsumer(
        bootstrap_servers=kafka_broker_url,
        client_id=_kafka_resource_env(
            "INDEXING_KAFKA_CONSUMER_CLIENT_ID",
            "imposbro-indexing-consumer",
        ),
        auto_offset_reset="earliest",
        group_id=_kafka_resource_env(
            "INDEXING_KAFKA_CONSUMER_GROUP_ID",
            "imposbro_federated_indexing_group",
        ),
        consumer_timeout_ms=1000,  # Allow periodic shutdown checks
        enable_auto_commit=False,
        metadata_max_age_ms=metadata_max_age_ms,
        max_poll_interval_ms=max_poll_interval_ms,
        max_poll_records=max_poll_records,
        **kafka_security_kwargs(),
    )
    pattern = build_topic_subscription_pattern(topic_prefix)
    consumer.subscribe(pattern=pattern)
    logger.info(
        "Kafka Consumer connected and subscribed to pattern '%s' "
        "(metadata refresh: %sms)",
        pattern,
        metadata_max_age_ms,
    )
    return consumer


def decode_kafka_value(raw_value) -> Dict:
    """Decode raw Kafka record bytes into the JSON object expected by the worker."""
    try:
        if isinstance(raw_value, bytes):
            decoded = raw_value.decode("utf-8")
        elif isinstance(raw_value, str):
            decoded = raw_value
        elif isinstance(raw_value, dict):
            return raw_value
        else:
            raise TypeError(f"unsupported payload type {type(raw_value).__name__}")
        message = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise InvalidKafkaMessageError(f"Invalid Kafka JSON payload: {exc}") from exc

    if not isinstance(message, dict):
        raise InvalidKafkaMessageError("Invalid Kafka JSON payload: expected object")
    return message


def dlq_safe_message(raw_value):
    """Return a JSON-serializable representation of a raw poison Kafka payload."""
    if isinstance(raw_value, bytes):
        return raw_value.decode("utf-8", errors="replace")
    return raw_value


def commit_message_offset(consumer, message) -> None:
    """Commit the offset immediately after successful processing or quarantine."""
    consumer.commit(
        {
            TopicPartition(message.topic, message.partition): OffsetAndMetadata(
                message.offset + 1,
                None,
            )
        }
    )


def build_topic_subscription_pattern(topic_prefix: str) -> str:
    """Build a collection-topic regex that excludes the worker DLQ topic."""
    escaped_prefix = re.escape(topic_prefix)
    dlq_topic_name = re.escape(DLQ_TOPIC_SUFFIX.lstrip("_"))
    return rf"^{escaped_prefix}_(?!{dlq_topic_name}$).*"


def resolve_target_cluster(target_cluster: Optional[str], typesense_clients: Dict) -> str:
    """Resolve legacy/virtual target names to a concrete loaded data cluster."""
    if target_cluster and target_cluster != "default":
        return target_cluster
    if "default-data-cluster" in typesense_clients:
        return "default-data-cluster"
    if typesense_clients:
        return next(iter(typesense_clients.keys()))
    return target_cluster or "default"


def _set_consumer_active(health_state, active: bool) -> None:
    """Update optional worker readiness without coupling the consumer to HTTP."""
    if health_state is not None:
        health_state.set_consumer_active(active)


def run_consumer(
    typesense_clients: Dict,
    refresh_clients=None,
    health_state=None,
    checkpoint_store: Optional[CheckpointStore] = None,
    allow_legacy: Optional[bool] = None,
) -> None:
    """
    Main consumer loop for indexing documents from Kafka.

    Implements the SMART PRODUCER pattern:
    - The Producer (Query API) decides the target cluster
    - The Consumer trusts and executes that decision
    - No routing logic duplication

    Args:
        typesense_clients: Dictionary mapping cluster names to Typesense clients
    """
    global shutdown_requested
    _set_consumer_active(health_state, False)
    kafka_broker_url = os.environ.get("KAFKA_BROKER_URL")
    topic_prefix = os.environ.get("KAFKA_TOPIC_PREFIX")
    if not kafka_broker_url or not topic_prefix:
        raise KafkaConfigurationError(
            "KAFKA_BROKER_URL and KAFKA_TOPIC_PREFIX are required"
        )
    kafka_security_kwargs()
    if allow_legacy is None:
        allow_legacy = legacy_events_enabled()
    if checkpoint_store is None:
        checkpoint_store = build_checkpoint_store_from_env(typesense_clients)
    checkpoint_store.ensure_ready()

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    consumer: Optional[KafkaConsumer] = None
    dlq_producer: Optional[KafkaProducer] = None
    max_processing_attempts = get_int_env(
        "INDEXING_MAX_PROCESSING_ATTEMPTS", DEFAULT_MAX_PROCESSING_ATTEMPTS
    )

    # Connect to Kafka with retry logic
    while not shutdown_requested:
        try:
            consumer = create_kafka_consumer(kafka_broker_url, topic_prefix)
            dlq_producer = create_dlq_producer(kafka_broker_url)
            _set_consumer_active(health_state, True)
            break
        except Exception as e:
            logger.warning(f"Failed to connect to Kafka: {e}. Retrying in 5 seconds...")
            time.sleep(5)

    if not consumer:
        logger.error("Failed to initialize Kafka consumer")
        return

    logger.info("Listening for documents to index...")

    # Main processing loop
    while not shutdown_requested:
        try:
            # Poll for messages (with timeout for shutdown checks)
            messages = consumer.poll(timeout_ms=1000)
            _set_consumer_active(health_state, True)

            for topic_partition, msgs in messages.items():
                for message in msgs:
                    if shutdown_requested:
                        break

                    try:
                        decoded_message = decode_kafka_value(message.value)
                    except InvalidKafkaMessageError as e:
                        logger.error(
                            "Invalid Kafka payload from %s partition=%s offset=%s: %s",
                            message.topic,
                            message.partition,
                            message.offset,
                            e,
                        )
                        if not dlq_producer:
                            break
                        try:
                            publish_to_dlq(
                                dlq_producer,
                                topic_prefix=topic_prefix,
                                source_topic=message.topic,
                                message=dlq_safe_message(message.value),
                                error=e,
                                message_key=getattr(message, "key", None),
                                source_partition=message.partition,
                                source_offset=message.offset,
                            )
                            commit_message_offset(consumer, message)
                        except Exception as dlq_error:
                            logger.error(
                                "Failed to quarantine invalid Kafka payload: %s",
                                dlq_error,
                            )
                            break
                        continue

                    try:
                        process_message_with_retries(
                            decoded_message,
                            typesense_clients,
                            refresh_clients=refresh_clients,
                            dlq_producer=dlq_producer,
                            source_topic=message.topic,
                            topic_prefix=topic_prefix,
                            max_attempts=max_processing_attempts,
                            checkpoint_store=checkpoint_store,
                            message_key=getattr(message, "key", None),
                            allow_legacy=allow_legacy,
                            source_partition=message.partition,
                            source_offset=message.offset,
                        )
                        commit_message_offset(consumer, message)
                    except Exception as e:
                        logger.error(f"Error processing message: {e}")
                        # Do not commit failed offsets unless they have been
                        # published to the DLQ. Leaving the offset uncommitted
                        # lets Kafka retry transient infrastructure failures.
                        break

        except Exception as e:
            if not shutdown_requested:
                _set_consumer_active(health_state, False)
                logger.error(f"Error in consumer loop: {e}")
                time.sleep(1)

    # Cleanup
    _set_consumer_active(health_state, False)
    if consumer:
        logger.info("Closing Kafka consumer...")
        consumer.close()
    if dlq_producer:
        logger.info("Closing Kafka DLQ producer...")
        dlq_producer.close()

    logger.info("Indexing service shutdown complete.")


def process_message_with_retries(
    message: Dict,
    typesense_clients: Dict,
    *,
    refresh_clients=None,
    dlq_producer=None,
    source_topic: str,
    topic_prefix: str,
    max_attempts: int,
    checkpoint_store: Optional[CheckpointStore] = None,
    message_key=None,
    allow_legacy: Optional[bool] = None,
    source_partition: Optional[int] = None,
    source_offset: Optional[int] = None,
) -> None:
    """Process a message with bounded retries, config refresh, and DLQ quarantine."""
    last_error: Optional[Exception] = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            process_message(
                message,
                typesense_clients,
                checkpoint_store=checkpoint_store,
                message_key=message_key,
                allow_legacy=allow_legacy,
            )
            return
        except (EventEnvelopeError, EventOrderingError) as exc:
            last_error = exc
            collection, target_cluster = metrics.message_labels(message)
            metrics.PROCESSING_RETRIES.labels(
                collection=collection,
                target_cluster=target_cluster,
                error=type(exc).__name__,
            ).inc()
            metrics.INDEXING_EVENTS.labels(
                envelope_version=metrics.envelope_version_label(message),
                operation=metrics.operation_label(message),
                result="rejected",
            ).inc()
            break
        except MissingTargetClusterError as exc:
            last_error = exc
            collection, target_cluster = metrics.message_labels(message)
            metrics.PROCESSING_RETRIES.labels(
                collection=collection,
                target_cluster=target_cluster,
                error=type(exc).__name__,
            ).inc()
            if refresh_clients:
                logger.warning("%s Refreshing cluster configuration...", exc)
                refreshed_clients = refresh_clients()
                typesense_clients.clear()
                typesense_clients.update(refreshed_clients)
                continue
            break
        except Exception as exc:
            last_error = exc
            collection, target_cluster = metrics.message_labels(message)
            metrics.PROCESSING_RETRIES.labels(
                collection=collection,
                target_cluster=target_cluster,
                error=type(exc).__name__,
            ).inc()
            logger.warning(
                "Indexing attempt %s/%s failed: %s",
                attempt,
                max_attempts,
                exc,
            )
            if attempt < max_attempts:
                time.sleep(1)

    if dlq_producer and last_error:
        if not isinstance(last_error, (EventEnvelopeError, EventOrderingError)):
            metrics.INDEXING_EVENTS.labels(
                envelope_version=metrics.envelope_version_label(message),
                operation=metrics.operation_label(message),
                result="failed",
            ).inc()
        publish_to_dlq(
            dlq_producer,
            topic_prefix=topic_prefix,
            source_topic=source_topic,
            message=message,
            error=last_error,
            message_key=message_key,
            source_partition=source_partition,
            source_offset=source_offset,
        )
        return
    if last_error:
        if not isinstance(last_error, (EventEnvelopeError, EventOrderingError)):
            metrics.INDEXING_EVENTS.labels(
                envelope_version=metrics.envelope_version_label(message),
                operation=metrics.operation_label(message),
                result="failed",
            ).inc()
        raise last_error


def publish_to_dlq(
    dlq_producer,
    *,
    topic_prefix: str,
    source_topic: str,
    message,
    error: Exception,
    message_key=None,
    source_partition: Optional[int] = None,
    source_offset: Optional[int] = None,
) -> None:
    """Publish a failed indexing message to a dead-letter topic."""
    dlq_topic = f"{topic_prefix}{DLQ_TOPIC_SUFFIX}"
    payload = {
        "source_topic": source_topic,
        "error": type(error).__name__,
        "error_message": str(error),
        "message": message,
        "source_partition": source_partition,
        "source_offset": source_offset,
    }
    encoded_key = _encode_dlq_key(message_key)
    if encoded_key is not None:
        payload["message_key"] = encoded_key
        payload["message_key_encoding"] = "base64"
    dlq_producer.send(dlq_topic, value=payload)
    dlq_producer.flush()
    metrics.DLQ_MESSAGES.labels(
        source_topic=source_topic,
        error=type(error).__name__,
    ).inc()
    logger.error(
        "Published failed message from %s to DLQ %s: %s",
        source_topic,
        dlq_topic,
        error,
    )


def _encode_dlq_key(message_key) -> Optional[str]:
    if message_key is None:
        return None
    if isinstance(message_key, str):
        raw_key = message_key.encode("utf-8")
    elif isinstance(message_key, bytes):
        raw_key = message_key
    else:
        raw_key = str(message_key).encode("utf-8")
    return base64.b64encode(raw_key).decode("ascii")


def process_message(
    message: Dict,
    typesense_clients: Dict,
    *,
    checkpoint_store: Optional[CheckpointStore] = None,
    message_key=None,
    allow_legacy: Optional[bool] = None,
) -> None:
    """
    Process a single message from Kafka.

    Version 2 carries one logical event and every physical target. Checkpointing
    after each target makes retries resume only incomplete fan-out work.

    Args:
        message: The message payload containing document and routing info
        typesense_clients: Available Typesense clients
    """
    if allow_legacy is None:
        allow_legacy = legacy_events_enabled()
    event = parse_indexing_event(message, allow_legacy=allow_legacy)
    if isinstance(event, LegacyIndexingEvent):
        _process_legacy_event(event, typesense_clients)
        metrics.INDEXING_EVENTS.labels(
            envelope_version="legacy",
            operation=event.operation,
            result="applied",
        ).inc()
        return
    if checkpoint_store is None:
        raise CheckpointStoreError(
            "Version 2 events require a durable checkpoint store"
        )
    _validate_kafka_key(event, message_key)
    trace_carrier = {
        "traceparent": event.trace.traceparent,
        "tracestate": event.trace.tracestate,
    }
    with consumer_span(trace_carrier):
        _process_v2_with_checkpoint(event, typesense_clients, checkpoint_store)


def _process_v2_with_checkpoint(
    event: IndexingEventV2,
    typesense_clients: Dict,
    checkpoint_store: CheckpointStore,
) -> None:
    """Apply one validated v2 event inside its extracted Kafka trace context."""
    try:
        with checkpoint_store.event_scope(event) as checkpoint_session:
            result = _process_v2_event(
                event,
                typesense_clients,
                checkpoint_session,
            )
    except Exception as exc:
        logger.error(
            "Failed event=%s operation=%s identity=%s request_id=%s "
            "traceparent=%s: %s",
            event.event_id,
            event.operation,
            event.identity.logical_key,
            event.trace.request_id or event.trace.correlation_id or "-",
            event.trace.traceparent or "-",
            exc,
        )
        scope_result = (
            "lock_timeout"
            if isinstance(exc, CheckpointLockTimeoutError)
            else "processing_error"
        )
        metrics.CHECKPOINT_SCOPES.labels(
            backend=checkpoint_store.backend_name,
            result=scope_result,
        ).inc()
        raise
    metrics.CHECKPOINT_SCOPES.labels(
        backend=checkpoint_store.backend_name,
        result="committed",
    ).inc()
    metrics.INDEXING_EVENTS.labels(
        envelope_version="2",
        operation=event.operation,
        result=result,
    ).inc()


def _process_v2_event(
    event: IndexingEventV2,
    typesense_clients: Dict,
    checkpoint_store: EventCheckpointSession,
) -> str:
    request_id = event.trace.request_id or event.trace.correlation_id or "-"
    traceparent = event.trace.traceparent or "-"
    decisions = []
    target_plans = []
    for target_cluster in event.target_clusters:
        client = typesense_clients.get(target_cluster)
        if client is None:
            raise MissingTargetClusterError(
                f"No client found for cluster '{target_cluster}'. "
                f"Document {event.identity.document_id} cannot be indexed. "
                f"Available clusters: {list(typesense_clients.keys())} "
                f"request_id={request_id} event_id={event.event_id} "
                f"traceparent={traceparent}"
            )

        try:
            checkpoint = checkpoint_store.load(event, target_cluster)
        except CheckpointStoreError:
            metrics.CHECKPOINT_OPERATIONS.labels(
                backend=checkpoint_store.backend_name,
                result="load_error",
            ).inc()
            raise
        metrics.CHECKPOINT_OPERATIONS.labels(
            backend=checkpoint_store.backend_name,
            result="load_hit" if checkpoint is not None else "load_miss",
        ).inc()
        decision = classify_event(event, checkpoint)
        decisions.append(decision)
        target_plans.append((target_cluster, client, decision))

    suppress_applies = "stale" in decisions
    if suppress_applies and "apply" in decisions:
        logger.warning(
            "Skipped incomplete fan-out for stale event=%s identity=%s; "
            "at least one target has already advanced traceparent=%s",
            event.event_id,
            event.identity.logical_key,
            traceparent,
        )

    for target_cluster, client, decision in target_plans:
        effective_decision = (
            "suppressed_stale"
            if suppress_applies and decision == "apply"
            else decision
        )
        metrics.IDEMPOTENCY_DECISIONS.labels(
            backend=checkpoint_store.backend_name,
            operation=event.operation,
            decision=effective_decision,
        ).inc()
        if decision != "apply" or suppress_applies:
            logger.info(
                "Skipped %s event=%s document=%s target=%s decision=%s "
                "traceparent=%s",
                event.operation,
                event.event_id,
                event.identity.document_id,
                target_cluster,
                effective_decision,
                traceparent,
            )
            continue

        if event.operation in {"delete", "tombstone"}:
            delete_document(
                client,
                collection_name=event.identity.collection,
                document_id=event.identity.document_id,
                target_cluster=target_cluster,
                request_id=request_id,
                filter_by=event.delete_filter,
                traceparent=traceparent,
            )
        else:
            _upsert_document(
                client,
                collection_name=event.identity.collection,
                document=event.document or {},
                target_cluster=target_cluster,
                request_id=request_id,
                traceparent=traceparent,
            )

        try:
            checkpoint_store.save(checkpoint_from_event(event, target_cluster))
        except CheckpointStoreError:
            metrics.CHECKPOINT_OPERATIONS.labels(
                backend=checkpoint_store.backend_name,
                result="save_error",
            ).inc()
            raise
        metrics.CHECKPOINT_OPERATIONS.labels(
            backend=checkpoint_store.backend_name,
            result="write_staged",
        ).inc()
        logger.info(
            "Applied event=%s operation=%s identity=%s target=%s "
            "document_version=%s sequence=%s routing_revision=%s rollout_id=%s "
            "traceparent=%s",
            event.event_id,
            event.operation,
            event.identity.logical_key,
            target_cluster,
            event.document_version,
            event.sequence,
            event.routing_revision,
            event.rollout_id or "-",
            traceparent,
        )

    if "apply" in decisions and not suppress_applies:
        result = "applied"
    elif "stale" in decisions:
        result = "stale"
    elif all(decision == "duplicate" for decision in decisions):
        result = "duplicate"
    else:
        result = "noop_mixed"
    return result


def _validate_kafka_key(event: IndexingEventV2, message_key) -> None:
    if message_key is None:
        raise EventEnvelopeError(
            "Version 2 events require a Kafka key derived from tenant, collection, and document"
        )
    if isinstance(message_key, str):
        actual_key = message_key.encode("utf-8")
    elif isinstance(message_key, bytes):
        actual_key = message_key
    else:
        raise EventEnvelopeError("Kafka message key must be UTF-8 bytes or string")
    if actual_key != event.identity.kafka_key:
        raise EventEnvelopeError(
            "Kafka message key does not match the event identity"
        )


def _process_legacy_event(event: LegacyIndexingEvent, typesense_clients: Dict) -> None:
    target_cluster = resolve_target_cluster(event.target_cluster, typesense_clients)
    if not event.target_cluster:
        logger.warning(
            "Legacy message missing target_cluster request_id=%s; using '%s'.",
            event.request_id,
            target_cluster,
        )
    client = typesense_clients.get(target_cluster)
    if client is None:
        raise MissingTargetClusterError(
            f"No client found for cluster '{target_cluster}'. "
            f"Document {event.document_id} cannot be indexed. "
            f"Available clusters: {list(typesense_clients.keys())} "
            f"request_id={event.request_id}"
        )
    if event.operation == "delete":
        delete_document(
            client,
            collection_name=event.collection,
            document_id=event.document_id,
            target_cluster=target_cluster,
            request_id=event.request_id,
            filter_by=event.delete_filter,
        )
        return
    _upsert_document(
        client,
        collection_name=event.collection,
        document=event.document or {},
        target_cluster=target_cluster,
        request_id=event.request_id,
    )


def _upsert_document(
    client,
    *,
    collection_name: str,
    document: Dict,
    target_cluster: str,
    request_id: str,
    traceparent: str = "-",
) -> None:
    try:
        with typesense_span("upsert", target_cluster):
            client.collections[collection_name].documents.upsert(document)
        metrics.INDEXED_DOCUMENTS.labels(
            collection=collection_name,
            target_cluster=target_cluster,
        ).inc()
        logger.info(
            "Indexed document %s into '%s' on cluster '%s' request_id=%s "
            "traceparent=%s",
            document.get("id", "unknown"),
            collection_name,
            target_cluster,
            request_id,
            traceparent,
        )
    except Exception as e:
        logger.error(
            "Failed to index document %s to %s@%s request_id=%s "
            "traceparent=%s: %s",
            document.get("id", "unknown"),
            collection_name,
            target_cluster,
            request_id,
            traceparent,
            e,
        )
        raise


def delete_document(
    client,
    *,
    collection_name: str,
    document_id: str,
    target_cluster: str,
    request_id: str,
    filter_by: Optional[str] = None,
    traceparent: str = "-",
) -> None:
    """Delete a document by id or tenant-safe filter, treating misses as no-ops."""
    result_label = "deleted"
    try:
        with typesense_span("delete", target_cluster):
            if filter_by:
                result = client.collections[collection_name].documents.delete(
                    {"filter_by": filter_by}
                )
                deleted_count = int((result or {}).get("num_deleted", 0))
                result_label = "deleted" if deleted_count > 0 else "not_found"
            else:
                client.collections[collection_name].documents[document_id].delete()
    except typesense.exceptions.ObjectNotFound:
        result_label = "not_found"
    except Exception as e:
        logger.error(
            "Failed to delete document %s from %s@%s request_id=%s "
            "traceparent=%s: %s",
            document_id,
            collection_name,
            target_cluster,
            request_id,
            traceparent,
            e,
        )
        raise

    metrics.DELETED_DOCUMENTS.labels(
        collection=collection_name,
        target_cluster=target_cluster,
        result=result_label,
    ).inc()
    logger.info(
        "Processed delete for document %s from '%s' on cluster '%s' result=%s "
        "request_id=%s traceparent=%s",
        document_id,
        collection_name,
        target_cluster,
        result_label,
        request_id,
        traceparent,
    )
