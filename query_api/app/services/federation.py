"""
Federation Service for IMPOSBRO Search.

This module manages the federation of multiple Typesense clusters,
including cluster registration, client creation, and document routing.
"""

import copy
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
        self.collection_schemas: Dict[str, Dict] = {}
        self.collection_aliases: Dict[str, Dict[str, Dict[str, str]]] = {}

    @staticmethod
    def split_hosts(hosts: str) -> List[str]:
        """Return non-empty host entries from a comma-separated Typesense node list."""
        return [host.strip() for host in hosts.split(",") if host.strip()]

    @staticmethod
    def create_single_node_client(host: str, port: str, api_key: str) -> typesense.Client:
        """Create a Typesense client pinned to one node for readiness checks."""
        return typesense.Client(
            {
                "nodes": [{"host": host, "port": str(port), "protocol": "http"}],
                "api_key": api_key,
                "connection_timeout_seconds": 5,
            }
        )

    @staticmethod
    def node_statuses(hosts: str, port: str, api_key: str) -> List[Dict[str, str]]:
        """Check every declared Typesense node by listing collections."""
        statuses: List[Dict[str, str]] = []
        for host in FederationService.split_hosts(hosts):
            try:
                client = FederationService.create_single_node_client(host, port, api_key)
                client.collections.retrieve()
                statuses.append({"host": host, "status": "ok"})
            except Exception as exc:
                statuses.append(
                    {
                        "host": host,
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return statuses

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
        port = str(config.get("port", 8108))
        nodes = [
            {"host": h, "port": port, "protocol": "http"}
            for h in FederationService.split_hosts(config["host"])
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

        statuses = self.node_statuses(host, str(port), api_key)
        failed = [node for node in statuses if node.get("status") != "ok"]
        if not statuses or failed:
            raise ValueError(
                f"Cluster '{name}' is not reachable on all configured node(s): {failed or statuses}"
            )

        config = {
            "name": name,
            "host": host,
            "port": port,
            "api_key": api_key,
        }
        self.clients[name] = self.create_client(config)
        self.clusters_config[name] = config
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
                if name in rule.get("clusters", []):
                    raise ValueError(
                        f"Cluster in use by a rule for collection '{collection}'."
                    )

        del self.clients[name]
        del self.clusters_config[name]
        self.collection_aliases.pop(name, None)
        logger.info(f"Cluster '{name}' unregistered.")

    def backfill_collection_schemas(self, cluster_name: str) -> List[str]:
        """
        Ensure a newly registered cluster has every desired collection schema.

        Existing collections are treated as already reconciled. The method returns
        only collection names that were actually created on the target cluster.
        """
        client = self.clients.get(cluster_name)
        if not client:
            raise ValueError(f"Cluster '{cluster_name}' not found.")

        created: List[str] = []
        for collection_name, schema in self.collection_schemas.items():
            try:
                client.collections.create(schema)
                created.append(collection_name)
                logger.info(
                    "Backfilled collection '%s' on cluster '%s'.",
                    collection_name,
                    cluster_name,
                )
            except typesense.exceptions.ObjectAlreadyExists:
                logger.debug(
                    "Collection '%s' already exists on cluster '%s'.",
                    collection_name,
                    cluster_name,
                )
        return created

    def cluster_node_statuses(self, cluster_name: str) -> List[Dict[str, str]]:
        """Return readiness status for every node configured in a registered cluster."""
        config = self.clusters_config.get(cluster_name)
        if not config:
            raise ValueError(f"Cluster '{cluster_name}' not found.")
        return self.node_statuses(
            config["host"],
            str(config.get("port", 8108)),
            config["api_key"],
        )

    def reconcile_collection_schemas(self) -> Dict[str, Dict[str, List[str]]]:
        """
        Reconcile desired collection schemas across all registered clusters.

        Returns a per-cluster report with existing and created collections. Any
        unexpected Typesense error is propagated so operators do not mistake a
        partial reconcile for a successful one.
        """
        report: Dict[str, Dict[str, List[str]]] = {}
        for cluster_name, client in self.clients.items():
            cluster_report = {"existing": [], "created": []}
            for collection_name, schema in self.collection_schemas.items():
                try:
                    client.collections[collection_name].retrieve()
                    cluster_report["existing"].append(collection_name)
                except typesense.exceptions.ObjectNotFound:
                    try:
                        client.collections.create(schema)
                        cluster_report["created"].append(collection_name)
                        logger.info(
                            "Reconciled missing collection '%s' on cluster '%s'.",
                            collection_name,
                            cluster_name,
                        )
                    except typesense.exceptions.ObjectAlreadyExists:
                        cluster_report["existing"].append(collection_name)
            report[cluster_name] = cluster_report
        return report

    def _resolve_default_cluster(self) -> Optional[str]:
        """
        Resolve the virtual 'default' cluster to an actual registered cluster.
        The admin API exposes 'default' as the internal HA cluster; for routing
        we need a real data cluster (e.g. default-data-cluster).
        """
        if "default" in self.clients:
            return "default"
        if "default-data-cluster" in self.clients:
            return "default-data-cluster"
        if self.clients:
            return next(iter(self.clients.keys()))
        return None

    def get_client_for_cluster(self, cluster_name: str) -> Optional[typesense.Client]:
        """Return the Typesense client for a cluster (resolves 'default' to a real cluster)."""
        name = self.resolve_cluster_name(cluster_name)
        return self.clients.get(name) if name else None

    def resolve_cluster_name(self, cluster_name: str) -> Optional[str]:
        """Return the real registered cluster name for a public cluster identifier."""
        if cluster_name == "default":
            return self._resolve_default_cluster()
        return cluster_name if cluster_name in self.clients else None

    def get_client_for_document(
        self, collection_name: str, document: Dict
    ) -> Tuple[Optional[typesense.Client], Optional[str]]:
        """
        Determine the target cluster for a document based on routing rules.

        Args:
            collection_name: Name of the collection
            document: Document to route

        Returns:
            Tuple of (Typesense client, cluster name). Cluster name may be None
            if no suitable cluster is available.
        """
        collection_rules = self.routing_rules.get(collection_name)

        if not collection_rules or not collection_rules.get("rules"):
            default_name = self._resolve_default_cluster()
            if default_name is None:
                return None, None
            return self.clients.get(default_name), default_name

        for rule in collection_rules["rules"]:
            doc_value = document.get(rule["field"])
            if doc_value is not None and str(doc_value) == rule["value"]:
                # Fan-out: rule may have "clusters" (list) or "cluster" (single)
                target_names = rule.get("clusters")
                if target_names is None:
                    target_names = [rule["cluster"]]
                targets: List[Tuple[Optional[typesense.Client], Optional[str]]] = []
                for target_name in target_names:
                    client = self.clients.get(target_name)
                    if client:
                        targets.append((client, target_name))
                    else:
                        logger.warning(f"Target cluster '{target_name}' from rule not found.")
                if targets:
                    # Return first for backward compat; use get_targets_for_document for full list
                    return targets[0][0], targets[0][1]
                return None, target_names[0] if target_names else None

        default_name = collection_rules.get("default_cluster", "default")
        if default_name == "default":
            default_name = self._resolve_default_cluster()
        if default_name is None:
            return None, None
        return self.clients.get(default_name), default_name

    def get_targets_for_document(
        self, collection_name: str, document: Dict
    ) -> List[Tuple[typesense.Client, str]]:
        """
        Return all (client, cluster_name) pairs for a document (single or fan-out).
        Use this for ingest when replicating to multiple clusters.
        """
        collection_rules = self.routing_rules.get(collection_name)

        if not collection_rules or not collection_rules.get("rules"):
            default_name = self._resolve_default_cluster()
            if default_name is None:
                return []
            client = self.clients.get(default_name)
            return [(client, default_name)] if client else []

        for rule in collection_rules["rules"]:
            doc_value = document.get(rule["field"])
            if doc_value is not None and str(doc_value) == rule["value"]:
                target_names = rule.get("clusters") or [rule["cluster"]]
                result: List[Tuple[typesense.Client, str]] = []
                for name in target_names:
                    client = self.clients.get(name)
                    if client:
                        result.append((client, name))
                return result

        default_name = collection_rules.get("default_cluster", "default")
        if default_name == "default":
            default_name = self._resolve_default_cluster()
        if default_name is None:
            return []
        client = self.clients.get(default_name)
        return [(client, default_name)] if client else []

    def _cluster_names_for_search(self, collection_name: str) -> List[str]:
        """Return cluster names that may contain documents for a collection."""
        rules = self.routing_rules.get(collection_name, {})
        if not rules.get("rules"):
            return list(self.clients.keys())

        cluster_names: List[str] = []

        def add_cluster(name: str) -> None:
            if name == "default":
                name = self._resolve_default_cluster() or ""
            if name and name in self.clients and name not in cluster_names:
                cluster_names.append(name)

        for rule in rules.get("rules", []):
            if "clusters" in rule:
                for name in rule["clusters"]:
                    add_cluster(name)
            else:
                add_cluster(rule["cluster"])
        add_cluster(rules.get("default_cluster", "default"))
        return cluster_names

    def get_named_clients_for_search(
        self, collection_name: str
    ) -> List[Tuple[str, typesense.Client]]:
        """Get named clients that may contain documents for a collection."""
        return [
            (name, self.clients[name])
            for name in self._cluster_names_for_search(collection_name)
        ]

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
        return [client for _, client in self.get_named_clients_for_search(collection_name)]

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
        # Validate all clusters exist (resolve virtual "default" for validation)
        all_clusters = set()
        for r in rules:
            if "clusters" in r:
                all_clusters.update(r["clusters"])
            else:
                all_clusters.add(r["cluster"])
        effective_default = (
            self._resolve_default_cluster()
            if default_cluster == "default"
            else default_cluster
        )
        if effective_default:
            all_clusters.add(effective_default)

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
        self,
        clusters_config: Dict[str, Dict],
        routing_rules: Dict[str, Dict],
        collection_schemas: Optional[Dict[str, Dict]] = None,
        collection_aliases: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None,
    ) -> None:
        """
        Load federation configuration from saved state.

        Args:
            clusters_config: Saved cluster configurations
            routing_rules: Saved routing rules
            collection_schemas: Desired collection schemas to reconcile across clusters
            collection_aliases: Desired collection aliases keyed by cluster name
        """
        self.clusters_config.update(clusters_config)
        self.routing_rules.update(routing_rules)
        self.collection_schemas.update(collection_schemas or {})
        self.collection_aliases.update(copy.deepcopy(collection_aliases or {}))

        for name, config in clusters_config.items():
            port = config.get("port", 8108)
            if isinstance(port, int):
                port = str(port)
            nodes = [
                {"host": h, "port": port, "protocol": "http"}
                for h in self.split_hosts(config["host"])
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
        self,
        clusters_config: Dict[str, Dict],
        routing_rules: Dict[str, Dict],
        collection_schemas: Optional[Dict[str, Dict]] = None,
        collection_aliases: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None,
    ) -> None:
        """
        Reload federation configuration from saved state.

        Unlike load_from_state, this clears existing config before loading,
        ensuring a complete refresh for config synchronization.

        Args:
            clusters_config: Fresh cluster configurations
            routing_rules: Fresh routing rules
            collection_schemas: Fresh desired collection schemas
            collection_aliases: Fresh desired collection aliases
        """
        # Clear existing state
        self.clusters_config.clear()
        self.clients.clear()
        self.routing_rules.clear()
        self.collection_schemas.clear()
        self.collection_aliases.clear()

        # Load fresh state
        self.load_from_state(
            clusters_config,
            routing_rules,
            collection_schemas,
            collection_aliases,
        )
        logger.info("Configuration reloaded from state store.")
