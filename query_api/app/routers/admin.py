"""
Admin Router for IMPOSBRO Search API.

This module contains all administrative endpoints for managing clusters,
collections, and routing rules. All configuration changes are broadcast
via Redis Pub/Sub for multi-instance synchronization.
"""

import asyncio
import copy
import hashlib
import logging
import re
import typesense
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends, Path, Query, Request
from pydantic import ValidationError
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
    ControlPlaneStateSnapshot,
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


def _federation_runtime_snapshot(federation: FederationService) -> Dict[str, Any]:
    """Capture mutable runtime federation state before a control-plane mutation."""
    return {
        "clusters_config": copy.deepcopy(federation.clusters_config),
        "clients": dict(federation.clients),
        "routing_rules": copy.deepcopy(federation.routing_rules),
        "collection_schemas": copy.deepcopy(federation.collection_schemas),
    }


def _restore_federation_runtime(
    federation: FederationService,
    snapshot: Dict[str, Any],
) -> None:
    """Restore mutable runtime federation state after a failed persistence write."""
    federation.clusters_config.clear()
    federation.clusters_config.update(snapshot["clusters_config"])
    federation.clients.clear()
    federation.clients.update(snapshot["clients"])
    federation.routing_rules.clear()
    federation.routing_rules.update(snapshot["routing_rules"])
    federation.collection_schemas.clear()
    federation.collection_schemas.update(snapshot["collection_schemas"])


