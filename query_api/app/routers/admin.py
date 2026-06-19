"""
Admin Router for IMPOSBRO Search API.

This module contains all administrative endpoints for managing clusters,
collections, and routing rules. All configuration changes are broadcast
via Redis Pub/Sub for multi-instance synchronization.
"""

import asyncio
import hashlib
import logging
import typesense
from fastapi import APIRouter, HTTPException, Depends, Path, Query, Request
from typing import Dict, Any, Optional

from constants import NAME_PATTERN
from deps import (
    get_federation_service,
    get_state_manager,
    get_config_notifier,
    require_admin_api_key,
)
from models import (
    Cluster,
    CollectionSchema,
    RoutingRules,
    OperationResponse,
    AuditLogResponse,
)
from services import FederationService, StateManager, SyncConfigNotifier
from settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin",
    tags=["Administration"],
    dependencies=[Depends(require_admin_api_key)],
)


def _notify_config_change(notifier: SyncConfigNotifier, change_type: str):
    """Helper to broadcast config changes to all instances."""
    try:
        notifier.notify(change_type)
    except Exception as e:
        logger.warning("Failed to broadcast config change: %s", e)


def _persist_state_or_500(
    state_manager: StateManager,
    federation: FederationService,
) -> None:
    """Persist config mutations and fail the request if the control-plane state cannot be saved."""
    saved = state_manager.save_state(
        federation.clusters_config,
        federation.routing_rules,
    )
    if not saved:
        raise HTTPException(
            status_code=500,
            detail="Configuration changed in memory but could not be persisted.",
        )


def _mask_api_key(api_key: str, visible: int = 4) -> str:
    """Mask API key for display (show only last visible chars)."""
    if not api_key or api_key == "N/A":
        return api_key
    if len(api_key) <= visible:
        return "***"
    return "*" * (len(api_key) - visible) + api_key[-visible:]


def _admin_actor_from_request(request: Request) -> str:
    """Return a non-sensitive actor id for audit events."""
    provided = request.headers.get("X-API-Key")
    authorization = request.headers.get("Authorization", "")
    if not provided and authorization.startswith("Bearer "):
        provided = authorization[7:].strip()
    if provided:
        digest = hashlib.sha256(provided.encode("utf-8")).hexdigest()[:12]
        return f"api_key:{digest}"
    if settings.ALLOW_UNAUTHENTICATED_ADMIN:
        return "unauthenticated-dev"
    return "unknown"


