"""
Federation Service for IMPOSBRO Search.

This module manages the federation of multiple Typesense clusters,
including cluster registration, client creation, and document routing.
"""

import logging
import typesense
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class FederationService:
    """
    Manages federated Typesense clusters and document routing.

    The FederationService maintains connections to multiple Typesense clusters
    and provides routing logic to determine which cluster(s) should handle
    specific documents based on configurable rules.

    Attributes:
        clusters_config: Configuration dictionary for all registered clusters
        clients: Dictionary mapping cluster names to Typesense clients
        routing_rules: Dictionary mapping collection names to their routing rules
    """

    def __init__(self):
        """Initialize the FederationService with empty configurations."""
        self.clusters_config: Dict[str, Dict] = {}
        self.clients: Dict[str, typesense.Client] = {}
        self.routing_rules: Dict[str, Dict] = {}

    @staticmethod
    def create_client_config(name: str, nodes_str: str, api_key: str) -> Dict:
        """
        Create a cluster configuration dictionary.

        Args:
            name: Cluster name
            nodes_str: Comma-separated list of node hostnames
            api_key: API key for authentication

        Returns:
            Configuration dictionary
        """
        return {"name": name, "host": nodes_str, "port": 8108, "api_key": api_key}

    @staticmethod
    def create_client(config: Dict) -> typesense.Client:
        """
        Create a Typesense client from a configuration dictionary.

        Args:
            config: Configuration with 'host', 'port', and 'api_key'

        Returns:
            Configured Typesense client
        """
        nodes = [
            {"host": h.strip(), "port": "8108", "protocol": "http"}
            for h in config["host"].split(",")
        ]
        return typesense.Client(
            {
                "nodes": nodes,
                "api_key": config["api_key"],
                "connection_timeout_seconds": 5,
            }
        )

    def register_cluster(self, name: str, host: str, port: int, api_key: str) -> None:
        """
        Register a new cluster with the federation.

        Args:
            name: Unique cluster name
            host: Cluster hostname
            port: Cluster port
            api_key: Cluster API key

        Raises:
            ValueError: If cluster already exists
        """
        if name in self.clients:
            raise ValueError(f"Cluster '{name}' is already registered.")

        config = {
            "nodes": [{"host": host, "port": port, "protocol": "http"}],
            "api_key": api_key,
            "connection_timeout_seconds": 5,
        }
        self.clients[name] = typesense.Client(config)
        self.clusters_config[name] = {
            "name": name,
            "host": host,
            "port": port,
            "api_key": api_key,
        }
        logger.info(f"Cluster '{name}' registered successfully.")

    def unregister_cluster(self, name: str) -> None:
        """
        Remove a cluster from the federation.

        Args:
            name: Cluster name to remove

        Raises:
            ValueError: If cluster doesn't exist or is in use
        """
        if name not in self.clients:
            raise ValueError(f"Cluster '{name}' not found.")

        # Check if cluster is in use
        for collection, rule_config in self.routing_rules.items():
            if rule_config.get("default_cluster") == name:
                raise ValueError(
                    f"Cluster in use as default for collection '{collection}'."
                )
            for rule in rule_config.get("rules", []):
                if rule.get("cluster") == name:
                    raise ValueError(
                        f"Cluster in use by a rule for collection '{collection}'."
                    )

        del self.clients[name]
        del self.clusters_config[name]
        logger.info(f"Cluster '{name}' unregistered.")

    def get_client_for_document(
        self, collection_name: str, document: Dict
    ) -> Tuple[Optional[typesense.Client], str]:
        """
        Determine the target cluster for a document based on routing rules.

        Args:
            collection_name: Name of the collection
            document: Document to route

        Returns:
            Tuple of (Typesense client, cluster name)
        """
        collection_rules = self.routing_rules.get(collection_name)

        if not collection_rules or not collection_rules.get("rules"):
            return self.clients.get("default"), "default"

        for rule in collection_rules["rules"]:
            doc_value = document.get(rule["field"])
            if doc_value is not None and str(doc_value) == rule["value"]:
                target_name = rule["cluster"]
                client = self.clients.get(target_name)
                if client:
                    return client, target_name
                logger.warning(f"Target cluster '{target_name}' from rule not found.")
                return None, target_name

        default_name = collection_rules.get("default_cluster", "default")
        return self.clients.get(default_name), default_name

    def get_clients_for_search(self, collection_name: str) -> List[typesense.Client]:
        """
        Get all clients that might contain documents for a collection.

        For sharded collections, returns clients for all clusters mentioned
        in the routing rules. For non-sharded collections, returns all clients.

        Args:
            collection_name: Name of the collection to search

        Returns:
            List of Typesense clients to query
        """
        rules = self.routing_rules.get(collection_name, {})
        if rules.get("rules"):
            cluster_names = {r["cluster"] for r in rules.get("rules", [])}
            cluster_names.add(rules.get("default_cluster", "default"))
            return [
                self.clients[name] for name in cluster_names if name in self.clients
            ]

        return list(self.clients.values())

    def set_routing_rules(
        self, collection: str, rules: List[Dict], default_cluster: str
    ) -> None:
        """
        Set routing rules for a collection.

        Args:
            collection: Collection name
            rules: List of routing rules
            default_cluster: Default cluster when no rules match
        """
        # Validate all clusters exist
        all_clusters = {r["cluster"] for r in rules}
        all_clusters.add(default_cluster)

        missing = all_clusters - set(self.clients.keys())
        if missing:
            raise ValueError(f"Clusters not registered: {missing}")

        self.routing_rules[collection] = {
            "collection": collection,
            "rules": rules,
            "default_cluster": default_cluster,
        }
        logger.info(f"Routing rules for '{collection}' updated.")

    def delete_routing_rules(self, collection: str) -> None:
        """Reset routing rules for a collection to defaults."""
        self.routing_rules[collection] = {"rules": [], "default_cluster": "default"}
        logger.info(f"Routing rules for '{collection}' reset to defaults.")

    def load_from_state(
        self, clusters_config: Dict[str, Dict], routing_rules: Dict[str, Dict]
    ) -> None:
        """
        Load federation configuration from saved state.

        Args:
            clusters_config: Saved cluster configurations
            routing_rules: Saved routing rules
        """
        self.clusters_config.update(clusters_config)
        self.routing_rules.update(routing_rules)

        for name, config in clusters_config.items():
            nodes = [
                {"host": h.strip(), "port": config["port"], "protocol": "http"}
                for h in config["host"].split(",")
            ]
            self.clients[name] = typesense.Client(
                {
                    "nodes": nodes,
                    "api_key": config["api_key"],
                    "connection_timeout_seconds": 5,
                }
            )

        logger.info(f"Loaded {len(clusters_config)} federated cluster(s) from state.")

    def reload_from_state(
        self, clusters_config: Dict[str, Dict], routing_rules: Dict[str, Dict]
    ) -> None:
        """
        Reload federation configuration from saved state.

        Unlike load_from_state, this clears existing config before loading,
        ensuring a complete refresh for config synchronization.

        Args:
            clusters_config: Fresh cluster configurations
            routing_rules: Fresh routing rules
        """
        # Clear existing state
        self.clusters_config.clear()
        self.clients.clear()
        self.routing_rules.clear()

        # Load fresh state
        self.load_from_state(clusters_config, routing_rules)
        logger.info("Configuration reloaded from state store.")
