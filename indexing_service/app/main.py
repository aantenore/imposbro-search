"""
IMPOSBRO Search - Indexing Service Entry Point

This service consumes document ingestion messages from Kafka and indexes
them into the appropriate federated Typesense clusters.

Architecture: SMART PRODUCER PATTERN
The Query API (Producer) determines routing. This Consumer executes the
routing decision without recalculating it.
"""

import os
import sys
import logging
import typesense
import time
import requests
from typing import Dict

import health as worker_health
import metrics
from checkpoint_store import CheckpointStoreError, build_checkpoint_store_from_env
from event_envelope import legacy_events_enabled
from telemetry import (
    TelemetryConfig,
    TelemetryConfigurationError,
    configure_tracing,
    get_runtime as get_telemetry_runtime,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def normalize_typesense_protocol(value) -> str:
    """Return a supported Typesense transport, defaulting legacy config to HTTP."""
    protocol = str(value or "http").strip().lower()
    if protocol not in {"http", "https"}:
        raise ValueError("Typesense protocol must be 'http' or 'https'.")
    return protocol


def build_admin_headers() -> Dict[str, str]:
    """Build service-to-service headers for Query API admin endpoints."""
    admin_api_key = (
        os.environ.get("INTERNAL_QUERY_API_ADMIN_API_KEY", "").strip()
        or os.environ.get("ADMIN_API_KEY", "").strip()
    )
    return {"X-API-Key": admin_api_key} if admin_api_key else {}


def create_typesense_client(cluster_info: Dict) -> typesense.Client:
    """
    Create a Typesense client from cluster configuration.

    Args:
        cluster_info: Configuration dictionary with host, port, and api_key

    Returns:
        Configured Typesense client
    """
    host = cluster_info.get("host", "")
    port = cluster_info.get("port", 8108)
    api_key = cluster_info.get("api_key", "")
    protocol = normalize_typesense_protocol(cluster_info.get("protocol"))

    # Handle comma-separated hosts for HA clusters
    nodes = [
        {"host": h.strip(), "port": port, "protocol": protocol}
        for h in host.split(",")
    ]

    return typesense.Client(
        {"nodes": nodes, "api_key": api_key, "connection_timeout_seconds": 5}
    )


def fetch_cluster_configuration(query_api_url: str) -> Dict[str, typesense.Client]:
    """
    Fetch cluster configuration from the Query API.

    Note: With Smart Producer pattern, we only need cluster clients.
    Routing rules are no longer needed by the Consumer.

    Args:
        query_api_url: Base URL of the Query API

    Returns:
        Dictionary mapping cluster names to Typesense clients
    """
    federation_clients: Dict[str, typesense.Client] = {}

    while True:
        try:
            # Fetch only cluster configuration (no routing rules needed)
            endpoint = f"{query_api_url}/admin/federation/clusters/internal"
            logger.info("Fetching internal cluster config from %s...", endpoint)
            cluster_response = requests.get(
                endpoint,
                headers=build_admin_headers(),
                timeout=10,
            )
            cluster_response.raise_for_status()
            cluster_data = cluster_response.json()

            if not cluster_data:
                metrics.CLUSTER_CONFIG_FETCHES.labels(result="empty").inc()
                logger.warning(
                    "Query API returned no clusters. Retrying in 10 seconds..."
                )
                time.sleep(10)
                continue

            # Create Typesense clients for each cluster.
            # Skip the virtual "default" entry (internal HA cluster) returned by the admin API.
            for cluster_name, cluster_info in cluster_data.items():
                if cluster_name == "default":
                    continue
                if cluster_name not in federation_clients:
                    try:
                        federation_clients[cluster_name] = create_typesense_client(
                            cluster_info
                        )
                        logger.info(
                            f"Initialized Typesense client for cluster: {cluster_name}"
                        )
                    except Exception as e:
                        logger.error(f"Failed to create client for {cluster_name}: {e}")

            logger.info(
                f"Successfully loaded {len(federation_clients)} cluster client(s)"
            )
            metrics.CLUSTER_CONFIG_FETCHES.labels(result="success").inc()
            metrics.LOADED_CLUSTERS.set(len(federation_clients))
            return federation_clients

        except requests.exceptions.RequestException as e:
            metrics.CLUSTER_CONFIG_FETCHES.labels(result="error").inc()
            logger.warning(
                f"Failed to connect to Query API: {e}. Retrying in 10 seconds..."
            )
            time.sleep(10)


def main():
    """Main entry point for the Indexing Service."""
    logger.info("=" * 60)
    logger.info("IMPOSBRO Search - Indexing Service")
    logger.info("Architecture: Smart Producer Pattern")
    logger.info("=" * 60)

    try:
        configure_tracing(
            TelemetryConfig.from_env(
                default_service_name="imposbro-indexing-service",
                default_service_version="4.0.0",
            )
        )
    except TelemetryConfigurationError as exc:
        logger.error("Invalid OpenTelemetry configuration: %s", exc)
        raise SystemExit(1) from exc

    worker_health.WORKER_HEALTH.reset()
    checkpoint_store = None
    try:
        health_server = worker_health.start_health_server()
    except (OSError, RuntimeError) as exc:
        logger.error("Failed to start indexing health server: %s", exc)
        get_telemetry_runtime().shutdown()
        raise SystemExit(1) from exc

    try:
        metrics.start_metrics_server_from_env()
        try:
            allow_legacy = legacy_events_enabled()
        except RuntimeError as exc:
            logger.error("Invalid indexing compatibility configuration: %s", exc)
            raise SystemExit(1) from exc
        if allow_legacy:
            logger.warning(
                "Legacy indexing events are enabled. This mode has no durable "
                "version/tombstone protection and is for development migration only."
            )

        # Get Query API URL from environment
        query_api_url = os.environ.get(
            "INTERNAL_QUERY_API_URL", "http://query_api:8000"
        )

        # Fetch cluster configuration from Query API. Readiness remains false
        # until at least one concrete data-cluster client has been loaded.
        federation_clients = fetch_cluster_configuration(query_api_url)
        if not federation_clients:
            logger.error("No federation clients available. Cannot start consumer.")
            raise SystemExit(1)

        try:
            checkpoint_store = build_checkpoint_store_from_env(federation_clients)
        except CheckpointStoreError as exc:
            logger.error("Indexing checkpoint store is not ready: %s", exc)
            raise SystemExit(1) from exc
        worker_health.WORKER_HEALTH.set_config_loaded(True)

        logger.info(
            "Starting Indexing Service with %s cluster client(s)",
            len(federation_clients),
        )
        logger.info(
            "Consumer will trust Producer's routing decisions (Smart Producer pattern)"
        )

        # Import and run the consumer
        from consumer import run_consumer

        run_consumer(
            federation_clients,
            refresh_clients=lambda: fetch_cluster_configuration(query_api_url),
            health_state=worker_health.WORKER_HEALTH,
            checkpoint_store=checkpoint_store,
            allow_legacy=allow_legacy,
        )
    finally:
        if checkpoint_store is not None:
            checkpoint_store.close()
        worker_health.WORKER_HEALTH.reset()
        worker_health.stop_health_server(health_server)
        get_telemetry_runtime().shutdown()


if __name__ == "__main__":
    main()