def _record_admin_audit(
    state_manager: StateManager,
    request: Request,
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort admin audit logging for successful control-plane mutations."""
    if not settings.AUDIT_LOG_ENABLED:
        return
    state_manager.record_admin_audit(
        actor=_admin_actor_from_request(request),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details or {},
    )


# ----- Cluster Management -----


@router.get("/stats", summary="Metrics summary for dashboard")
def get_admin_stats(
    federation: FederationService = Depends(get_federation_service),
) -> Dict[str, Any]:
    """
    Return a JSON summary of key metrics for the Admin UI dashboard.
    For full Prometheus metrics use GET /metrics.
    """
    clusters = len(federation.clients) if federation else 0
    collections = len(federation.routing_rules) if federation else 0
    return {
        "clusters": clusters,
        "collections": collections,
        "metrics_url": "/metrics",
    }


@router.get("/federation/clusters", summary="List all registered clusters")
def get_all_clusters(
    federation: FederationService = Depends(get_federation_service),
) -> Dict[str, Any]:
    """
    Retrieve all registered federation clusters.

    Returns a dictionary of cluster configurations including a virtual
    'default' entry representing the internal HA cluster.
    API keys are masked in the response for security.
    """
    display_config = {}
    for name, cfg in federation.clusters_config.items():
        display_config[name] = {**cfg, "api_key": _mask_api_key(cfg.get("api_key", ""))}
    display_config["default"] = {
        "name": "default",
        "host": "Internal HA Cluster",
        "port": 8108,
        "api_key": "N/A",
    }
    return display_config


@router.get(
    "/audit-log",
    summary="List recent admin audit events",
    response_model=AuditLogResponse,
)
def get_audit_log(
    limit: int = Query(
        min(50, settings.AUDIT_LOG_MAX_RESULTS),
        ge=1,
        le=settings.AUDIT_LOG_MAX_RESULTS,
    ),
    action: Optional[str] = Query(None, pattern=r"^[A-Za-z0-9_.:-]+$"),
    resource_type: Optional[str] = Query(None, pattern=r"^[A-Za-z0-9_.:-]+$"),
    state_manager: StateManager = Depends(get_state_manager),
) -> Dict[str, Any]:
    """Return recent successful admin mutations without exposing secrets."""
    return {
        "entries": state_manager.list_admin_audit(
            limit=limit,
            action=action,
            resource_type=resource_type,
        )
    }


@router.get(
    "/federation/clusters/internal",
    summary="List raw cluster config for internal services",
    include_in_schema=False,
)
def get_internal_clusters(
    federation: FederationService = Depends(get_federation_service),
) -> Dict[str, Any]:
    """
    Return raw cluster configuration for trusted service-to-service consumers.

    The public cluster listing intentionally masks API keys for the Admin UI.
    The indexing service needs unmasked credentials to build Typesense clients,
    and this route inherits the Admin API key dependency from the router.
    """
    return {name: dict(cfg) for name, cfg in federation.clusters_config.items()}


@router.post("/federation/clusters", status_code=201, summary="Register a new cluster")
def register_cluster(
    request: Request,
    cluster: Cluster,
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
    notifier: SyncConfigNotifier = Depends(get_config_notifier),
) -> OperationResponse:
    """
    Register a new Typesense cluster with the federation.

    The cluster will be available for routing rules and federated search.
    All API instances will be notified of this change.
    """
    try:
        federation.register_cluster(
            name=cluster.name,
            host=cluster.host,
            port=cluster.port,
            api_key=cluster.api_key,
        )
        _persist_state_or_500(state_manager, federation)
        _notify_config_change(notifier, f"cluster_registered:{cluster.name}")
        _record_admin_audit(
            state_manager,
            request,
            action="cluster_registered",
            resource_type="cluster",
            resource_id=cluster.name,
            details={"host": cluster.host, "port": cluster.port},
        )
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
    request: Request,
    cluster_name: str = Path(..., pattern=NAME_PATTERN, description="Cluster name"),
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
    notifier: SyncConfigNotifier = Depends(get_config_notifier),
) -> OperationResponse:
    """
    Remove a cluster from the federation.

    The cluster must not be in use by any routing rules.
    All API instances will be notified of this change.
    """
    if cluster_name == "default":
        raise HTTPException(
            status_code=400, detail="Cannot delete the default cluster."
        )

    try:
        federation.unregister_cluster(cluster_name)
        _persist_state_or_500(state_manager, federation)
        _notify_config_change(notifier, f"cluster_deleted:{cluster_name}")
        _record_admin_audit(
            state_manager,
            request,
            action="cluster_deleted",
            resource_type="cluster",
            resource_id=cluster_name,
        )
        return OperationResponse(message=f"Cluster '{cluster_name}' deleted.")
    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))


# ----- Collection Management -----


@router.get(
    "/collections/{collection_name}",
    summary="Get collection schema",
)
def get_collection_schema(
    collection_name: str = Path(..., pattern=NAME_PATTERN, description="Collection name"),
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
    request: Request,
    schema: CollectionSchema,
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
    notifier: SyncConfigNotifier = Depends(get_config_notifier),
) -> OperationResponse:
    """
    Create a collection across all federated clusters.

    The schema is applied to all registered clusters simultaneously.
    All API instances will be notified of this change.
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

    _persist_state_or_500(state_manager, federation)
    _notify_config_change(notifier, f"collection_created:{schema.name}")
    _record_admin_audit(
        state_manager,
        request,
        action="collection_created",
        resource_type="collection",
        resource_id=schema.name,
        details={
            "fields": [field.model_dump() for field in schema.fields],
            "default_sorting_field": schema.default_sorting_field,
            "cluster_count": len(federation.clients),
        },
    )

    return OperationResponse(message="Collection created successfully on all clusters.")


@router.delete("/collections/{collection_name}", summary="Delete a collection")
async def delete_collection(
    request: Request,
    collection_name: str = Path(..., pattern=NAME_PATTERN, description="Collection name"),
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
    notifier: SyncConfigNotifier = Depends(get_config_notifier),
) -> OperationResponse:
    """
    Delete a collection from all federated clusters.
    All API instances will be notified of this change.
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

    _persist_state_or_500(state_manager, federation)
    _notify_config_change(notifier, f"collection_deleted:{collection_name}")
    _record_admin_audit(
        state_manager,
        request,
        action="collection_deleted",
        resource_type="collection",
        resource_id=collection_name,
    )

    return OperationResponse(message=f"Collection '{collection_name}' deleted.")


# ----- Collection Aliases (zero-downtime reindexing) -----


@router.put(
    "/aliases/{alias_name}",
    summary="Create or update collection alias",
)
async def upsert_alias(
    request: Request,
    alias_name: str = Path(..., pattern=NAME_PATTERN, description="Alias name"),
    collection_name: str = Query(
        ..., pattern=NAME_PATTERN, description="Target collection name"
    ),
    cluster_name: str = Query(
        "default", pattern=NAME_PATTERN, description="Cluster where the alias is created"
    ),
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
) -> OperationResponse:
    """
    Create or update an alias pointing to a collection (Typesense alias).
    Use for zero-downtime reindexing: point alias to new collection after reindex.
    """
    client = federation.get_client_for_cluster(cluster_name)
    if not client:
        raise HTTPException(
            status_code=404,
            detail=f"Cluster '{cluster_name}' not found or not available.",
        )
    try:
        await asyncio.to_thread(
            client.aliases[alias_name].upsert,
            {"collection_name": collection_name},
        )
        _record_admin_audit(
            state_manager,
            request,
            action="alias_upserted",
            resource_type="alias",
            resource_id=alias_name,
            details={"collection_name": collection_name, "cluster_name": cluster_name},
        )
        return OperationResponse(
            message=f"Alias '{alias_name}' -> '{collection_name}' on cluster '{cluster_name}'."
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/aliases",
    summary="List aliases on a cluster",
)
def list_aliases(
    cluster_name: str = Query(
        "default", pattern=NAME_PATTERN, description="Cluster to list aliases from"
    ),
    federation: FederationService = Depends(get_federation_service),
) -> Dict[str, Any]:
    """List all collection aliases on the given cluster."""
    client = federation.get_client_for_cluster(cluster_name)
    if not client:
        raise HTTPException(
            status_code=404,
            detail=f"Cluster '{cluster_name}' not found or not available.",
        )
    try:
        aliases = client.aliases.retrieve()
        return {"cluster": cluster_name, "aliases": aliases}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete(
    "/aliases/{alias_name}",
    summary="Delete collection alias",
)
async def delete_alias(
    request: Request,
    alias_name: str = Path(..., pattern=NAME_PATTERN),
    cluster_name: str = Query(
        "default", pattern=NAME_PATTERN, description="Cluster where the alias lives"
    ),
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
) -> OperationResponse:
    """Remove an alias from the cluster."""
    client = federation.get_client_for_cluster(cluster_name)
    if not client:
        raise HTTPException(
            status_code=404,
            detail=f"Cluster '{cluster_name}' not found or not available.",
        )
    try:
        await asyncio.to_thread(client.aliases[alias_name].delete)
        _record_admin_audit(
            state_manager,
            request,
            action="alias_deleted",
            resource_type="alias",
            resource_id=alias_name,
            details={"cluster_name": cluster_name},
        )
        return OperationResponse(message=f"Alias '{alias_name}' deleted.")
    except typesense.exceptions.ObjectNotFound:
        raise HTTPException(status_code=404, detail=f"Alias '{alias_name}' not found.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
    request: Request,
    rules_config: RoutingRules,
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
    notifier: SyncConfigNotifier = Depends(get_config_notifier),
) -> OperationResponse:
    """
    Set or update routing rules for a collection.

    Rules are evaluated in order - the first matching rule determines
    the target cluster for a document. All API instances will be notified.
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
        _persist_state_or_500(state_manager, federation)
        _notify_config_change(notifier, f"routing_updated:{collection_name}")
        _record_admin_audit(
            state_manager,
            request,
            action="routing_updated",
            resource_type="routing_rule",
            resource_id=collection_name,
            details={
                "rules_count": len(rules_list),
                "default_cluster": rules_config.default_cluster,
            },
        )
        return OperationResponse(
            message=f"Routing rules for '{collection_name}' have been updated."
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete(
    "/routing-rules/{collection_name}",
    summary="Delete routing rules",
)
def delete_routing_rule(
    request: Request,
    collection_name: str = Path(..., pattern=NAME_PATTERN, description="Collection name"),
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
    notifier: SyncConfigNotifier = Depends(get_config_notifier),
) -> OperationResponse:
    """
    Delete all routing rules for a collection, reverting to default routing.
    All API instances will be notified.
    """
    federation.delete_routing_rules(collection_name)
    _persist_state_or_500(state_manager, federation)
    _notify_config_change(notifier, f"routing_deleted:{collection_name}")
    _record_admin_audit(
        state_manager,
        request,
        action="routing_deleted",
        resource_type="routing_rule",
        resource_id=collection_name,
    )
    return OperationResponse(
        message=f"Routing rules for '{collection_name}' have been deleted."
    )
