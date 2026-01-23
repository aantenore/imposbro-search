"""
IMPOSBRO Search - Indexing Service Consumer

This module consumes document ingestion messages from Kafka and indexes
them into the appropriate Typesense clusters based on routing rules.

Features:
- Graceful shutdown handling
- Configurable logging
- Re-routing logic for document placement
- Error handling with retry support
"""

import os
import json
import signal
import logging
import sys
from typing import Dict, Optional
from kafka import KafkaConsumer
import time

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Graceful shutdown flag
shutdown_requested = False


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    global shutdown_requested
    logger.info(f"Received signal {signum}. Initiating graceful shutdown...")
    shutdown_requested = True


def determine_target_cluster(
    collection_name: str, document: Dict, routing_rules: Dict
) -> str:
    """
    Determine the target cluster for a document based on routing rules.

    This logic mirrors the Query API's routing logic to ensure consistent
    document placement.

    Args:
        collection_name: Name of the target collection
        document: Document being indexed
        routing_rules: Dictionary of routing rules keyed by collection

    Returns:
        Target cluster name
    """
    rule = routing_rules.get(collection_name)
    target_cluster_name = "default"  # Fallback cluster

    if not rule:
        return target_cluster_name

    # Check each rule in order
    for r in rule.get("rules", []):
        doc_value = document.get(r.get("field"))
        if doc_value is not None and str(doc_value) == str(r.get("value")):
            return r.get("cluster", target_cluster_name)

    # No rule matched, use default cluster for this collection
    return rule.get("default_cluster", target_cluster_name)


def run_consumer(typesense_clients: Dict, collection_routing_rules: Dict) -> None:
    """
    Main consumer loop for indexing documents from Kafka.

    Consumes messages from Kafka topics matching the configured pattern
    and indexes documents into the appropriate Typesense clusters.

    Args:
        typesense_clients: Dictionary mapping cluster names to Typesense clients
        collection_routing_rules: Dictionary of routing rules by collection
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

    # Connect to Kafka with retry logic
    while not shutdown_requested:
        try:
            consumer = KafkaConsumer(
                bootstrap_servers=kafka_broker_url,
                auto_offset_reset="earliest",
                group_id="imposbro_federated_indexing_group",
                value_deserializer=lambda m: json.loads(m.decode("utf-8")),
                consumer_timeout_ms=1000,  # Allow periodic shutdown checks
                enable_auto_commit=True,
                auto_commit_interval_ms=5000,
            )
            consumer.subscribe(pattern=f"^{topic_prefix}_.*")
            logger.info(
                f"Kafka Consumer connected and subscribed to pattern '{topic_prefix}_*'"
            )
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
                        process_message(
                            message.value, typesense_clients, collection_routing_rules
                        )
                    except Exception as e:
                        logger.error(f"Error processing message: {e}")
                        # Continue processing other messages

        except Exception as e:
            if not shutdown_requested:
                logger.error(f"Error in consumer loop: {e}")
                time.sleep(1)

    # Cleanup
    if consumer:
        logger.info("Closing Kafka consumer...")
        consumer.close()

    logger.info("Indexing service shutdown complete.")


def process_message(
    message: Dict, typesense_clients: Dict, routing_rules: Dict
) -> None:
    """
    Process a single message from Kafka.

    Args:
        message: The message payload containing document and metadata
        typesense_clients: Available Typesense clients
        routing_rules: Current routing rules
    """
    collection_name = message.get("collection")
    document = message.get("document")

    if not collection_name or not document:
        logger.warning(
            f"Invalid message format (missing collection or document): {message}"
        )
        return

    doc_id = document.get("id", "unknown")

    # Determine target cluster using current routing rules
    target_cluster = determine_target_cluster(collection_name, document, routing_rules)

    client = typesense_clients.get(target_cluster)
    if not client:
        logger.warning(
            f"No client found for cluster '{target_cluster}'. "
            f"Document {doc_id} may be lost."
        )
        return

    # Upsert the document
    try:
        client.collections[collection_name].documents.upsert(document)
        logger.info(
            f"Indexed document {doc_id} into '{collection_name}' "
            f"on cluster '{target_cluster}'"
        )
    except Exception as e:
        logger.error(f"Failed to index document {doc_id}: {e}")
        raise
