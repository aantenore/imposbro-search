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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


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

    # Handle comma-separated hosts for HA clusters
    nodes = [
        {"host": h.strip(), "port": port, "protocol": "http"} for h in host.split(",")
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
            logger.info(
                f"Fetching cluster config from {query_api_url}/admin/federation/clusters..."
            )
            cluster_response = requests.get(
                f"{query_api_url}/admin/federation/clusters", timeout=10
            )
            cluster_response.raise_for_status()
            cluster_data = cluster_response.json()

            if not cluster_data:
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
            return federation_clients

        except requests.exceptions.RequestException as e:
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

    # Get Query API URL from environment
    query_api_url = os.environ.get("INTERNAL_QUERY_API_URL", "http://query_api:8000")

    # Fetch cluster configuration from Query API
    # Note: No routing rules needed - Smart Producer pattern means
    # the Producer (Query API) determines routing
    federation_clients = fetch_cluster_configuration(query_api_url)

    if not federation_clients:
        logger.error("No federation clients available. Cannot start consumer.")
        sys.exit(1)

    logger.info(
        f"Starting Indexing Service with {len(federation_clients)} cluster client(s)"
    )
    logger.info(
        "Consumer will trust Producer's routing decisions (Smart Producer pattern)"
    )

    # Import and run the consumer
    from consumer import run_consumer

    run_consumer(federation_clients)


if __name__ == "__main__":
    main()
