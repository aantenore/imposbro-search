"""
State Manager Service for IMPOSBRO Search.

This module handles persistence of application state to the internal
Typesense HA cluster. It provides save/load functionality for federation
configuration and routing rules.
"""

import json
import logging
import typesense
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger(__name__)

# Constants for state storage
STATE_COLLECTION_NAME = "_imposbro_state"
STATE_DOCUMENT_ID = "config_v1"
AUDIT_COLLECTION_NAME = "_imposbro_audit_log"


class StateLoadError(RuntimeError):
    """Raised when persisted control-plane state exists but cannot be loaded safely."""


class StateManager:
    """
    Manages application state persistence using Typesense as the backend.

    The StateManager stores federation cluster configurations and routing
    rules in a highly available Typesense cluster, ensuring the configuration
    survives restarts and can be shared across multiple API instances.

    Attributes:
        client: Typesense client connected to the internal HA cluster
    """

    def __init__(self, client: typesense.Client):
        """
        Initialize the StateManager.

        Args:
            client: A Typesense client connected to the internal state cluster
        """
        self.client = client
        self._ensure_collection_exists()

    def _ensure_collection_exists(self) -> None:
        """Ensure internal state collections exist, creating them if needed."""
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
    ) -> bool:
        """
        Save the current application state to Typesense.

        Args:
            federation_clusters_config: Dictionary of cluster configurations
            collection_routing_rules: Dictionary of collection routing rules

        Returns:
            True if save was successful, False otherwise
        """
        try:
            state = {
                "federation_clusters_config": federation_clusters_config,
                "collection_routing_rules": collection_routing_rules,
            }
            state_document = {"id": STATE_DOCUMENT_ID, "state_data": json.dumps(state)}
            self.client.collections[STATE_COLLECTION_NAME].documents.upsert(
                state_document
            )
            logger.info("State successfully saved to Typesense.")
            return True
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
            return False

    def load_state(self) -> Tuple[Optional[Dict], Optional[Dict]]:
        """
        Load application state from Typesense.

        Returns:
            Tuple of (federation_clusters_config, collection_routing_rules)
            Returns (None, None) if no state exists or an error occurs
        """
        try:
            state_document = (
                self.client.collections[STATE_COLLECTION_NAME]
                .documents[STATE_DOCUMENT_ID]
                .retrieve()
            )
            state = json.loads(state_document.get("state_data", "{}"))

            clusters_config = state.get("federation_clusters_config", {})
            routing_rules = state.get("collection_routing_rules", {})

            logger.info(
                f"Loaded {len(clusters_config)} cluster(s) and {len(routing_rules)} routing rule(s) from state."
            )
            return clusters_config, routing_rules

        except typesense.exceptions.ObjectNotFound:
            logger.info("No saved state document found.")
            return None, None
        except (json.JSONDecodeError, TypeError, ValueError) as e:
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
    ) -> bool:
        """
        Persist a safe admin audit event.

        Raw API keys, cluster credentials, and request bodies containing secrets should
        not be passed in details; callers are expected to provide safe metadata only.
        """
        try:
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
        try:
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
