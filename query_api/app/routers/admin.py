"""
Admin Router for IMPOSBRO Search API.

This module contains all administrative endpoints for managing clusters,
collections, and routing rules.
"""

import asyncio
import logging
import typesense
from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any

from models import Cluster, CollectionSchema, RoutingRules, OperationResponse
from services import FederationService, StateManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Administration"])


def get_federation_service() -> FederationService:
    """Dependency injection placeholder - set at startup."""
    raise NotImplementedError("Federation service not initialized")


def get_state_manager() -> StateManager:
    """Dependency injection placeholder - set at startup."""
    raise NotImplementedError("State manager not initialized")


# ----- Cluster Management -----


@router.get("/federation/clusters", summary="List all registered clusters")
def get_all_clusters(
    federation: FederationService = Depends(get_federation_service),
) -> Dict[str, Any]:
    """
    Retrieve all registered federation clusters.

    Returns a dictionary of cluster configurations including a virtual
    'default' entry representing the internal HA cluster.
    """
    display_config = federation.clusters_config.copy()
    display_config["default"] = {
        "name": "default",
        "host": "Internal HA Cluster",
        "port": 8108,
        "api_key": "N/A",
    }
    return display_config


@router.post("/federation/clusters", status_code=201, summary="Register a new cluster")
def register_cluster(
    cluster: Cluster,
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
) -> OperationResponse:
    """
    Register a new Typesense cluster with the federation.

    The cluster will be available for routing rules and federated search.
    """
    try:
        federation.register_cluster(
            name=cluster.name,
            host=cluster.host,
            port=cluster.port,
            api_key=cluster.api_key,
        )
        state_manager.save_state(federation.clusters_config, federation.routing_rules)
        return OperationResponse(message=f"Cluster '{cluster.name}' registered.")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to register cluster: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to create client: {str(e)}"
        )


@router.delete("/federation/clusters/{cluster_name}", summary="Remove a cluster")
def delete_cluster(
    cluster_name: str,
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
) -> OperationResponse:
    """
    Remove a cluster from the federation.

    The cluster must not be in use by any routing rules.
    """
    if cluster_name == "default":
        raise HTTPException(
            status_code=400, detail="Cannot delete the default cluster."
        )

    try:
        federation.unregister_cluster(cluster_name)
        state_manager.save_state(federation.clusters_config, federation.routing_rules)
        return OperationResponse(message=f"Cluster '{cluster_name}' deleted.")
    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))


# ----- Collection Management -----


@router.get("/collections/{collection_name}", summary="Get collection schema")
def get_collection_schema(
    collection_name: str,
    federation: FederationService = Depends(get_federation_service),
) -> Dict[str, Any]:
    """
    Retrieve the schema for a collection.

    Queries the first available cluster to get schema information.
    """
    clients = list(federation.clients.values())
    if not clients:
        raise HTTPException(status_code=500, detail="No clusters available.")

    client = clients[0]
    try:
        schema_info = client.collections[collection_name].retrieve()
        return {"fields": schema_info.get("fields", [])}
    except typesense.exceptions.ObjectNotFound:
        raise HTTPException(
            status_code=404, detail=f"Collection '{collection_name}' not found."
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to retrieve schema: {str(e)}"
        )


@router.post("/collections", status_code=201, summary="Create a collection")
async def create_collection(
    schema: CollectionSchema,
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
) -> OperationResponse:
    """
    Create a collection across all federated clusters.

    The schema is applied to all registered clusters simultaneously.
    """
    schema_dict = schema.model_dump()
    if schema_dict.get("default_sorting_field") is None:
        del schema_dict["default_sorting_field"]

    async def create_on_cluster(client: typesense.Client, name: str) -> None:
        try:
            await asyncio.to_thread(client.collections.create, schema_dict)
            logger.info(f"Collection '{schema.name}' created on cluster '{name}'")
        except typesense.exceptions.ObjectAlreadyExists:
            logger.debug(f"Collection '{schema.name}' already exists on '{name}'")
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Failed on cluster {name}: {e}"
            )

    tasks = [
        create_on_cluster(client, name) for name, client in federation.clients.items()
    ]
    await asyncio.gather(*tasks)

    # Initialize default routing for this collection
    if schema.name not in federation.routing_rules:
        federation.routing_rules[schema.name] = {
            "rules": [],
            "default_cluster": "default",
        }

    state_manager.save_state(federation.clusters_config, federation.routing_rules)

    return OperationResponse(message="Collection created successfully on all clusters.")


@router.delete("/collections/{collection_name}", summary="Delete a collection")
async def delete_collection(
    collection_name: str,
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
) -> OperationResponse:
    """
    Delete a collection from all federated clusters.
    """

    async def delete_on_cluster(client: typesense.Client) -> None:
        try:
            await asyncio.to_thread(client.collections[collection_name].delete)
        except typesense.exceptions.ObjectNotFound:
            pass

    tasks = [delete_on_cluster(client) for client in federation.clients.values()]
    await asyncio.gather(*tasks)

    if collection_name in federation.routing_rules:
        del federation.routing_rules[collection_name]

    state_manager.save_state(federation.clusters_config, federation.routing_rules)

    return OperationResponse(message=f"Collection '{collection_name}' deleted.")


# ----- Routing Rules Management -----


@router.get("/routing-map", summary="Get complete routing configuration")
def get_routing_map(
    federation: FederationService = Depends(get_federation_service),
) -> Dict[str, Any]:
    """
    Get the complete routing configuration including all clusters and rules.
    """
    return {
        "clusters": list(federation.clients.keys()),
        "collections": federation.routing_rules,
    }


@router.post("/routing-rules", status_code=201, summary="Set routing rules")
def set_routing_rules(
    rules_config: RoutingRules,
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
) -> OperationResponse:
    """
    Set or update routing rules for a collection.

    Rules are evaluated in order - the first matching rule determines
    the target cluster for a document.
    """
    collection_name = rules_config.collection

    # Verify collection exists
    clients = list(federation.clients.values())
    if clients:
        try:
            clients[0].collections[collection_name].retrieve()
        except typesense.exceptions.ObjectNotFound:
            raise HTTPException(
                status_code=404,
                detail=f"Cannot set rules for non-existent collection '{collection_name}'.",
            )

    try:
        rules_list = [r.model_dump() for r in rules_config.rules]
        federation.set_routing_rules(
            collection=collection_name,
            rules=rules_list,
            default_cluster=rules_config.default_cluster,
        )
        state_manager.save_state(federation.clusters_config, federation.routing_rules)
        return OperationResponse(
            message=f"Routing rules for '{collection_name}' have been updated."
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/routing-rules/{collection_name}", summary="Delete routing rules")
def delete_routing_rule(
    collection_name: str,
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
) -> OperationResponse:
    """
    Delete all routing rules for a collection, reverting to default routing.
    """
    federation.delete_routing_rules(collection_name)
    state_manager.save_state(federation.clusters_config, federation.routing_rules)
    return OperationResponse(
        message=f"Routing rules for '{collection_name}' have been deleted."
    )
