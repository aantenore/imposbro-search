"""Prometheus metrics for the indexing worker."""

import logging
import os
from typing import Dict, Tuple

from prometheus_client import REGISTRY, Counter, Gauge, start_http_server

logger = logging.getLogger(__name__)

DEFAULT_METRICS_PORT = 9108
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}

CLUSTER_CONFIG_FETCHES = Counter(
    "indexing_cluster_config_fetch_total",
    "Total Query API cluster configuration fetches by result.",
    ["result"],
)
LOADED_CLUSTERS = Gauge(
    "indexing_loaded_clusters",
    "Number of Typesense data cluster clients loaded by the indexing worker.",
)
INDEXED_DOCUMENTS = Counter(
    "indexing_documents_indexed_total",
    "Total documents successfully indexed by the worker.",
    ["collection", "target_cluster"],
)
DELETED_DOCUMENTS = Counter(
    "indexing_documents_deleted_total",
    "Total document delete events processed by the worker.",
    ["collection", "target_cluster", "result"],
)
PROCESSING_RETRIES = Counter(
    "indexing_processing_retries_total",
    "Total failed processing attempts before success or DLQ.",
    ["collection", "target_cluster", "error"],
)
DLQ_MESSAGES = Counter(
    "indexing_dlq_messages_total",
    "Total messages published to the indexing dead-letter topic.",
    ["source_topic", "error"],
)


def parse_bool_env(name: str, default: bool) -> bool:
    """Parse a bool env var with safe fallback semantics."""
    raw_value = os.environ.get(name, "").strip().lower()
    if not raw_value:
        return default
    if raw_value in TRUE_VALUES:
        return True
    if raw_value in FALSE_VALUES:
        return False
    logger.warning("Invalid %s=%r. Falling back to %s.", name, raw_value, default)
    return default


def parse_int_env(name: str, default: int) -> int:
    """Parse a positive integer env var with safe fallback semantics."""
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        logger.warning("Invalid %s=%r. Falling back to %s.", name, raw_value, default)
        return default
    if parsed <= 0:
        logger.warning("Invalid %s=%r. Falling back to %s.", name, raw_value, default)
        return default
    return parsed


def start_metrics_server_from_env() -> bool:
    """Start the Prometheus metrics HTTP server when enabled."""
    if not parse_bool_env("INDEXING_METRICS_ENABLED", True):
        logger.info("Indexing metrics HTTP server disabled.")
        return False

    port = parse_int_env("INDEXING_METRICS_PORT", DEFAULT_METRICS_PORT)
    try:
        start_http_server(port)
    except OSError as exc:
        logger.error(
            "Failed to start indexing metrics HTTP server on :%s: %s",
            port,
            exc,
        )
        return False
    logger.info("Indexing metrics HTTP server listening on :%s", port)
    return True


def message_labels(message: Dict) -> Tuple[str, str]:
    """Return low-cardinality collection and cluster labels for a Kafka payload."""
    collection = message.get("collection") or "unknown"
    target_cluster = message.get("target_cluster") or "default"
    return str(collection), str(target_cluster)
