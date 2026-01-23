"""
Kafka Producer Service for IMPOSBRO Search.

This module provides a managed Kafka producer for publishing document
ingestion messages to the message queue.
"""

import json
import logging
import time
from kafka import KafkaProducer
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


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

    def __init__(self, broker_url: str, topic_prefix: str):
        """
        Initialize the Kafka service.

        Args:
            broker_url: Kafka broker URL (e.g., 'kafka:29092')
            topic_prefix: Prefix for Kafka topic names
        """
        self.broker_url = broker_url
        self.topic_prefix = topic_prefix
        self._producer: Optional[KafkaProducer] = None

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
                    acks="all",  # Ensure durability
                    retries=3,
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

    def publish_document(
        self, collection_name: str, document: Dict[str, Any], target_cluster: str
    ) -> None:
        """
        Publish a document for indexing.

        Args:
            collection_name: Target collection name
            document: Document to index
            target_cluster: Target cluster for this document

        Raises:
            Exception: If publish fails
        """
        topic_name = f"{self.topic_prefix}_{collection_name}"
        doc_id = document.get("id", "unknown")

        message = {
            "target_cluster": target_cluster,
            "collection": collection_name,
            "document": document,
        }

        self.producer.send(topic_name, key=str(doc_id).encode("utf-8"), value=message)
        self.producer.flush()

        logger.debug(f"Published document {doc_id} to topic {topic_name}")

    def close(self) -> None:
        """Close the Kafka producer connection."""
        if self._producer:
            self._producer.close()
            self._producer = None
            logger.info("Kafka Producer closed.")
