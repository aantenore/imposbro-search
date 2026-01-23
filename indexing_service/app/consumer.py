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


def run_consumer(typesense_clients: Dict) -> None:
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
                        process_message(message.value, typesense_clients)
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
    collection_name = message.get("collection")
    document = message.get("document")
    target_cluster = message.get("target_cluster")

    # Validate message structure
    if not collection_name or not document:
        logger.warning(f"Invalid message format (missing collection or document)")
        return

    if not target_cluster:
        logger.warning(
            f"Message missing target_cluster field. "
            f"Falling back to 'default' cluster for backward compatibility."
        )
        target_cluster = "default"

    doc_id = document.get("id", "unknown")

    # Get the client for the target cluster (as determined by Producer)
    client = typesense_clients.get(target_cluster)

    if not client:
        # Log error but don't crash - cluster might have been removed
        logger.error(
            f"No client found for cluster '{target_cluster}'. "
            f"Document {doc_id} cannot be indexed. "
            f"Available clusters: {list(typesense_clients.keys())}"
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
        logger.error(
            f"Failed to index document {doc_id} to {collection_name}@{target_cluster}: {e}"
        )
        raise
