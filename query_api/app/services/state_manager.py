"""
State Manager Service for IMPOSBRO Search.

This module exposes one state-management facade over the transactional
control-plane store and the read-compatible legacy Typesense backend.
"""

import copy
import json
import logging
import typesense
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

from control_plane import (
    AuditEvent,
    ControlPlaneStore,
    StateConflictError,
)
from secret_resolver import (
    SecretResolutionError,
    SecretResolver,
    build_secret_resolver,
    validate_cluster_secret_config,
)

logger = logging.getLogger(__name__)

# Constants for state storage
STATE_COLLECTION_NAME = "_imposbro_state"
STATE_DOCUMENT_ID = "config_v1"
AUDIT_COLLECTION_NAME = "_imposbro_audit_log"


class StateLoadError(RuntimeError):
    """Raised when persisted control-plane state exists but cannot be loaded safely."""


class StateManager:
    """
    Manages durable control-plane state through an explicit store contract.

    PostgreSQL is the transactional production backend. The Typesense client
    remains available only for compatibility while existing installations
    migrate their v1 state.

    Attributes:
        client: Typesense client connected to the internal HA cluster
    """

    def __init__(
        self,
        client: Optional[typesense.Client] = None,
        *,
        store: Optional[ControlPlaneStore] = None,
        secret_resolver: Optional[SecretResolver] = None,
        allow_inline_cluster_secrets: bool = True,
    ):
        """
        Initialize the StateManager.

        Args:
            client: A Typesense client connected to the internal state cluster
        """
        if (client is None) == (store is None):
            raise ValueError("Configure exactly one control-plane persistence backend")
        self.client = client
        self.store = store
        self.secret_resolver = secret_resolver or build_secret_resolver(
            "/run/secrets/imposbro"
        )
        self.allow_inline_cluster_secrets = allow_inline_cluster_secrets
        self.current_revision = 0
        self.state_digest = ""
        self.routing_rollouts: Dict[str, Dict[str, Any]] = {}
        if self.store is not None:
            self.store.check_ready()
            self.current_revision = self.store.latest_revision() or 0
        else:
            self._ensure_collection_exists()

    @property
    def transactional(self) -> bool:
        """Return whether state, audit, and outbox share one transaction."""
        return self.store is not None

    def _validate_cluster_secrets(
        self,
        clusters_config: Dict[str, Dict],
    ) -> None:
        for config in clusters_config.values():
            validate_cluster_secret_config(
                config,
                allow_inline=self.allow_inline_cluster_secrets,
                resolver=self.secret_resolver,
            )

    def _ensure_collection_exists(self) -> None:
        """Ensure internal state collections exist, creating them if needed."""
        if self.client is None:
            raise RuntimeError("Legacy Typesense state client is not configured")
        try:
            self.client.collections[STATE_COLLECTION_NAME].retrieve()
            logger.info(f"State collection '{STATE_COLLECTION_NAME}' exists.")
        except typesense.exceptions.ObjectNotFound:
            logger.info(f"Creating state collection '{STATE_COLLECTION_NAME}'...")
            schema = {
                "name": STATE_COLLECTION_NAME,
                "fields": [{"name": "state_data", "type": "string"}],
            }
            self.client.collections.create(schema)
            logger.info(f"State collection '{STATE_COLLECTION_NAME}' created.")

        try:
            self.client.collections[AUDIT_COLLECTION_NAME].retrieve()
            logger.info("Audit collection '%s' exists.", AUDIT_COLLECTION_NAME)
        except typesense.exceptions.ObjectNotFound:
            logger.info("Creating audit collection '%s'...", AUDIT_COLLECTION_NAME)
            schema = {
                "name": AUDIT_COLLECTION_NAME,
                "fields": [
                    {"name": "timestamp_ms", "type": "int64", "sort": True},
                    {"name": "timestamp", "type": "string"},
                    {"name": "actor", "type": "string", "facet": True},
                    {"name": "action", "type": "string", "facet": True},
                    {"name": "resource_type", "type": "string", "facet": True},
                    {"name": "resource_id", "type": "string", "facet": True},
                    {"name": "status", "type": "string", "facet": True},
                    {"name": "details_json", "type": "string"},
                ],
                "default_sorting_field": "timestamp_ms",
            }
            self.client.collections.create(schema)
            logger.info("Audit collection '%s' created.", AUDIT_COLLECTION_NAME)

    def save_state(
        self,
        federation_clusters_config: Dict[str, Dict],
        collection_routing_rules: Dict[str, Dict],
        collection_schemas: Optional[Dict[str, Dict]] = None,
        collection_aliases: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None,
        routing_rollouts: Optional[Dict[str, Dict[str, Any]]] = None,
        *,
        audit: Optional[AuditEvent] = None,
        event_type: str = "control_plane.changed",
        event_payload: Optional[Dict[str, Any]] = None,
        expected_revision: Optional[int] = None,
    ) -> bool:
        """
        Save the current application state to Typesense.

        Args:
            federation_clusters_config: Dictionary of cluster configurations
            collection_routing_rules: Dictionary of collection routing rules
            collection_schemas: Desired collection schemas for reconciliation
            collection_aliases: Desired collection aliases keyed by cluster name

        Returns:
            True if save was successful, False otherwise
        """
        self._validate_cluster_secrets(federation_clusters_config)
        state = {
            "federation_clusters_config": federation_clusters_config,
            "collection_routing_rules": collection_routing_rules,
            "collection_schemas": collection_schemas or {},
            "collection_aliases": collection_aliases or {},
            "routing_rollouts": (
                self.routing_rollouts
                if routing_rollouts is None
                else routing_rollouts
            ),
        }
        if self.store is not None:
            try:
                record = self.store.commit(
                    expected_revision=(
                        self.current_revision
                        if expected_revision is None
                        else expected_revision
                    ),
                    state=state,
                    audit=audit
                    or AuditEvent(
                        actor="system",
                        action="state_bootstrapped",
                        resource_type="control_plane_state",
                        resource_id="current",
                    ),
                    event_type=event_type,
                    event_payload=event_payload or {},
                )
                self.current_revision = record.revision
                self.state_digest = record.state_digest
                self.routing_rollouts = copy.deepcopy(state["routing_rollouts"])
                logger.info(
                    "Control-plane state committed at revision %s.",
                    record.revision,
                )
                return True
            except StateConflictError:
                raise
            except Exception as exc:
                logger.error("Failed to commit transactional state: %s", exc)
                return False

        try:
            if self.client is None:
                raise RuntimeError("Legacy Typesense state client is not configured")
            state_document = {"id": STATE_DOCUMENT_ID, "state_data": json.dumps(state)}
            self.client.collections[STATE_COLLECTION_NAME].documents.upsert(
                state_document
            )
            self.routing_rollouts = copy.deepcopy(state["routing_rollouts"])
            logger.info("State successfully saved to Typesense.")
            return True
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
            return False

    def load_state(
        self,
    ) -> Tuple[Optional[Dict], Optional[Dict], Optional[Dict], Optional[Dict]]:
        """
        Load application state from Typesense.

        Returns:
            Tuple of (
                federation_clusters_config,
                collection_routing_rules,
                collection_schemas,
                collection_aliases,
            )
            Returns (None, None, None, None) if no state exists
        """
        if self.store is not None:
            try:
                record = self.store.load()
            except Exception as exc:
                logger.error("Error loading transactional state: %s", exc)
                raise StateLoadError("Could not load persisted state") from exc
            if record is None:
                self.current_revision = 0
                self.state_digest = ""
                self.routing_rollouts = {}
                return None, None, None, None
            self.current_revision = record.revision
            self.state_digest = record.state_digest
            state = record.state
            try:
                self._validate_cluster_secrets(
                    state.get("federation_clusters_config") or {}
                )
            except SecretResolutionError as exc:
                raise StateLoadError(
                    "Persisted cluster secret policy is invalid"
                ) from exc
            self.routing_rollouts = state.get("routing_rollouts") or {}
            return (
                state.get("federation_clusters_config") or {},
                state.get("collection_routing_rules") or {},
                state.get("collection_schemas") or {},
                state.get("collection_aliases") or {},
            )

        try:
            if self.client is None:
                raise RuntimeError("Legacy Typesense state client is not configured")
            state_document = (
                self.client.collections[STATE_COLLECTION_NAME]
                .documents[STATE_DOCUMENT_ID]
                .retrieve()
            )
            state = json.loads(state_document.get("state_data", "{}"))

            clusters_config = state.get("federation_clusters_config") or {}
            self._validate_cluster_secrets(clusters_config)
            routing_rules = state.get("collection_routing_rules") or {}
            collection_schemas = state.get("collection_schemas") or {}
            collection_aliases = state.get("collection_aliases") or {}
            self.routing_rollouts = state.get("routing_rollouts") or {}

            logger.info(
                (
                    "Loaded %s cluster(s), %s routing rule(s), %s collection schema(s), "
                    "%s collection alias(es), and %s routing rollout(s) from state."
                ),
                len(clusters_config),
                len(routing_rules),
                len(collection_schemas),
                sum(len(aliases) for aliases in collection_aliases.values()),
                len(self.routing_rollouts),
            )
            return clusters_config, routing_rules, collection_schemas, collection_aliases

        except typesense.exceptions.ObjectNotFound:
            logger.info("No saved state document found.")
            self.routing_rollouts = {}
            return None, None, None, None
        except (json.JSONDecodeError, TypeError, ValueError, SecretResolutionError) as e:
            logger.error("Persisted state document is invalid: %s", e)
            raise StateLoadError("Persisted state document is invalid") from e
        except Exception as e:
            logger.error("Error loading state: %s", e)
            raise StateLoadError("Could not load persisted state") from e

    def record_admin_audit(
        self,
        *,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        status: str = "success",
        details: Optional[Dict[str, Any]] = None,
        request_id: str = "",
    ) -> bool:
        """
        Persist a safe admin audit event.

        Raw API keys, cluster credentials, and request bodies containing secrets should
        not be passed in details; callers are expected to provide safe metadata only.
        """
        if self.store is not None:
            try:
                return self.store.append_audit(
                    AuditEvent(
                        actor=actor,
                        action=action,
                        resource_type=resource_type,
                        resource_id=resource_id,
                        status=status,
                        request_id=request_id,
                        details=details or {},
                    ),
                    revision=self.current_revision or None,
                )
            except Exception as exc:
                logger.warning("Failed to record transactional audit event: %s", exc)
                return False

        try:
            if self.client is None:
                raise RuntimeError("Legacy Typesense state client is not configured")
            now = datetime.now(timezone.utc)
            timestamp_ms = int(now.timestamp() * 1000)
            document = {
                "id": f"{timestamp_ms}-{uuid.uuid4().hex}",
                "timestamp_ms": timestamp_ms,
                "timestamp": now.isoformat(),
                "actor": actor,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "status": status,
                "details_json": json.dumps(details or {}, sort_keys=True),
            }
            self.client.collections[AUDIT_COLLECTION_NAME].documents.upsert(document)
            logger.info(
                "Audit event recorded: %s %s/%s by %s",
                action,
                resource_type,
                resource_id,
                actor,
            )
            return True
        except Exception as e:
            logger.warning("Failed to record admin audit event: %s", e)
            return False

    def list_admin_audit(
        self,
        *,
        limit: int = 50,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
    ) -> list[Dict[str, Any]]:
        """Return recent admin audit events, newest first."""
        if self.store is not None:
            try:
                return self.store.list_audit(
                    limit=limit,
                    action=action,
                    resource_type=resource_type,
                )
            except Exception as exc:
                logger.warning("Failed to load transactional audit log: %s", exc)
                return []

        try:
            if self.client is None:
                raise RuntimeError("Legacy Typesense state client is not configured")
            filters = []
            if action:
                filters.append(f"action:={action}")
            if resource_type:
                filters.append(f"resource_type:={resource_type}")

            result = self.client.collections[AUDIT_COLLECTION_NAME].documents.search(
                {
                    "q": "*",
                    "query_by": "action,resource_type,resource_id,actor,status",
                    "sort_by": "timestamp_ms:desc",
                    "per_page": limit,
                    **({"filter_by": " && ".join(filters)} if filters else {}),
                }
            )
            entries = []
            for hit in result.get("hits", []):
                document = hit.get("document", {})
                details_json = document.pop("details_json", "{}")
                try:
                    details = json.loads(details_json)
                except json.JSONDecodeError:
                    details = {}
                entries.append({**document, "details": details})
            return entries
        except typesense.exceptions.ObjectNotFound:
            return []
        except Exception as e:
            logger.warning("Failed to load admin audit log: %s", e)
            return []

    def export_admin_audit(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """Verify and export one ascending audit-chain page."""
        if self.store is None:
            entries = self.list_admin_audit(limit=limit)
            entries.reverse()
            return [
                item
                for item in entries
                if int(item.get("sequence", 0)) > after_sequence
            ][:limit]
        self.store.verify_audit_chain()
        return self.store.export_audit(
            after_sequence=after_sequence,
            limit=limit,
        )

    def authoritative_revision(self) -> int:
        """Return the durable head revision used by replica reconciliation."""
        if self.store is None:
            return self.current_revision
        revision = self.store.latest_revision()
        return int(revision or 0)

    def pending_outbox(self, *, after_revision: int = 0, limit: int = 100):
        """Expose durable changes for the notifier/reconciler boundary."""
        if self.store is None:
            return []
        return self.store.list_outbox(after_revision=after_revision, limit=limit)

    def unpublished_outbox(self, *, limit: int = 100):
        if self.store is None:
            return []
        return self.store.list_unpublished_outbox(limit=limit)

    def mark_outbox_published(self, event_id: str) -> bool:
        if self.store is None:
            return False
        return self.store.mark_outbox_published(event_id)

    def record_outbox_failure(self, event_id: str, error: str) -> bool:
        if self.store is None:
            return False
        return self.store.record_outbox_failure(event_id, error)
