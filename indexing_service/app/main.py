"""
IMPOSBRO Search - Indexing Service Entry Point

This service consumes document ingestion messages from Kafka and indexes
them into the appropriate federated Typesense clusters.

The service fetches its configuration (cluster connections and routing rules)
from the Query API at startup, ensuring consistency across the system.
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


def fetch_configuration(query_api_url: str) -> tuple:
    """
    Fetch cluster and routing configuration from the Query API.

    Args:
        query_api_url: Base URL of the Query API

    Returns:
        Tuple of (federation_clients dict, routing_rules dict)
    """
    federation_clients: Dict[str, typesense.Client] = {}
    collection_routing_rules: Dict = {}

    while True:
        try:
            # Fetch cluster configuration
            logger.info(
                f"Fetching cluster config from {query_api_url}/admin/federation/clusters..."
            )
            cluster_response = requests.get(
                f"{query_api_url}/admin/federation/clusters", timeout=10
            )
            cluster_response.raise_for_status()
            cluster_data = cluster_response.json()

            # Fetch routing rules
            logger.info(
                f"Fetching routing rules from {query_api_url}/admin/routing-map..."
            )
            rules_response = requests.get(
                f"{query_api_url}/admin/routing-map", timeout=10
            )
            rules_response.raise_for_status()
            rules_data = rules_response.json()

            if not cluster_data:
                logger.warning(
                    "Query API returned no clusters. Retrying in 10 seconds..."
                )
                time.sleep(10)
                continue

            # Create Typesense clients for each cluster
            for cluster_name, cluster_info in cluster_data.items():
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

            # Store routing rules
            collection_routing_rules = rules_data.get("collections", {})
            logger.info(f"Loaded {len(collection_routing_rules)} routing rule(s).")

            return federation_clients, collection_routing_rules

        except requests.exceptions.RequestException as e:
            logger.warning(
                f"Failed to connect to Query API: {e}. Retrying in 10 seconds..."
            )
            time.sleep(10)


def main():
    """Main entry point for the Indexing Service."""
    logger.info("=" * 60)
    logger.info("IMPOSBRO Search - Indexing Service")
    logger.info("=" * 60)

    # Get Query API URL from environment
    query_api_url = os.environ.get("INTERNAL_QUERY_API_URL", "http://query_api:8000")

    # Fetch configuration from Query API
    federation_clients, routing_rules = fetch_configuration(query_api_url)

    if not federation_clients:
        logger.error("No federation clients available. Cannot start consumer.")
        sys.exit(1)

    logger.info(
        f"Starting Indexing Service with {len(federation_clients)} federated client(s)..."
    )

    # Import and run the consumer
    from consumer import run_consumer

    run_consumer(federation_clients, routing_rules)


if __name__ == "__main__":
    main()
