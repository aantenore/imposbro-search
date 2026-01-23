"""
State Manager Service for IMPOSBRO Search.

This module handles persistence of application state to the internal
Typesense HA cluster. It provides save/load functionality for federation
configuration and routing rules.
"""

import json
import logging
import typesense
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger(__name__)

# Constants for state storage
STATE_COLLECTION_NAME = "_imposbro_state"
STATE_DOCUMENT_ID = "config_v1"


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
        """Ensure the state collection exists, creating it if needed."""
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
        except Exception as e:
            logger.error(f"Error loading state: {e}")
            return None, None
