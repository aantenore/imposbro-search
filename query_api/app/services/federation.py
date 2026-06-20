"""
Federation Service for IMPOSBRO Search.

This module manages the federation of multiple Typesense clusters,
including cluster registration, client creation, and document routing.
"""

import copy
import fnmatch
import logging
import typesense
from typing import Any, Dict, List, Optional, Tuple

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

    @staticmethod
    def _document_value(document: Dict[str, Any], path: str) -> Any:
        value: Any = document
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        return value

    @staticmethod
    def _rule_targets(rule: Dict[str, Any]) -> List[str]:
        targets = rule.get("clusters")
        if targets is None:
            targets = [rule.get("cluster")]
        return [str(target) for target in targets if target]

    @staticmethod
    def _ordered_rules_with_index(
        rules: List[Dict[str, Any]]
    ) -> List[Tuple[int, Dict[str, Any]]]:
        return sorted(
            enumerate(rules),
            key=lambda item: (
                item[1].get("priority")
                if item[1].get("priority") is not None
                else item[0],
                item[0],
            ),
        )

    @staticmethod
    def _ordered_rules(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [rule for _, rule in FederationService._ordered_rules_with_index(rules)]

    @staticmethod
    def _numeric_value(value: Any) -> Optional[float]:
        if isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _rule_matches(self, rule: Dict[str, Any], document: Dict[str, Any]) -> bool:
        doc_value = self._document_value(document, str(rule.get("field", "")))
        if doc_value is None:
            return False

        operator = str(rule.get("operator") or "equals").strip().lower()
        if operator == "equals":
            expected = rule.get("value")
            if expected is None:
                values = rule.get("values") or []
                expected = values[0] if values else None
            return expected is not None and str(doc_value) == str(expected)
        if operator == "in":
            values = rule.get("values") or []
            return str(doc_value) in {str(value) for value in values}
        if operator == "glob":
            pattern = rule.get("pattern") or rule.get("value")
            return bool(pattern) and fnmatch.fnmatchcase(str(doc_value), str(pattern))
        if operator == "range":
            numeric = self._numeric_value(doc_value)
            if numeric is None:
                return False
            min_value = self._numeric_value(rule.get("min"))
            max_value = self._numeric_value(rule.get("max"))
            if min_value is not None and numeric < min_value:
                return False
            if max_value is not None and numeric > max_value:
                return False
            return min_value is not None or max_value is not None
        return False

    def _targets_for_names(
        self,
        target_names: List[str],
    ) -> List[Tuple[typesense.Client, str]]:
        result: List[Tuple[typesense.Client, str]] = []
        for target_name in target_names:
            resolved_name = self.resolve_cluster_name(target_name)
            client = self.clients.get(resolved_name) if resolved_name else None
            if client:
                result.append((client, resolved_name))
            else:
                logger.warning("Target cluster '%s' from rule not found.", target_name)
        return result

    def _default_targets(
        self,
        default_cluster: str = "default",
    ) -> List[Tuple[typesense.Client, str]]:
        default_name = self.resolve_cluster_name(default_cluster)
        if default_name is None:
            return []
        client = self.clients.get(default_name)
        return [(client, default_name)] if client else []

    def preview_routing(
        self,
        collection_name: str,
        document: Dict[str, Any],
        *,
        rules_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        collection_rules = rules_config or self.routing_rules.get(collection_name, {})
        rules = collection_rules.get("rules", [])
        for index, rule in self._ordered_rules_with_index(rules):
            if self._rule_matches(rule, document):
                target_names = self._rule_targets(rule)
                targets = self._targets_for_names(target_names)
                return {
                    "collection": collection_name,
                    "matched": True,
                    "matched_rule": copy.deepcopy(rule),
                    "matched_rule_index": index,
                    "used_default": False,
                    "routed_to": [name for _, name in targets],
                    "target_clusters": target_names,
                }

        default_cluster = collection_rules.get("default_cluster", "default")
        targets = self._default_targets(default_cluster)
        return {
            "collection": collection_name,
            "matched": False,
            "matched_rule": None,
            "matched_rule_index": None,
            "used_default": True,
            "routed_to": [name for _, name in targets],
            "target_clusters": [default_cluster],
        }

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
        targets = self.get_targets_for_document(collection_name, document)
        if targets:
            return targets[0]
        preview = self.preview_routing(collection_name, document)
        unresolved = preview.get("target_clusters") or []
        return None, unresolved[0] if unresolved else None

    def get_targets_for_document(
        self, collection_name: str, document: Dict
    ) -> List[Tuple[typesense.Client, str]]:
        """
        Return all (client, cluster_name) pairs for a document (single or fan-out).
        Use this for ingest when replicating to multiple clusters.
        """
        preview = self.preview_routing(collection_name, document)
        target_names = preview.get("target_clusters") or []
        if preview.get("used_default"):
            return self._default_targets(target_names[0] if target_names else "default")
        return self._targets_for_names(target_names)

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
            for name in self._rule_targets(rule):
                add_cluster(name)
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

    def get_named_clients_for_delete(
        self, collection_name: str
    ) -> List[Tuple[str, typesense.Client]]:
        """
        Get named clients that may contain historical copies for deletion.

        Deletes are idempotent, so fan out to every registered data cluster. This
        avoids orphaning documents after routing rules move a collection away
        from a cluster that previously received writes.
        """
        return list(self.clients.items())

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
            for target_name in self._rule_targets(r):
                resolved_name = self.resolve_cluster_name(target_name)
                all_clusters.add(resolved_name or target_name)
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