def _persist_state_or_500(
    state_manager: StateManager,
    federation: FederationService,
    rollback_snapshot: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist config mutations, rolling runtime state back if persistence fails."""
    saved = state_manager.save_state(
        federation.clusters_config,
        federation.routing_rules,
        federation.collection_schemas,
    )
    if not saved:
        if rollback_snapshot is not None:
            _restore_federation_runtime(federation, rollback_snapshot)
        raise HTTPException(
            status_code=500,
            detail="Configuration could not be persisted; runtime state was rolled back.",
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
    if hasattr(request.state, "auth_actor"):
        return str(request.state.auth_actor)
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


def _is_masked_api_key(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("***")


def _mask_cluster_secrets(clusters_config: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    masked = copy.deepcopy(clusters_config)
    for config in masked.values():
        if "api_key" in config:
            config["api_key"] = _mask_api_key(str(config.get("api_key", "")))
    return masked


def _state_snapshot_from_federation(
    federation: FederationService,
    *,
    include_secrets: bool,
) -> ControlPlaneStateSnapshot:
    clusters_config = copy.deepcopy(federation.clusters_config)
    if not include_secrets:
        clusters_config = _mask_cluster_secrets(clusters_config)

    return ControlPlaneStateSnapshot(
        exported_at=datetime.now(timezone.utc).isoformat(),
        secrets_included=include_secrets,
        federation_clusters_config=clusters_config,
        collection_routing_rules=copy.deepcopy(federation.routing_rules),
        collection_schemas=copy.deepcopy(federation.collection_schemas),
    )


def _snapshot_counts(snapshot: ControlPlaneStateSnapshot) -> Dict[str, int]:
    return {
        "clusters": len(snapshot.federation_clusters_config),
        "routing_rules": len(snapshot.collection_routing_rules),
        "collection_schemas": len(snapshot.collection_schemas),
    }


def _validate_name_map(name: str, values: Dict[str, Any]) -> None:
    for key in values.keys():
        if not re.fullmatch(NAME_PATTERN, key):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {name} name '{key}'.",
            )


def _validation_400(context: str, exc: ValidationError) -> None:
    first = exc.errors()[0] if exc.errors() else {}
    message = first.get("msg", str(exc))
    raise HTTPException(status_code=400, detail=f"Invalid {context}: {message}")


def _validate_import_snapshot(snapshot: ControlPlaneStateSnapshot, *, apply: bool) -> None:
    if snapshot.version != "imposbro.state.v1":
        raise HTTPException(status_code=400, detail="Unsupported state snapshot version.")

    _validate_name_map("cluster", snapshot.federation_clusters_config)
    _validate_name_map("collection", snapshot.collection_routing_rules)
    _validate_name_map("collection", snapshot.collection_schemas)

    cluster_names = set(snapshot.federation_clusters_config.keys())
    for collection, schema_config in snapshot.collection_schemas.items():
        try:
            schema = CollectionSchema.model_validate(schema_config)
        except ValidationError as exc:
            _validation_400(f"schema for '{collection}'", exc)
        if schema.name != collection:
            raise HTTPException(
                status_code=400,
                detail=f"Collection schema name mismatch for '{collection}'.",
            )

    for cluster_name, config in snapshot.federation_clusters_config.items():
        if config.get("name", cluster_name) != cluster_name:
            raise HTTPException(
                status_code=400,
                detail=f"Cluster config name mismatch for '{cluster_name}'.",
            )
        for required in ("host", "api_key"):
            if not str(config.get(required, "")).strip():
                raise HTTPException(
                    status_code=400,
                    detail=f"Cluster '{cluster_name}' is missing '{required}'.",
                )
        if apply and _is_masked_api_key(config.get("api_key")):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Cannot apply a snapshot with masked cluster API keys. "
                    "Export with include_secrets=true or provide raw keys."
                ),
            )

    for collection, rules_config in snapshot.collection_routing_rules.items():
        rules_payload = dict(rules_config)
        rules_payload.setdefault("collection", collection)
        try:
            routing_rules = RoutingRules.model_validate(rules_payload)
        except ValidationError as exc:
            _validation_400(f"routing rules for '{collection}'", exc)
        if routing_rules.collection != collection:
            raise HTTPException(
                status_code=400,
                detail=f"Routing rules collection mismatch for '{collection}'.",
            )
        default_cluster = routing_rules.default_cluster
        if default_cluster != "default" and default_cluster not in cluster_names:
            raise HTTPException(
                status_code=400,
                detail=f"Routing default for '{collection}' references unknown cluster '{default_cluster}'.",
            )
        for rule in routing_rules.rules:
            targets = rule.clusters or [rule.cluster]
            for target in targets:
                if target != "default" and target not in cluster_names:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Routing rule for '{collection}' references unknown "
                            f"cluster '{target}'."
                        ),
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
    collections = len(federation.collection_schemas) if federation else 0
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
    "/state/export",
    summary="Export control-plane state for backup",
    response_model=ControlPlaneStateSnapshot,
)
def export_control_plane_state(
    request: Request,
    include_secrets: bool = Query(
        False,
        description="Include raw federated cluster API keys for restorable backups.",
    ),
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
) -> ControlPlaneStateSnapshot:
    """
    Export the current in-memory control-plane state.

    Secrets are masked by default so operators can inspect or share the snapshot
    safely. Use `include_secrets=true` only when creating a restore-ready backup.
    """
    snapshot = _state_snapshot_from_federation(
        federation,
        include_secrets=include_secrets,
    )
    _record_admin_audit(
        state_manager,
        request,
        action="state_exported",
        resource_type="control_plane_state",
        resource_id="config_v1",
        details={
            **_snapshot_counts(snapshot),
            "secrets_included": include_secrets,
        },
    )
    return snapshot


@router.post(
    "/state/import",
    summary="Validate or import control-plane state",
)
def import_control_plane_state(
    request: Request,
    snapshot: ControlPlaneStateSnapshot,
    apply_changes: bool = Query(
        False,
        alias="apply",
        description="Persist and load this snapshot. Defaults to dry-run validation.",
    ),
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
    notifier: SyncConfigNotifier = Depends(get_config_notifier),
) -> Dict[str, Any]:
    """
    Validate or apply a control-plane state snapshot.

    Dry-run is the default. Applying a snapshot requires raw cluster API keys;
    masked export snapshots are intentionally rejected for restore operations.
    """
    _validate_import_snapshot(snapshot, apply=apply_changes)
    counts = _snapshot_counts(snapshot)
    has_masked_secrets = any(
        _is_masked_api_key(config.get("api_key"))
        for config in snapshot.federation_clusters_config.values()
    )

    if not apply_changes:
        return {
            "status": "ok",
            "dry_run": True,
            "message": "State snapshot is valid.",
            "importable": not has_masked_secrets,
            "counts": counts,
        }

    clusters_config = copy.deepcopy(snapshot.federation_clusters_config)
    routing_rules = copy.deepcopy(snapshot.collection_routing_rules)
    collection_schemas = copy.deepcopy(snapshot.collection_schemas)

    saved = state_manager.save_state(
        clusters_config,
        routing_rules,
        collection_schemas,
    )
    if not saved:
        raise HTTPException(status_code=500, detail="Could not persist imported state.")

    federation.reload_from_state(
        clusters_config,
        routing_rules,
        collection_schemas,
    )
    _notify_config_change(notifier, "state_imported")
    _record_admin_audit(
        state_manager,
        request,
        action="state_imported",
        resource_type="control_plane_state",
        resource_id="config_v1",
        details=counts,
    )
    return {
        "status": "ok",
        "dry_run": False,
        "message": "State snapshot imported.",
        "counts": counts,
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
    rollback_snapshot = _federation_runtime_snapshot(federation)
    try:
        federation.register_cluster(
            name=cluster.name,
            host=cluster.host,
            port=cluster.port,
            api_key=cluster.api_key,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    try:
        created_collections = federation.backfill_collection_schemas(cluster.name)
    except Exception as e:
        logger.error("Failed to backfill schemas on cluster '%s': %s", cluster.name, e)
        _restore_federation_runtime(federation, rollback_snapshot)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to backfill collection schemas on cluster '{cluster.name}': {e}",
        )

    _persist_state_or_500(state_manager, federation, rollback_snapshot)
    _notify_config_change(notifier, f"cluster_registered:{cluster.name}")
    _record_admin_audit(
        state_manager,
        request,
        action="cluster_registered",
        resource_type="cluster",
        resource_id=cluster.name,
        details={
            "host": cluster.host,
            "port": cluster.port,
            "collections_backfilled": len(created_collections),
        },
    )
    return OperationResponse(message=f"Cluster '{cluster.name}' registered.")


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

    rollback_snapshot = _federation_runtime_snapshot(federation)
    try:
        federation.unregister_cluster(cluster_name)
        _persist_state_or_500(state_manager, federation, rollback_snapshot)
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


@router.post(
    "/collections/reconcile",
    summary="Reconcile desired collection schemas across all clusters",
)
def reconcile_collections(
    request: Request,
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
) -> Dict[str, Any]:
    """
    Idempotently create missing desired collections on registered clusters.

    This is useful after operational recovery or when a cluster was restored
    without all schemas. It never deletes collections or modifies stored schemas.
    """
    try:
        report = federation.reconcile_collection_schemas()
    except Exception as e:
        logger.error("Collection schema reconciliation failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    created_count = sum(len(cluster["created"]) for cluster in report.values())
    _record_admin_audit(
        state_manager,
        request,
        action="collections_reconciled",
        resource_type="collection_schema",
        resource_id="*",
        details={
            "clusters_checked": len(report),
            "collections_desired": len(federation.collection_schemas),
            "collections_created": created_count,
        },
    )
    return {
        "status": "ok",
        "message": "Collection schema reconciliation completed.",
        "collections_desired": len(federation.collection_schemas),
        "clusters": report,
    }


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
    stored_schema = federation.collection_schemas.get(collection_name)
    if stored_schema:
        return {
            "fields": stored_schema.get("fields", []),
            **(
                {"default_sorting_field": stored_schema["default_sorting_field"]}
                if stored_schema.get("default_sorting_field")
                else {}
            ),
        }

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
    schema_dict = schema.model_dump(exclude_none=True)

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

    rollback_snapshot = _federation_runtime_snapshot(federation)
    tasks = [
        create_on_cluster(client, name) for name, client in federation.clients.items()
    ]
    await asyncio.gather(*tasks)

    federation.collection_schemas[schema.name] = schema_dict

    # Initialize default routing for this collection
    if schema.name not in federation.routing_rules:
        federation.routing_rules[schema.name] = {
            "rules": [],
            "default_cluster": "default",
        }

    _persist_state_or_500(state_manager, federation, rollback_snapshot)
    _notify_config_change(notifier, f"collection_created:{schema.name}")
    _record_admin_audit(
        state_manager,
        request,
        action="collection_created",
        resource_type="collection",
        resource_id=schema.name,
        details={
            "fields": [field.model_dump(exclude_none=True) for field in schema.fields],
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

    rollback_snapshot = _federation_runtime_snapshot(federation)
    tasks = [delete_on_cluster(client) for client in federation.clients.values()]
    await asyncio.gather(*tasks)

    if collection_name in federation.routing_rules:
        del federation.routing_rules[collection_name]
    federation.collection_schemas.pop(collection_name, None)

    _persist_state_or_500(state_manager, federation, rollback_snapshot)
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
            client.aliases.upsert,
            alias_name,
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
    if collection_name not in federation.collection_schemas and clients:
        try:
            clients[0].collections[collection_name].retrieve()
        except typesense.exceptions.ObjectNotFound:
            raise HTTPException(
                status_code=404,
                detail=f"Cannot set rules for non-existent collection '{collection_name}'.",
            )

    rollback_snapshot = _federation_runtime_snapshot(federation)
    try:
        rules_list = [r.model_dump(exclude_none=True) for r in rules_config.rules]
        federation.set_routing_rules(
            collection=collection_name,
            rules=rules_list,
            default_cluster=rules_config.default_cluster,
        )
        _persist_state_or_500(state_manager, federation, rollback_snapshot)
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
    rollback_snapshot = _federation_runtime_snapshot(federation)
    federation.delete_routing_rules(collection_name)
    _persist_state_or_500(state_manager, federation, rollback_snapshot)
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
