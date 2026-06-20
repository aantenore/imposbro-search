"""
IMPOSBRO Search - Indexing Service Consumer

This module consumes document ingestion messages from Kafka and indexes
them into the appropriate Typesense clusters.

Architecture Decision: SMART PRODUCER PATTERN
The Producer (Query API) determines the target cluster and includes it in the
Kafka message. The Consumer trusts this decision and executes it directly.
This ensures consistency and avoids routing logic duplication.
"""

import os
import json
import re
import signal
import logging
import sys
from typing import Dict, Optional
from kafka import KafkaConsumer, KafkaProducer
from kafka.structs import OffsetAndMetadata, TopicPartition
import time
import typesense

import metrics

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
DLQ_TOPIC_SUFFIX = "_dlq"


class MissingTargetClusterError(RuntimeError):
    """Raised when a message targets a cluster the consumer has not loaded."""


class InvalidKafkaMessageError(ValueError):
    """Raised when a Kafka record cannot be decoded into an indexing payload."""


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
        return int(raw_value)
    except ValueError:
        logger.warning("Invalid %s=%r. Falling back to %s.", name, raw_value, default)
        return default


def create_dlq_producer(kafka_broker_url: str) -> KafkaProducer:
    """Create a producer for quarantining poison messages."""
    return KafkaProducer(
        bootstrap_servers=kafka_broker_url,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        acks="all",
        retries=3,
    )


def create_kafka_consumer(kafka_broker_url: str, topic_prefix: str) -> KafkaConsumer:
    """Create a consumer tuned to discover dynamically created collection topics."""
    metadata_max_age_ms = get_int_env(
        "KAFKA_METADATA_MAX_AGE_MS", DEFAULT_METADATA_MAX_AGE_MS
    )
    consumer = KafkaConsumer(
        bootstrap_servers=kafka_broker_url,
        auto_offset_reset="earliest",
        group_id="imposbro_federated_indexing_group",
        consumer_timeout_ms=1000,  # Allow periodic shutdown checks
        enable_auto_commit=False,
        metadata_max_age_ms=metadata_max_age_ms,
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


def run_consumer(typesense_clients: Dict, refresh_clients=None) -> None:
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

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    kafka_broker_url = os.environ.get("KAFKA_BROKER_URL")
    topic_prefix = os.environ.get("KAFKA_TOPIC_PREFIX")

    if not kafka_broker_url or not topic_prefix:
        logger.error(
            "Missing required environment variables: KAFKA_BROKER_URL or KAFKA_TOPIC_PREFIX"
        )
        return

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
                logger.error(f"Error in consumer loop: {e}")
                time.sleep(1)

    # Cleanup
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
) -> None:
    """Process a message with bounded retries, config refresh, and DLQ quarantine."""
    last_error: Optional[Exception] = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            process_message(message, typesense_clients)
            return
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
        publish_to_dlq(
            dlq_producer,
            topic_prefix=topic_prefix,
            source_topic=source_topic,
            message=message,
            error=last_error,
        )
        return
    if last_error:
        raise last_error


def publish_to_dlq(
    dlq_producer,
    *,
    topic_prefix: str,
    source_topic: str,
    message,
    error: Exception,
) -> None:
    """Publish a failed indexing message to a dead-letter topic."""
    dlq_topic = f"{topic_prefix}{DLQ_TOPIC_SUFFIX}"
    payload = {
        "source_topic": source_topic,
        "error": type(error).__name__,
        "error_message": str(error),
        "message": message,
    }
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


def process_message(message: Dict, typesense_clients: Dict) -> None:
    """
    Process a single message from Kafka.

    SMART PRODUCER PATTERN:
    The message contains `target_cluster` as determined by the Producer.
    We trust this value and execute the indexing operation directly.

    Args:
        message: The message payload containing document and routing info
        typesense_clients: Available Typesense clients
    """
    action = str(message.get("action") or "upsert").strip().lower()
    collection_name = message.get("collection")
    document = message.get("document")
    document_id = str(message.get("document_id") or "")
    target_cluster = message.get("target_cluster")
    request_id = str(message.get("request_id") or "-")

    # Validate message structure
    if not collection_name:
        raise ValueError(
            f"Invalid message format (missing collection) request_id={request_id}"
        )
    if action == "upsert" and not document:
        raise ValueError(
            f"Invalid upsert message format (missing document) request_id={request_id}"
        )
    if action == "delete" and not document_id:
        raise ValueError(
            f"Invalid delete message format (missing document_id) request_id={request_id}"
        )
    if action not in {"upsert", "delete"}:
        raise ValueError(
            f"Unsupported indexing action '{action}' request_id={request_id}"
        )

    if not target_cluster:
        logger.warning(
            "Message missing target_cluster field request_id=%s. "
            "Falling back to 'default' cluster for backward compatibility.",
            request_id,
        )
    target_cluster = resolve_target_cluster(target_cluster, typesense_clients)

    doc_id = document.get("id", "unknown") if document else document_id

    # Get the client for the target cluster (as determined by Producer)
    client = typesense_clients.get(target_cluster)

    if not client:
        raise MissingTargetClusterError(
            f"No client found for cluster '{target_cluster}'. "
            f"Document {doc_id} cannot be indexed. "
            f"Available clusters: {list(typesense_clients.keys())} "
            f"request_id={request_id}"
        )

    if action == "delete":
        delete_document(
            client,
            collection_name=collection_name,
            document_id=document_id,
            target_cluster=target_cluster,
            request_id=request_id,
            filter_by=message.get("filter_by"),
        )
        return

    # Upsert the document
    try:
        client.collections[collection_name].documents.upsert(document)
        metrics.INDEXED_DOCUMENTS.labels(
            collection=collection_name,
            target_cluster=target_cluster,
        ).inc()
        logger.info(
            "Indexed document %s into '%s' on cluster '%s' request_id=%s",
            doc_id,
            collection_name,
            target_cluster,
            request_id,
        )
    except Exception as e:
        logger.error(
            "Failed to index document %s to %s@%s request_id=%s: %s",
            doc_id,
            collection_name,
            target_cluster,
            request_id,
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
) -> None:
    """Delete a document by id or tenant-safe filter, treating misses as no-ops."""
    result_label = "deleted"
    try:
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
            "Failed to delete document %s from %s@%s request_id=%s: %s",
            document_id,
            collection_name,
            target_cluster,
            request_id,
            e,
        )
        raise

    metrics.DELETED_DOCUMENTS.labels(
        collection=collection_name,
        target_cluster=target_cluster,
        result=result_label,
    ).inc()
    logger.info(
        "Processed delete for document %s from '%s' on cluster '%s' result=%s request_id=%s",
        document_id,
        collection_name,
        target_cluster,
        result_label,
        request_id,
    )
