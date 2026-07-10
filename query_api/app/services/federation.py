"""
Federation Service for IMPOSBRO Search.

This module manages the federation of multiple Typesense clusters,
including cluster registration, client creation, and document routing.
"""

import copy
import fnmatch
import logging
import typesense
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from domain.routing_rollout import RoutingRollout
from secret_resolver import (
    SecretResolver,
    build_secret_resolver,
    materialize_cluster_secret,
    validate_cluster_secret_config,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FederationRuntimeSnapshot:
    """One atomically replaceable view used by concurrent data-plane requests."""

    revision: int
    clusters_config: Dict[str, Dict]
    clients: Dict[str, typesense.Client]
    routing_rules: Dict[str, Dict]
    collection_schemas: Dict[str, Dict]
    collection_aliases: Dict[str, Dict[str, Dict[str, str]]]
    routing_rollouts: Dict[str, Dict[str, Any]]


class _RotatingTypesenseClient:
    """Resolve a referenced API key for every top-level Typesense operation."""

    def __init__(self, factory: Callable[[], typesense.Client]):
        self._factory = factory

    def __getattr__(self, name: str) -> Any:
        return getattr(self._factory(), name)


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

    def __init__(
        self,
        *,
        secret_resolver: Optional[SecretResolver] = None,
        allow_inline_secrets: bool = True,
    ):
        """Initialize the FederationService with empty configurations."""
        self.secret_resolver = secret_resolver or build_secret_resolver(
            "/run/secrets/imposbro"
        )
        self.allow_inline_secrets = allow_inline_secrets
        self._runtime = FederationRuntimeSnapshot(
            revision=0,
            clusters_config={},
            clients={},
            routing_rules={},
            collection_schemas={},
            collection_aliases={},
            routing_rollouts={},
        )

    @property
    def applied_revision(self) -> int:
        return self._runtime.revision

    @property
    def clusters_config(self) -> Dict[str, Dict]:
        return self._runtime.clusters_config

    @clusters_config.setter
    def clusters_config(self, value: Dict[str, Dict]) -> None:
        self._runtime = replace(self._runtime, clusters_config=value)

    @property
    def clients(self) -> Dict[str, typesense.Client]:
        return self._runtime.clients

    @clients.setter
    def clients(self, value: Dict[str, typesense.Client]) -> None:
        self._runtime = replace(self._runtime, clients=value)

    @property
    def routing_rules(self) -> Dict[str, Dict]:
        return self._runtime.routing_rules

    @routing_rules.setter
    def routing_rules(self, value: Dict[str, Dict]) -> None:
        self._runtime = replace(self._runtime, routing_rules=value)

    @property
    def collection_schemas(self) -> Dict[str, Dict]:
        return self._runtime.collection_schemas

    @collection_schemas.setter
    def collection_schemas(self, value: Dict[str, Dict]) -> None:
        self._runtime = replace(self._runtime, collection_schemas=value)

    @property
    def collection_aliases(self) -> Dict[str, Dict[str, Dict[str, str]]]:
        return self._runtime.collection_aliases

    @collection_aliases.setter
    def collection_aliases(self, value: Dict[str, Dict[str, Dict[str, str]]]) -> None:
        self._runtime = replace(self._runtime, collection_aliases=value)

    @property
    def routing_rollouts(self) -> Dict[str, Dict[str, Any]]:
        return self._runtime.routing_rollouts

    @routing_rollouts.setter
    def routing_rollouts(self, value: Dict[str, Dict[str, Any]]) -> None:
        self._runtime = replace(self._runtime, routing_rollouts=value)

    def runtime_snapshot(self) -> FederationRuntimeSnapshot:
        """Return the current snapshot so one request can keep a coherent view."""
        return self._runtime

    def replace_runtime(self, **changes: Any) -> None:
        """Atomically replace selected immutable runtime snapshot fields."""
        self._runtime = replace(self._runtime, **changes)

    def mark_applied_revision(self, revision: int) -> None:
        if revision < self._runtime.revision:
            raise ValueError("Federation applied revision cannot move backwards")
        self._runtime = replace(self._runtime, revision=revision)

    @staticmethod
    def split_hosts(hosts: str) -> List[str]:
        """Return non-empty host entries from a comma-separated Typesense node list."""
        return [host.strip() for host in hosts.split(",") if host.strip()]

    @staticmethod
    def normalize_protocol(protocol: Any = None) -> str:
        """Return a supported Typesense transport, defaulting legacy state to HTTP."""
        normalized = str(protocol or "http").strip().lower()
        if normalized not in {"http", "https"}:
            raise ValueError("Typesense protocol must be 'http' or 'https'.")
        return normalized

    @classmethod
    def normalize_cluster_config(
        cls,
        config: Dict[str, Any],
        *,
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Copy cluster config and add defaults required by current clients/state."""
        normalized = dict(config)
        if name and not normalized.get("name"):
            normalized["name"] = name
        normalized["protocol"] = cls.normalize_protocol(normalized.get("protocol"))
        return normalized

    @staticmethod
    def create_single_node_client(
        host: str,
        port: str,
        api_key: str,
        protocol: str = "http",
        connection_timeout_seconds: float = 5,
    ) -> typesense.Client:
        """Create a Typesense client pinned to one node for readiness checks."""
        protocol = FederationService.normalize_protocol(protocol)
        return typesense.Client(
            {
                "nodes": [{"host": host, "port": str(port), "protocol": protocol}],
                "api_key": api_key,
                "connection_timeout_seconds": connection_timeout_seconds,
            }
        )

    @staticmethod
    def node_statuses(
        hosts: str,
        port: str,
        api_key: str,
        protocol: str = "http",
        connection_timeout_seconds: float = 5,
    ) -> List[Dict[str, str]]:
        """Check every declared Typesense node concurrently by listing collections."""
        protocol = FederationService.normalize_protocol(protocol)
        declared_hosts = FederationService.split_hosts(hosts)

        def check_node(host: str) -> Dict[str, str]:
            try:
                client = FederationService.create_single_node_client(
                    host,
                    port,
                    api_key,
                    protocol,
                    connection_timeout_seconds,
                )
                client.collections.retrieve()
                return {"host": host, "status": "ok"}
            except Exception as exc:
                return {
                    "host": host,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }

        if not declared_hosts:
            return []
        worker_count = min(len(declared_hosts), 8)
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="typesense-health",
        ) as executor:
            return list(executor.map(check_node, declared_hosts))

    @staticmethod
    def create_client_config(
        name: str,
        nodes_str: str,
        api_key: Optional[str] = None,
        protocol: str = "http",
        *,
        api_key_ref: Optional[str] = None,
        allow_inline_secrets: bool = True,
    ) -> Dict:
        """
        Create a cluster configuration dictionary.

        Args:
            name: Cluster name
            nodes_str: Comma-separated list of node hostnames
            api_key: API key for authentication

        Returns:
            Configuration dictionary
        """
        config = {
            "name": name,
            "host": nodes_str,
            "port": 8108,
            "protocol": FederationService.normalize_protocol(protocol),
        }
        if api_key_ref:
            config["api_key_ref"] = api_key_ref
        if api_key:
            config["api_key"] = api_key
        validate_cluster_secret_config(
            config,
            allow_inline=allow_inline_secrets,
        )
        return config

    @staticmethod
    def create_client(
        config: Dict,
        *,
        secret_resolver: Optional[SecretResolver] = None,
        allow_inline_secrets: bool = True,
    ) -> typesense.Client:
        """
        Create a Typesense client from a configuration dictionary.

        Args:
            config: Configuration with 'host', 'port', and 'api_key'

        Returns:
            Configured Typesense client
        """
        normalized = FederationService.normalize_cluster_config(config)
        resolver = secret_resolver or build_secret_resolver("/run/secrets/imposbro")

        def factory() -> typesense.Client:
            api_key = materialize_cluster_secret(
                normalized,
                allow_inline=allow_inline_secrets,
                resolver=resolver,
            )
            port = str(normalized.get("port", 8108))
            nodes = [
                {"host": h, "port": port, "protocol": normalized["protocol"]}
                for h in FederationService.split_hosts(normalized["host"])
            ]
            return typesense.Client(
                {
                    "nodes": nodes,
                    "api_key": api_key,
                    "connection_timeout_seconds": 5,
                }
            )

        # Validate and resolve once so invalid startup/import state fails early.
        initial_client = factory()
        if normalized.get("api_key_ref"):
            return _RotatingTypesenseClient(factory)  # type: ignore[return-value]
        return initial_client

    def build_client(self, config: Dict) -> typesense.Client:
        """Build a policy-aware client for one persisted cluster config."""
        return self.create_client(
            config,
            secret_resolver=self.secret_resolver,
            allow_inline_secrets=self.allow_inline_secrets,
        )

    def materialize_cluster_config(self, config: Dict) -> Dict[str, Any]:
        """Return an ephemeral worker config with a fresh raw API key."""
        normalized = self.normalize_cluster_config(config)
        api_key = materialize_cluster_secret(
            normalized,
            allow_inline=self.allow_inline_secrets,
            resolver=self.secret_resolver,
        )
        normalized.pop("api_key_ref", None)
        normalized["api_key"] = api_key
        return normalized

    def register_cluster(
        self,
        name: str,
        host: str,
        port: int,
        api_key: Optional[str] = None,
        protocol: str = "http",
        *,
        api_key_ref: Optional[str] = None,
    ) -> None:
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

        protocol = self.normalize_protocol(protocol)
        config: Dict[str, Any] = {
            "name": name,
            "host": host,
            "port": port,
            "protocol": protocol,
        }
        if api_key_ref:
            config["api_key_ref"] = api_key_ref
        if api_key:
            config["api_key"] = api_key
        resolved_api_key = materialize_cluster_secret(
            config,
            allow_inline=self.allow_inline_secrets,
            resolver=self.secret_resolver,
        )
        statuses = self.node_statuses(host, str(port), resolved_api_key, protocol)
        failed = [node for node in statuses if node.get("status") != "ok"]
        if not statuses or failed:
            raise ValueError(
                f"Cluster '{name}' is not reachable on all configured node(s): {failed or statuses}"
            )

        self.clients[name] = self.build_client(config)
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

    def cluster_node_statuses(
        self,
        cluster_name: str,
        connection_timeout_seconds: float = 5,
    ) -> List[Dict[str, str]]:
        """Return readiness status for every node configured in a registered cluster."""
        config = self.clusters_config.get(cluster_name)
        if not config:
            raise ValueError(f"Cluster '{cluster_name}' not found.")
        return self.node_statuses(
            config["host"],
            str(config.get("port", 8108)),
            materialize_cluster_secret(
                config,
                allow_inline=self.allow_inline_secrets,
                resolver=self.secret_resolver,
            ),
            config.get("protocol", "http"),
            connection_timeout_seconds,
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

    def _resolve_default_cluster(
        self,
        runtime: Optional[FederationRuntimeSnapshot] = None,
    ) -> Optional[str]:
        """
        Resolve the virtual 'default' cluster to an actual registered cluster.
        The admin API exposes 'default' as the internal HA cluster; for routing
        we need a real data cluster (e.g. default-data-cluster).
        """
        clients = (runtime or self._runtime).clients
        if "default" in clients:
            return "default"
        if "default-data-cluster" in clients:
            return "default-data-cluster"
        if clients:
            return next(iter(clients.keys()))
        return None

    def get_client_for_cluster(self, cluster_name: str) -> Optional[typesense.Client]:
        """Return the Typesense client for a cluster (resolves 'default' to a real cluster)."""
        name = self.resolve_cluster_name(cluster_name)
        return self.clients.get(name) if name else None

    def resolve_cluster_name(
        self,
        cluster_name: str,
        runtime: Optional[FederationRuntimeSnapshot] = None,
    ) -> Optional[str]:
        """Return the real registered cluster name for a public cluster identifier."""
        runtime = runtime or self._runtime
        if cluster_name == "default":
            return self._resolve_default_cluster(runtime)
        return cluster_name if cluster_name in runtime.clients else None

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
        runtime: Optional[FederationRuntimeSnapshot] = None,
    ) -> List[Tuple[typesense.Client, str]]:
        runtime = runtime or self._runtime
        result: List[Tuple[typesense.Client, str]] = []
        for target_name in target_names:
            resolved_name = self.resolve_cluster_name(target_name, runtime)
            client = runtime.clients.get(resolved_name) if resolved_name else None
            if client:
                result.append((client, resolved_name))
            else:
                logger.warning("Target cluster '%s' from rule not found.", target_name)
        return result

    def _default_targets(
        self,
        default_cluster: str = "default",
        runtime: Optional[FederationRuntimeSnapshot] = None,
    ) -> List[Tuple[typesense.Client, str]]:
        runtime = runtime or self._runtime
        default_name = self.resolve_cluster_name(default_cluster, runtime)
        if default_name is None:
            return []
        client = runtime.clients.get(default_name)
        return [(client, default_name)] if client else []

    def preview_routing(
        self,
        collection_name: str,
        document: Dict[str, Any],
        *,
        rules_config: Optional[Dict[str, Any]] = None,
        runtime: Optional[FederationRuntimeSnapshot] = None,
    ) -> Dict[str, Any]:
        runtime = runtime or self._runtime
        collection_rules = rules_config or runtime.routing_rules.get(collection_name, {})
        rules = collection_rules.get("rules", [])
        for index, rule in self._ordered_rules_with_index(rules):
            if self._rule_matches(rule, document):
                target_names = self._rule_targets(rule)
                targets = self._targets_for_names(target_names, runtime)
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
        targets = self._default_targets(default_cluster, runtime)
        return {
            "collection": collection_name,
            "matched": False,
            "matched_rule": None,
            "matched_rule_index": None,
            "used_default": True,
            "routed_to": [name for _, name in targets],
            "target_clusters": [default_cluster],
        }

    def _rollout_for_collection(
        self,
        collection_name: str,
        runtime: Optional[FederationRuntimeSnapshot] = None,
    ) -> Optional[RoutingRollout]:
        """Return the newest relevant rollout, failing closed on corrupt state."""
        runtime = runtime or self._runtime
        rollouts = [
            RoutingRollout.from_dict(payload)
            for payload in runtime.routing_rollouts.values()
            if payload.get("collection") == collection_name
        ]
        if not rollouts:
            return None
        terminal = {"completed", "cancelled", "rolled_back"}
        active = [item for item in rollouts if item.phase.value not in terminal]
        candidates = active or rollouts
        return max(candidates, key=lambda item: (item.updated_at, item.rollout_id))

    def _routing_policies(
        self,
        collection_name: str,
        *,
        access: str,
        runtime: Optional[FederationRuntimeSnapshot] = None,
    ) -> List[Dict[str, Any]]:
        """Resolve active/candidate policies for one coherent runtime snapshot."""
        runtime = runtime or self._runtime
        if runtime.routing_rules.get(collection_name, {}).get("disabled") is True:
            return []
        rollout = self._rollout_for_collection(collection_name, runtime)
        if rollout is None:
            return [runtime.routing_rules.get(collection_name, {})]
        read_modes, write_modes = rollout.policy_modes()
        modes = read_modes if access == "read" else write_modes
        policies = {
            "active": rollout.active_policy,
            "candidate": rollout.candidate_policy,
        }
        return [copy.deepcopy(policies[mode]) for mode in modes]

    def _targets_for_policy_document(
        self,
        collection_name: str,
        document: Dict[str, Any],
        policy: Dict[str, Any],
        runtime: Optional[FederationRuntimeSnapshot] = None,
    ) -> List[Tuple[typesense.Client, str]]:
        runtime = runtime or self._runtime
        preview = self.preview_routing(
            collection_name,
            document,
            rules_config=policy,
            runtime=runtime,
        )
        target_names = preview.get("target_clusters") or []
        if preview.get("used_default"):
            return self._default_targets(
                target_names[0] if target_names else "default",
                runtime,
            )
        return self._targets_for_names(target_names, runtime)

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
        targets, _revision, _rollout_id = self.get_indexing_route(
            collection_name,
            document,
        )
        return targets

    def get_indexing_route(
        self,
        collection_name: str,
        document: Dict,
    ) -> Tuple[List[Tuple[typesense.Client, str]], int, Optional[str]]:
        """Return targets and routing metadata from one immutable snapshot."""
        runtime = self._runtime
        targets: List[Tuple[typesense.Client, str]] = []
        seen = set()
        for policy in self._routing_policies(
            collection_name,
            access="write",
            runtime=runtime,
        ):
            for client, cluster_name in self._targets_for_policy_document(
                collection_name,
                document,
                policy,
                runtime,
            ):
                if cluster_name not in seen:
                    seen.add(cluster_name)
                    targets.append((client, cluster_name))
        rollout = self._rollout_for_collection(collection_name, runtime)
        return targets, runtime.revision, rollout.rollout_id if rollout else None

    def get_delete_route(
        self,
        collection_name: str,
    ) -> Tuple[List[Tuple[str, typesense.Client]], int, Optional[str]]:
        """Return deletion fan-out and routing metadata from one snapshot."""
        runtime = self._runtime
        if runtime.routing_rules.get(collection_name, {}).get("disabled") is True:
            return [], runtime.revision, None
        rollout = self._rollout_for_collection(collection_name, runtime)
        return (
            list(runtime.clients.items()),
            runtime.revision,
            rollout.rollout_id if rollout else None,
        )

    def _cluster_names_for_search(
        self,
        collection_name: str,
        runtime: Optional[FederationRuntimeSnapshot] = None,
    ) -> List[str]:
        """Return cluster names that may contain documents for a collection."""
        runtime = runtime or self._runtime
        rollout = self._rollout_for_collection(collection_name, runtime)
        cluster_names: List[str] = []

        def add_cluster(name: str) -> None:
            if name == "default":
                name = self._resolve_default_cluster(runtime) or ""
            if name and name in runtime.clients and name not in cluster_names:
                cluster_names.append(name)

        for rules in self._routing_policies(
            collection_name,
            access="read",
            runtime=runtime,
        ):
            if not rules.get("rules"):
                if rollout is None:
                    # Preserve legacy unsharded behavior: all clusters may contain
                    # historical copies, so narrowing would hide documents.
                    for name in runtime.clients:
                        add_cluster(name)
                else:
                    add_cluster(rules.get("default_cluster", "default"))
                continue
            for rule in rules.get("rules", []):
                for name in self._rule_targets(rule):
                    add_cluster(name)
            add_cluster(rules.get("default_cluster", "default"))
        return cluster_names

    def get_named_clients_for_search(
        self, collection_name: str
    ) -> List[Tuple[str, typesense.Client]]:
        """Get named clients that may contain documents for a collection."""
        runtime = self._runtime
        return [
            (name, runtime.clients[name])
            for name in self._cluster_names_for_search(collection_name, runtime)
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
        if self._runtime.routing_rules.get(collection_name, {}).get("disabled") is True:
            return []
        return list(self._runtime.clients.items())

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

    def validate_routing_policy(
        self,
        rules: List[Dict],
        default_cluster: str,
    ) -> None:
        """Validate that every target resolves without mutating runtime state."""
        all_clusters = set()
        for rule in rules:
            for target_name in self._rule_targets(rule):
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
        self.validate_routing_policy(rules, default_cluster)

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
        routing_rollouts: Optional[Dict[str, Dict[str, Any]]] = None,
        *,
        revision: int = 0,
    ) -> None:
        """
        Load federation configuration from saved state.

        Args:
            clusters_config: Saved cluster configurations
            routing_rules: Saved routing rules
            collection_schemas: Desired collection schemas to reconcile across clusters
            collection_aliases: Desired collection aliases keyed by cluster name
        """
        normalized_configs = {
            name: self.normalize_cluster_config(config, name=name)
            for name, config in clusters_config.items()
        }
        clients = {
            name: self.build_client(config)
            for name, config in normalized_configs.items()
        }
        next_runtime = FederationRuntimeSnapshot(
            revision=revision,
            clusters_config=normalized_configs,
            clients=clients,
            routing_rules=copy.deepcopy(routing_rules),
            collection_schemas=copy.deepcopy(collection_schemas or {}),
            collection_aliases=copy.deepcopy(collection_aliases or {}),
            routing_rollouts=copy.deepcopy(routing_rollouts or {}),
        )
        # Building clients and validating state happens above. One pointer swap
        # makes the complete view visible without an empty/partial reload window.
        self._runtime = next_runtime

        logger.info(f"Loaded {len(clusters_config)} federated cluster(s) from state.")

    def reload_from_state(
        self,
        clusters_config: Dict[str, Dict],
        routing_rules: Dict[str, Dict],
        collection_schemas: Optional[Dict[str, Dict]] = None,
        collection_aliases: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None,
        routing_rollouts: Optional[Dict[str, Dict[str, Any]]] = None,
        *,
        revision: int = 0,
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
        self.load_from_state(
            clusters_config,
            routing_rules,
            collection_schemas,
            collection_aliases,
            routing_rollouts,
            revision=revision,
        )
        logger.info("Configuration reloaded from state store.")
