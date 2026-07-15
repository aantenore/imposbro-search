"""
Admin Router for IMPOSBRO Search API.

This module contains all administrative endpoints for managing clusters,
collections, and routing rules. All configuration changes are broadcast
via Redis Pub/Sub for multi-instance synchronization.
"""

import asyncio
import copy
import hashlib
import json
import logging
import re
import typesense
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends, Path, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from typing import Dict, Any, Optional

from constants import NAME_PATTERN
from auth import ensure_enterprise_collection_policy
from control_plane import AuditEvent, StateConflictError
from indexing_events import LatestRepairDraft
from domain.routing_rollout import (
    InvalidRolloutTransition,
    RolloutGateError,
    RolloutPhase,
    RolloutVersionConflict,
    RoutingRollout,
    RoutingRolloutGates,
)
from deps import (
    get_federation_service,
    get_state_manager,
    get_config_notifier,
    get_kafka_service,
    require_admin_backup_key,
    require_admin_internal_key,
    require_admin_read_key,
    require_admin_restore_key,
    require_admin_write_key,
)
from models import (
    Cluster,
    CollectionSchema,
    RoutingPreviewRequest,
    RoutingPreviewResponse,
    RoutingRules,
    RoutingRolloutCreate,
    RoutingRolloutListResponse,
    RoutingRolloutMutationResponse,
    RoutingRolloutTransitionRequest,
    RoutingRolloutView,
    RoutingBackfillStepRequest,
    RoutingBackfillStepResponse,
    RoutingParityVerificationRequest,
    RoutingParityVerificationResponse,
    OperationResponse,
    AuditLogResponse,
    ControlPlaneStateSnapshot,
)
from observability import get_traceparent
from secret_resolver import SecretResolutionError
from services import FederationService, KafkaService, StateManager, SyncConfigNotifier
from services.routing_migration import RoutingMigrationError, RoutingMigrationExecutor
from settings import settings
from structured_logging import redact_text

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin",
    tags=["Administration"],
)


def _notify_config_change(
    notifier: SyncConfigNotifier,
    change_type: str,
    *,
    revision: Optional[int] = None,
):
    """Helper to broadcast config changes to all instances."""
    try:
        notifier.notify(change_type, revision=revision)
    except Exception as e:
        logger.warning("Failed to broadcast config change: %s", e)


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", ""))


def _expected_revision(request: Request, state_manager: StateManager) -> int:
    """Parse an HTTP If-Match revision and fail stale mutations before side effects."""
    value = request.headers.get("If-Match", "").strip()
    if not value:
        if settings.DEPLOYMENT_PROFILE == "enterprise":
            raise HTTPException(
                status_code=428,
                detail="If-Match with the current control-plane revision is required",
                headers={"ETag": f'"{state_manager.current_revision}"'},
            )
        return state_manager.current_revision
    if value.startswith("W/"):
        value = value[2:].strip()
    value = value.strip('"')
    try:
        revision = int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="If-Match must contain a revision") from exc
    if revision < 0:
        raise HTTPException(status_code=400, detail="If-Match revision must be non-negative")
    if revision != state_manager.current_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "control_plane_revision_conflict",
                "expected_revision": revision,
                "current_revision": state_manager.authoritative_revision(),
            },
            headers={"ETag": f'"{state_manager.authoritative_revision()}"'},
        )
    return revision


def _set_revision_headers(response: Response, state_manager: StateManager) -> None:
    response.headers["ETag"] = f'"{state_manager.current_revision}"'
    response.headers["X-Control-Plane-Revision"] = str(state_manager.current_revision)


def _federation_runtime_snapshot(federation: FederationService) -> Dict[str, Any]:
    """Capture mutable runtime federation state before a control-plane mutation."""
    return {
        "revision": getattr(federation, "applied_revision", 0),
        "clusters_config": copy.deepcopy(federation.clusters_config),
        "clients": dict(federation.clients),
        "routing_rules": copy.deepcopy(federation.routing_rules),
        "collection_schemas": copy.deepcopy(federation.collection_schemas),
        "collection_aliases": copy.deepcopy(federation.collection_aliases),
        "routing_rollouts": copy.deepcopy(federation.routing_rollouts),
    }


def _restore_federation_runtime(
    federation: FederationService,
    snapshot: Dict[str, Any],
) -> None:
    """Restore mutable runtime federation state after a failed persistence write."""
    federation.replace_runtime(
        revision=snapshot.get("revision", 0),
        clusters_config=snapshot["clusters_config"],
        clients=snapshot["clients"],
        routing_rules=snapshot["routing_rules"],
        collection_schemas=snapshot["collection_schemas"],
        collection_aliases=snapshot["collection_aliases"],
        routing_rollouts=snapshot["routing_rollouts"],
    )


def _persist_state_or_500(
    state_manager: StateManager,
    federation: FederationService,
    rollback_snapshot: Optional[Dict[str, Any]] = None,
    *,
    request: Request,
    action: str,
    resource_type: str,
    resource_id: str,
    details: Optional[Dict[str, Any]] = None,
    event_type: Optional[str] = None,
    expected_revision: Optional[int] = None,
) -> int:
    """Persist config mutations, rolling runtime state back if persistence fails."""
    audit = AuditEvent(
        actor=_admin_actor_from_request(request),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=_request_id(request),
        details=details or {},
    )
    try:
        save_kwargs = dict(
            audit=audit,
            event_type=event_type or action,
            event_payload={
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
            },
            expected_revision=expected_revision,
        )
        if federation.routing_rollouts:
            save_kwargs["routing_rollouts"] = federation.routing_rollouts
        saved = state_manager.save_state(
            federation.clusters_config,
            federation.routing_rules,
            federation.collection_schemas,
            federation.collection_aliases,
            **save_kwargs,
        )
    except StateConflictError as exc:
        if rollback_snapshot is not None:
            _restore_federation_runtime(federation, rollback_snapshot)
        current_revision = state_manager.authoritative_revision()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "control_plane_revision_conflict",
                "expected_revision": exc.expected_revision,
                "current_revision": current_revision,
            },
            headers={"ETag": f'"{current_revision}"'},
        ) from exc
    if not saved:
        if rollback_snapshot is not None:
            _restore_federation_runtime(federation, rollback_snapshot)
        raise HTTPException(
            status_code=500,
            detail="Configuration could not be persisted; runtime state was rolled back.",
        )
    federation.mark_applied_revision(state_manager.current_revision)
    if not state_manager.transactional:
        _record_admin_audit(
            state_manager,
            request,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
        )
    return state_manager.current_revision


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
        request_id=_request_id(request),
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
    state_manager: StateManager,
    *,
    include_secrets: bool,
) -> ControlPlaneStateSnapshot:
    clusters_config = {
        name: FederationService.normalize_cluster_config(config, name=name)
        for name, config in federation.clusters_config.items()
    }
    if not include_secrets:
        clusters_config = _mask_cluster_secrets(clusters_config)

    return ControlPlaneStateSnapshot(
        exported_at=datetime.now(timezone.utc).isoformat(),
        revision=state_manager.current_revision,
        state_digest=state_manager.state_digest,
        secrets_included=include_secrets,
        federation_clusters_config=clusters_config,
        collection_routing_rules=copy.deepcopy(federation.routing_rules),
        collection_schemas=copy.deepcopy(federation.collection_schemas),
        collection_aliases=copy.deepcopy(federation.collection_aliases),
        routing_rollouts=copy.deepcopy(federation.routing_rollouts),
    )


def _alias_count(snapshot: ControlPlaneStateSnapshot) -> int:
    return sum(len(aliases) for aliases in snapshot.collection_aliases.values())


def _snapshot_counts(snapshot: ControlPlaneStateSnapshot) -> Dict[str, int]:
    return {
        "clusters": len(snapshot.federation_clusters_config),
        "routing_rules": len(snapshot.collection_routing_rules),
        "collection_schemas": len(snapshot.collection_schemas),
        "collection_aliases": _alias_count(snapshot),
        "routing_rollouts": len(snapshot.routing_rollouts),
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


def _validate_import_snapshot(
    snapshot: ControlPlaneStateSnapshot,
    *,
    apply: bool,
    federation: Optional[FederationService] = None,
) -> None:
    if snapshot.version not in {"imposbro.state.v1", "imposbro.state.v2"}:
        raise HTTPException(status_code=400, detail="Unsupported state snapshot version.")

    _validate_name_map("cluster", snapshot.federation_clusters_config)
    _validate_name_map("collection", snapshot.collection_routing_rules)
    _validate_name_map("collection", snapshot.collection_schemas)
    _validate_name_map("cluster", snapshot.collection_aliases)
    _validate_name_map("routing rollout", snapshot.routing_rollouts)

    cluster_names = set(snapshot.federation_clusters_config.keys())
    for cluster_name, aliases in snapshot.collection_aliases.items():
        if cluster_name != "default" and cluster_name not in cluster_names:
            raise HTTPException(
                status_code=400,
                detail=f"Aliases reference unknown cluster '{cluster_name}'.",
            )
        _validate_name_map("alias", aliases)
        for alias_name, alias_config in aliases.items():
            collection_name = str(alias_config.get("collection_name", "")).strip()
            if not collection_name or not re.fullmatch(NAME_PATTERN, collection_name):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Alias '{alias_name}' on cluster '{cluster_name}' "
                        "has an invalid collection_name."
                    ),
                )

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
        try:
            cluster = Cluster.model_validate({"name": cluster_name, **config})
        except ValidationError as exc:
            _validation_400(f"cluster config for '{cluster_name}'", exc)
        for required in ("host",):
            if not str(config.get(required, "")).strip():
                raise HTTPException(
                    status_code=400,
                    detail=f"Cluster '{cluster_name}' is missing '{required}'.",
                )
        if cluster.api_key and settings.DEPLOYMENT_PROFILE != "development":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Inline cluster API keys cannot be imported outside the "
                    "development profile; use api_key_ref."
                ),
            )
        if apply and _is_masked_api_key(config.get("api_key")):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Cannot apply a snapshot with masked cluster API keys. "
                    "Export with include_secrets=true or provide raw keys."
                ),
            )
        if apply and federation is not None:
            try:
                federation.materialize_cluster_config(config)
            except SecretResolutionError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Cluster '{cluster_name}' secret reference cannot be resolved."
                    ),
                ) from exc

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

    for rollout_id, rollout_payload in snapshot.routing_rollouts.items():
        try:
            rollout = RoutingRollout.from_dict(rollout_payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid routing rollout '{rollout_id}': {exc}",
            ) from exc
        if rollout.rollout_id != rollout_id:
            raise HTTPException(
                status_code=400,
                detail=f"Routing rollout id mismatch for '{rollout_id}'.",
            )
        if rollout.collection not in snapshot.collection_routing_rules:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Routing rollout '{rollout_id}' references unknown collection "
                    f"'{rollout.collection}'."
                ),
            )
        for label, policy in (
            ("active", rollout.active_policy),
            ("candidate", rollout.candidate_policy),
        ):
            payload = dict(policy)
            payload.setdefault("collection", rollout.collection)
            try:
                RoutingRules.model_validate(payload)
            except ValidationError as exc:
                _validation_400(
                    f"{label} policy for routing rollout '{rollout_id}'",
                    exc,
                )


def _restore_collection_aliases(
    federation: FederationService,
    collection_aliases: Dict[str, Dict[str, Dict[str, str]]],
) -> None:
    """Apply desired alias bindings to their configured Typesense clusters."""
    for cluster_name, aliases in collection_aliases.items():
        client = federation.get_client_for_cluster(cluster_name)
        if not client:
            raise HTTPException(
                status_code=500,
                detail=f"Cannot restore aliases: cluster '{cluster_name}' is unavailable.",
            )
        for alias_name, alias_config in aliases.items():
            collection_name = alias_config["collection_name"]
            try:
                client.aliases.upsert(
                    alias_name,
                    {"collection_name": collection_name},
                )
            except Exception as exc:
                logger.error(
                    "Failed to restore alias '%s' on cluster '%s': %s",
                    alias_name,
                    cluster_name,
                    exc,
                )
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"Could not restore alias '{alias_name}' on cluster "
                        f"'{cluster_name}': {exc}"
                    ),
                )


def _resolved_alias_cluster_name(
    federation: FederationService,
    cluster_name: str,
) -> str:
    resolver = getattr(federation, "resolve_cluster_name", None)
    if callable(resolver):
        resolved = resolver(cluster_name)
        if isinstance(resolved, str) and resolved:
            return resolved
    return cluster_name


# ----- Cluster Management -----


@router.get(
    "/stats",
    summary="Metrics summary for dashboard",
    dependencies=[Depends(require_admin_read_key)],
)
def get_admin_stats(
    response: Response,
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
) -> Dict[str, Any]:
    """
    Return a JSON summary of key metrics for the Admin UI dashboard.
    For full Prometheus metrics use GET /metrics.
    """
    clusters = len(federation.clients) if federation else 0
    collections = len(federation.collection_schemas) if federation else 0
    _set_revision_headers(response, state_manager)
    return {
        "clusters": clusters,
        "collections": collections,
        "metrics_url": "/metrics",
        "control_plane_revision": state_manager.current_revision,
        "control_plane_state_digest": state_manager.state_digest,
        "routing_rollouts": len(federation.routing_rollouts),
        "routing_rollouts_active": sum(
            1
            for payload in federation.routing_rollouts.values()
            if payload.get("phase")
            not in {
                RolloutPhase.COMPLETED.value,
                RolloutPhase.CANCELLED.value,
                RolloutPhase.ROLLED_BACK.value,
            }
        ),
    }


@router.get(
    "/federation/clusters",
    summary="List all registered clusters",
    dependencies=[Depends(require_admin_read_key)],
)
def get_all_clusters(
    response: Response,
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
) -> Dict[str, Any]:
    """
    Retrieve all registered federation clusters.

    Returns a dictionary of cluster configurations including a virtual
    'default' entry representing the internal HA cluster.
    API keys are masked in the response for security.
    """
    _set_revision_headers(response, state_manager)
    display_config = {}
    for name, cfg in federation.clusters_config.items():
        normalized = FederationService.normalize_cluster_config(cfg, name=name)
        display_config[name] = dict(normalized)
        if "api_key" in normalized:
            display_config[name]["api_key"] = _mask_api_key(
                normalized.get("api_key", "")
            )
    display_config["default"] = {
        "name": "default",
        "host": "Internal HA Cluster",
        "port": 8108,
        "protocol": settings.INTERNAL_STATE_PROTOCOL,
        "api_key": "N/A",
    }
    return display_config


@router.get(
    "/audit-log",
    summary="List recent admin audit events",
    response_model=AuditLogResponse,
    dependencies=[Depends(require_admin_read_key)],
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
    "/audit-log/export",
    summary="Export a verified page of the tamper-evident audit chain",
    dependencies=[Depends(require_admin_backup_key)],
)
def export_audit_log(
    after_sequence: int = Query(0, ge=0),
    limit: int = Query(
        min(1000, settings.AUDIT_EXPORT_MAX_RESULTS),
        ge=1,
        le=settings.AUDIT_EXPORT_MAX_RESULTS,
    ),
    state_manager: StateManager = Depends(get_state_manager),
) -> JSONResponse:
    """Provide SIEM/archival pull pages only after verifying the full chain."""
    entries = state_manager.export_admin_audit(
        after_sequence=after_sequence,
        limit=limit,
    )
    encoded_entries = jsonable_encoder(entries)
    digest_material = json.dumps(
        encoded_entries,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    page_digest = "sha256:" + hashlib.sha256(digest_material).hexdigest()
    next_sequence = (
        int(encoded_entries[-1]["sequence"])
        if encoded_entries
        else after_sequence
    )
    return JSONResponse(
        {
            "format": "imposbro.audit.v1",
            "chain_verified": True,
            "after_sequence": after_sequence,
            "next_after_sequence": next_sequence,
            "has_more": len(encoded_entries) == limit,
            "retention_days": settings.AUDIT_RETENTION_DAYS,
            "page_digest": page_digest,
            "entries": encoded_entries,
        },
        headers={
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "X-Audit-Page-Digest": page_digest,
        },
    )


@router.get(
    "/state/export",
    summary="Export control-plane state for backup",
    response_model=ControlPlaneStateSnapshot,
    dependencies=[Depends(require_admin_backup_key)],
)
def export_control_plane_state(
    request: Request,
    response: Response,
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
    no_store_headers = {
        "Cache-Control": "no-store, private",
        "Pragma": "no-cache",
    }
    if include_secrets and settings.DEPLOYMENT_PROFILE != "development":
        raise HTTPException(
            status_code=403,
            detail=(
                "Plaintext secret export is disabled outside the development "
                "profile. Use secret references and an encrypted backup workflow."
            ),
            headers=no_store_headers,
        )
    for name, value in no_store_headers.items():
        response.headers[name] = value

    snapshot = _state_snapshot_from_federation(
        federation,
        state_manager,
        include_secrets=include_secrets,
    )
    _set_revision_headers(response, state_manager)
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
    dependencies=[Depends(require_admin_restore_key)],
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
    _validate_import_snapshot(
        snapshot,
        apply=apply_changes,
        federation=federation,
    )
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

    expected_revision = _expected_revision(request, state_manager)

    clusters_config = {
        name: FederationService.normalize_cluster_config(config, name=name)
        for name, config in snapshot.federation_clusters_config.items()
    }
    routing_rules = copy.deepcopy(snapshot.collection_routing_rules)
    collection_schemas = copy.deepcopy(snapshot.collection_schemas)
    collection_aliases = copy.deepcopy(snapshot.collection_aliases)
    routing_rollouts = copy.deepcopy(snapshot.routing_rollouts)

    audit = AuditEvent(
        actor=_admin_actor_from_request(request),
        action="state_imported",
        resource_type="control_plane_state",
        resource_id="current",
        request_id=_request_id(request),
        details=counts,
    )
    try:
        saved = state_manager.save_state(
            clusters_config,
            routing_rules,
            collection_schemas,
            collection_aliases,
            routing_rollouts,
            audit=audit,
            event_type="state.imported",
            event_payload={"counts": counts},
            expected_revision=expected_revision,
        )
    except StateConflictError as exc:
        current_revision = state_manager.authoritative_revision()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "control_plane_revision_conflict",
                "expected_revision": exc.expected_revision,
                "current_revision": current_revision,
            },
            headers={"ETag": f'"{current_revision}"'},
        ) from exc
    if not saved:
        raise HTTPException(status_code=500, detail="Could not persist imported state.")

    federation.reload_from_state(
        clusters_config,
        routing_rules,
        collection_schemas,
        collection_aliases,
        routing_rollouts,
        revision=state_manager.current_revision,
    )
    if collection_aliases:
        federation.reconcile_collection_schemas()
    _restore_collection_aliases(federation, collection_aliases)
    _notify_config_change(
        notifier,
        "state_imported",
        revision=state_manager.current_revision,
    )
    if not state_manager.transactional:
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
        "revision": state_manager.current_revision,
    }


@router.get(
    "/federation/clusters/internal",
    summary="List raw cluster config for internal services",
    include_in_schema=False,
    dependencies=[Depends(require_admin_internal_key)],
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
    try:
        return {
            name: federation.materialize_cluster_config({**cfg, "name": name})
            for name, cfg in federation.clusters_config.items()
        }
    except SecretResolutionError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "cluster_secret_unavailable",
                "message": "A cluster credential is unavailable.",
            },
            headers={"Retry-After": "5"},
        ) from exc


@router.post(
    "/federation/clusters",
    status_code=201,
    summary="Register a new cluster",
    dependencies=[Depends(require_admin_write_key)],
)
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
    if cluster.api_key and settings.DEPLOYMENT_PROFILE != "development":
        raise HTTPException(
            status_code=400,
            detail=(
                "Inline cluster API keys are allowed only in the development "
                "profile; use api_key_ref."
            ),
        )
    expected_revision = _expected_revision(request, state_manager)
    rollback_snapshot = _federation_runtime_snapshot(federation)
    try:
        federation.register_cluster(
            name=cluster.name,
            host=cluster.host,
            port=cluster.port,
            api_key=cluster.api_key,
            protocol=cluster.protocol,
            api_key_ref=cluster.api_key_ref or None,
        )
    except SecretResolutionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    revision = _persist_state_or_500(
        state_manager,
        federation,
        rollback_snapshot,
        request=request,
        action="cluster_registered",
        resource_type="cluster",
        resource_id=cluster.name,
        details={
            "host": cluster.host,
            "port": cluster.port,
            "protocol": cluster.protocol,
            "collections_desired": len(federation.collection_schemas),
        },
        expected_revision=expected_revision,
    )
    _notify_config_change(
        notifier,
        f"cluster_registered:{cluster.name}",
        revision=revision,
    )
    try:
        created_collections = federation.backfill_collection_schemas(cluster.name)
    except Exception as e:
        logger.error("Failed to backfill schemas on cluster '%s': %s", cluster.name, e)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "cluster_reconciliation_pending",
                "message": (
                    f"Cluster '{cluster.name}' is committed but schema reconciliation "
                    "must be retried."
                ),
                "revision": revision,
            },
            headers={"Retry-After": "10", "ETag": f'"{revision}"'},
        )
    return OperationResponse(
        message=(
            f"Cluster '{cluster.name}' registered; "
            f"{len(created_collections)} schema(s) reconciled."
        ),
        revision=revision,
    )


@router.delete(
    "/federation/clusters/{cluster_name}",
    summary="Remove a cluster",
    dependencies=[Depends(require_admin_write_key)],
)
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

    expected_revision = _expected_revision(request, state_manager)
    rollback_snapshot = _federation_runtime_snapshot(federation)
    try:
        federation.unregister_cluster(cluster_name)
        revision = _persist_state_or_500(
            state_manager,
            federation,
            rollback_snapshot,
            request=request,
            action="cluster_deleted",
            resource_type="cluster",
            resource_id=cluster_name,
            expected_revision=expected_revision,
        )
        _notify_config_change(
            notifier,
            f"cluster_deleted:{cluster_name}",
            revision=revision,
        )
        return OperationResponse(
            message=f"Cluster '{cluster_name}' deleted.", revision=revision
        )
    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))


# ----- Collection Management -----


@router.post(
    "/collections/reconcile",
    summary="Reconcile desired collection schemas across all clusters",
    dependencies=[Depends(require_admin_write_key)],
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
    dependencies=[Depends(require_admin_read_key)],
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


@router.post(
    "/collections",
    status_code=201,
    summary="Create a collection",
    dependencies=[Depends(require_admin_write_key)],
)
async def create_collection(
    request: Request,
    response: Response,
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
    ensure_enterprise_collection_policy(schema.name)
    expected_revision = _expected_revision(request, state_manager)
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
    federation.collection_schemas[schema.name] = schema_dict

    # Initialize default routing for this collection
    if (
        schema.name not in federation.routing_rules
        or federation.routing_rules[schema.name].get("disabled") is True
    ):
        federation.routing_rules[schema.name] = {
            "collection": schema.name,
            "rules": [],
            "default_cluster": "default",
        }

    details = {
        "fields": [field.model_dump(exclude_none=True) for field in schema.fields],
        "default_sorting_field": schema.default_sorting_field,
        "cluster_count": len(federation.clients),
    }
    revision = _persist_state_or_500(
        state_manager,
        federation,
        rollback_snapshot,
        request=request,
        action="collection_created",
        resource_type="collection",
        resource_id=schema.name,
        details=details,
        expected_revision=expected_revision,
    )
    _notify_config_change(
        notifier,
        f"collection_created:{schema.name}",
        revision=revision,
    )
    tasks = [
        create_on_cluster(client, name) for name, client in federation.clients.items()
    ]
    try:
        await asyncio.gather(*tasks)
    except HTTPException as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "collection_reconciliation_pending",
                "message": (
                    f"Collection '{schema.name}' is committed as desired state but "
                    "one or more clusters still require reconciliation."
                ),
                "revision": revision,
            },
            headers={"Retry-After": "10", "ETag": f'"{revision}"'},
        ) from exc
    _set_revision_headers(response, state_manager)
    return OperationResponse(
        message="Collection created successfully on all clusters.",
        revision=revision,
    )


@router.delete(
    "/collections/{collection_name}",
    summary="Delete a collection",
    dependencies=[Depends(require_admin_write_key)],
)
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
    expected_revision = _expected_revision(request, state_manager)

    for rollout_payload in federation.routing_rollouts.values():
        if rollout_payload.get("collection") != collection_name:
            continue
        try:
            phase = RolloutPhase(rollout_payload.get("phase"))
        except ValueError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "routing_rollout_state_invalid"},
            ) from exc
        if phase not in _TERMINAL_ROLLOUT_PHASES:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "active_routing_rollout_blocks_collection_delete",
                    "rollout_id": rollout_payload.get("rollout_id"),
                    "phase": phase.value,
                },
            )

    async def delete_on_cluster(client: typesense.Client) -> None:
        try:
            await asyncio.to_thread(client.collections[collection_name].delete)
        except typesense.exceptions.ObjectNotFound:
            pass

    # First commit a durable tombstone. Every replica will stop routing reads,
    # writes, and document deletes before the destructive provider calls begin.
    if federation.routing_rules.get(collection_name, {}).get("disabled") is not True:
        rollback_snapshot = _federation_runtime_snapshot(federation)
        federation.routing_rules[collection_name] = {
            "collection": collection_name,
            "rules": [],
            "default_cluster": "default",
            "disabled": True,
        }
        for aliases in federation.collection_aliases.values():
            for alias_name, alias_config in list(aliases.items()):
                if alias_config.get("collection_name") == collection_name:
                    aliases.pop(alias_name, None)
        deletion_revision = _persist_state_or_500(
            state_manager,
            federation,
            rollback_snapshot,
            request=request,
            action="collection_deletion_started",
            resource_type="collection",
            resource_id=collection_name,
            details={"phase": "tombstoned"},
            expected_revision=expected_revision,
        )
        _notify_config_change(
            notifier,
            f"collection_deletion_started:{collection_name}",
            revision=deletion_revision,
        )
    else:
        deletion_revision = expected_revision

    tasks = [delete_on_cluster(client) for client in federation.clients.values()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    failures = [item for item in results if isinstance(item, Exception)]
    if failures:
        logger.error(
            "Collection deletion remains tombstoned after %s provider failure(s)",
            len(failures),
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "collection_deletion_pending",
                "message": (
                    f"Collection '{collection_name}' is tombstoned; retry provider "
                    "reconciliation before finalization."
                ),
                "revision": deletion_revision,
                "failed_clusters": len(failures),
            },
            headers={"Retry-After": "10", "ETag": f'"{deletion_revision}"'},
        )

    # Keep the tombstone after physical deletion so a stale or partially
    # restored provider copy can never become visible through default routing.
    finalize_snapshot = _federation_runtime_snapshot(federation)
    federation.collection_schemas.pop(collection_name, None)
    for aliases in federation.collection_aliases.values():
        for alias_name, alias_config in list(aliases.items()):
            if alias_config.get("collection_name") == collection_name:
                aliases.pop(alias_name, None)

    revision = _persist_state_or_500(
        state_manager,
        federation,
        finalize_snapshot,
        request=request,
        action="collection_deleted",
        resource_type="collection",
        resource_id=collection_name,
        details={"phase": "provider_deleted"},
        expected_revision=deletion_revision,
    )
    _notify_config_change(
        notifier,
        f"collection_deleted:{collection_name}",
        revision=revision,
    )
    return OperationResponse(
        message=f"Collection '{collection_name}' deleted.", revision=revision
    )


# ----- Collection Aliases (zero-downtime reindexing) -----


@router.put(
    "/aliases/{alias_name}",
    summary="Create or update collection alias",
    dependencies=[Depends(require_admin_write_key)],
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
    notifier: SyncConfigNotifier = Depends(get_config_notifier),
) -> OperationResponse:
    """
    Create or update an alias pointing to a collection (Typesense alias).
    Use for zero-downtime reindexing: point alias to new collection after reindex.
    """
    expected_revision = _expected_revision(request, state_manager)
    client = federation.get_client_for_cluster(cluster_name)
    if not client:
        raise HTTPException(
            status_code=404,
            detail=f"Cluster '{cluster_name}' not found or not available.",
        )
    resolved_cluster_name = _resolved_alias_cluster_name(federation, cluster_name)
    rollback_snapshot = _federation_runtime_snapshot(federation)
    federation.collection_aliases.setdefault(resolved_cluster_name, {})[alias_name] = {
        "collection_name": collection_name
    }
    details = {
        "collection_name": collection_name,
        "cluster_name": resolved_cluster_name,
    }
    revision = _persist_state_or_500(
        state_manager,
        federation,
        rollback_snapshot,
        request=request,
        action="alias_upserted",
        resource_type="alias",
        resource_id=alias_name,
        details=details,
        expected_revision=expected_revision,
    )
    _notify_config_change(
        notifier,
        f"alias_upserted:{resolved_cluster_name}:{alias_name}",
        revision=revision,
    )
    try:
        await asyncio.to_thread(
            client.aliases.upsert,
            alias_name,
            {"collection_name": collection_name},
        )
    except Exception as exc:
        logger.error(
            "Alias %s desired state committed but provider reconciliation failed",
            redact_text(alias_name),
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "alias_reconciliation_pending",
                "message": "Alias desired state is committed and must be reconciled.",
                "revision": revision,
            },
            headers={"Retry-After": "10", "ETag": f'"{revision}"'},
        ) from exc
    return OperationResponse(
        message=f"Alias '{alias_name}' -> '{collection_name}' on cluster '{cluster_name}'.",
        revision=revision,
    )

@router.get(
    "/aliases",
    summary="List aliases on a cluster",
    dependencies=[Depends(require_admin_read_key)],
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
    dependencies=[Depends(require_admin_write_key)],
)
async def delete_alias(
    request: Request,
    alias_name: str = Path(..., pattern=NAME_PATTERN),
    cluster_name: str = Query(
        "default", pattern=NAME_PATTERN, description="Cluster where the alias lives"
    ),
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
    notifier: SyncConfigNotifier = Depends(get_config_notifier),
) -> OperationResponse:
    """Remove an alias from the cluster."""
    expected_revision = _expected_revision(request, state_manager)
    client = federation.get_client_for_cluster(cluster_name)
    if not client:
        raise HTTPException(
            status_code=404,
            detail=f"Cluster '{cluster_name}' not found or not available.",
        )
    resolved_cluster_name = _resolved_alias_cluster_name(federation, cluster_name)
    rollback_snapshot = _federation_runtime_snapshot(federation)
    aliases = federation.collection_aliases.get(resolved_cluster_name)
    if aliases is not None:
        aliases.pop(alias_name, None)
        if not aliases:
            federation.collection_aliases.pop(resolved_cluster_name, None)
    details = {"cluster_name": resolved_cluster_name}
    revision = _persist_state_or_500(
        state_manager,
        federation,
        rollback_snapshot,
        request=request,
        action="alias_deleted",
        resource_type="alias",
        resource_id=alias_name,
        details=details,
        expected_revision=expected_revision,
    )
    _notify_config_change(
        notifier,
        f"alias_deleted:{resolved_cluster_name}:{alias_name}",
        revision=revision,
    )
    try:
        await asyncio.to_thread(client.aliases[alias_name].delete)
    except typesense.exceptions.ObjectNotFound:
        pass
    except Exception as exc:
        logger.error(
            "Alias %s removal desired state committed but provider reconciliation failed",
            redact_text(alias_name),
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "alias_deletion_pending",
                "message": "Alias removal is committed and must be reconciled.",
                "revision": revision,
            },
            headers={"Retry-After": "10", "ETag": f'"{revision}"'},
        ) from exc
    return OperationResponse(
        message=f"Alias '{alias_name}' deleted.", revision=revision
    )


# ----- Routing Rules Management -----


_TERMINAL_ROLLOUT_PHASES = {
    RolloutPhase.COMPLETED,
    RolloutPhase.CANCELLED,
    RolloutPhase.ROLLED_BACK,
}


def _routing_rollout_or_404(
    federation: FederationService,
    rollout_id: str,
) -> RoutingRollout:
    payload = federation.routing_rollouts.get(rollout_id)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "routing_rollout_not_found", "rollout_id": rollout_id},
        )
    try:
        return RoutingRollout.from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        logger.error(
            "Persisted routing rollout %s is invalid: %s",
            redact_text(rollout_id),
            redact_text(exc),
        )
        raise HTTPException(
            status_code=500,
            detail={
                "code": "routing_rollout_state_invalid",
                "rollout_id": rollout_id,
            },
        ) from exc


def _routing_rollout_view(rollout: RoutingRollout) -> RoutingRolloutView:
    return RoutingRolloutView.model_validate(rollout.to_dict())


def _policy_payload(policy: RoutingRules) -> Dict[str, Any]:
    return policy.model_dump(exclude_none=True, exclude_defaults=True)


def _commit_rollout_reconciler_update(
    *,
    request: Request,
    federation: FederationService,
    state_manager: StateManager,
    notifier: SyncConfigNotifier,
    rollout: RoutingRollout,
    updated: RoutingRollout,
    expected_revision: int,
    action: str,
    details: Dict[str, Any],
) -> int:
    rollback_snapshot = _federation_runtime_snapshot(federation)
    next_rollouts = copy.deepcopy(federation.routing_rollouts)
    next_rollouts[rollout.rollout_id] = updated.to_dict()
    federation.replace_runtime(routing_rollouts=next_rollouts)
    revision = _persist_state_or_500(
        state_manager,
        federation,
        rollback_snapshot,
        request=request,
        action=action,
        resource_type="routing_rollout",
        resource_id=rollout.rollout_id,
        details={
            "collection": rollout.collection,
            "phase": rollout.phase.value,
            "rollout_version": updated.version,
            **details,
        },
        event_type=f"routing.rollout.{action}",
        expected_revision=expected_revision,
    )
    _notify_config_change(
        notifier,
        f"{action}:{rollout.rollout_id}",
        revision=revision,
    )
    return revision


@router.get(
    "/routing-map",
    summary="Get complete routing configuration",
    dependencies=[Depends(require_admin_read_key)],
)
def get_routing_map(
    response: Response,
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
) -> Dict[str, Any]:
    """
    Get the complete routing configuration including all clusters and rules.
    """
    _set_revision_headers(response, state_manager)
    return {
        "revision": state_manager.current_revision,
        "clusters": list(federation.clients.keys()),
        "collections": federation.routing_rules,
        "rollouts": federation.routing_rollouts,
    }


@router.post(
    "/routing-rules/preview",
    response_model=RoutingPreviewResponse,
    summary="Preview routing rules without saving",
    dependencies=[Depends(require_admin_read_key)],
)
def preview_routing_rules(
    request: RoutingPreviewRequest,
    federation: FederationService = Depends(get_federation_service),
) -> RoutingPreviewResponse:
    """Dry-run a document against draft or persisted routing rules."""
    rules_config = None
    if request.rules is not None:
        rules_config = {
            "collection": request.collection,
            "rules": [
                rule.model_dump(exclude_none=True, exclude_defaults=True)
                for rule in request.rules
            ],
            "default_cluster": request.default_cluster,
        }
    preview = federation.preview_routing(
        request.collection,
        request.document,
        rules_config=rules_config,
    )
    return RoutingPreviewResponse(**preview)


@router.get(
    "/routing-rollouts",
    response_model=RoutingRolloutListResponse,
    summary="List safe routing rollouts",
    dependencies=[Depends(require_admin_read_key)],
)
def list_routing_rollouts(
    response: Response,
    collection: Optional[str] = Query(None, pattern=NAME_PATTERN),
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
) -> RoutingRolloutListResponse:
    rollouts = [
        _routing_rollout_or_404(federation, rollout_id)
        for rollout_id in federation.routing_rollouts
    ]
    if collection:
        rollouts = [item for item in rollouts if item.collection == collection]
    rollouts.sort(key=lambda item: (item.updated_at, item.rollout_id), reverse=True)
    _set_revision_headers(response, state_manager)
    return RoutingRolloutListResponse(
        rollouts=[_routing_rollout_view(item) for item in rollouts],
        revision=state_manager.current_revision,
    )


@router.get(
    "/routing-rollouts/{rollout_id}",
    response_model=RoutingRolloutView,
    summary="Get a safe routing rollout",
    dependencies=[Depends(require_admin_read_key)],
)
def get_routing_rollout(
    response: Response,
    rollout_id: str = Path(..., pattern=NAME_PATTERN),
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
) -> RoutingRolloutView:
    rollout = _routing_rollout_or_404(federation, rollout_id)
    _set_revision_headers(response, state_manager)
    return _routing_rollout_view(rollout)


@router.post(
    "/routing-rollouts",
    status_code=201,
    response_model=RoutingRolloutMutationResponse,
    summary="Create a draft safe routing rollout",
    dependencies=[Depends(require_admin_write_key)],
)
def create_routing_rollout(
    request: Request,
    response: Response,
    payload: RoutingRolloutCreate,
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
    notifier: SyncConfigNotifier = Depends(get_config_notifier),
) -> RoutingRolloutMutationResponse:
    expected_revision = _expected_revision(request, state_manager)
    candidate_policy = _policy_payload(payload.candidate_policy)
    collection = payload.candidate_policy.collection
    if federation.routing_rules.get(collection, {}).get("disabled") is True:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "collection_tombstoned",
                "message": "Recreate the collection before changing its routing policy.",
            },
        )

    for rollout_id in federation.routing_rollouts:
        existing = _routing_rollout_or_404(federation, rollout_id)
        if (
            existing.collection == collection
            and existing.phase not in _TERMINAL_ROLLOUT_PHASES
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "routing_rollout_already_active",
                    "collection": collection,
                    "rollout_id": existing.rollout_id,
                    "phase": existing.phase.value,
                },
            )

    try:
        federation.validate_routing_policy(
            candidate_policy.get("rules", []),
            candidate_policy.get("default_cluster", "default"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    active_policy = copy.deepcopy(
        federation.routing_rules.get(
            collection,
            {
                "collection": collection,
                "rules": [],
                "default_cluster": "default",
            },
        )
    )
    active_policy.setdefault("collection", collection)
    rollout = RoutingRollout(
        rollout_id=uuid.uuid4().hex,
        collection=collection,
        active_policy=active_policy,
        candidate_policy=candidate_policy,
        created_by=_admin_actor_from_request(request),
        rollback_window_seconds=payload.rollback_window_seconds,
    )
    rollback_snapshot = _federation_runtime_snapshot(federation)
    next_rollouts = copy.deepcopy(federation.routing_rollouts)
    next_rollouts[rollout.rollout_id] = rollout.to_dict()
    federation.replace_runtime(routing_rollouts=next_rollouts)
    revision = _persist_state_or_500(
        state_manager,
        federation,
        rollback_snapshot,
        request=request,
        action="routing_rollout_created",
        resource_type="routing_rollout",
        resource_id=rollout.rollout_id,
        details={
            "collection": collection,
            "phase": rollout.phase.value,
            "rollout_version": rollout.version,
        },
        event_type="routing.rollout.created",
        expected_revision=expected_revision,
    )
    _notify_config_change(
        notifier,
        f"routing_rollout_created:{rollout.rollout_id}",
        revision=revision,
    )
    _set_revision_headers(response, state_manager)
    return RoutingRolloutMutationResponse(
        rollout=_routing_rollout_view(rollout),
        revision=revision,
    )


@router.post(
    "/routing-rollouts/{rollout_id}/transitions",
    response_model=RoutingRolloutMutationResponse,
    summary="Advance or roll back a safe routing rollout",
    dependencies=[Depends(require_admin_write_key)],
)
def transition_routing_rollout(
    request: Request,
    response: Response,
    transition: RoutingRolloutTransitionRequest,
    rollout_id: str = Path(..., pattern=NAME_PATTERN),
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
    kafka: KafkaService = Depends(get_kafka_service),
    notifier: SyncConfigNotifier = Depends(get_config_notifier),
) -> RoutingRolloutMutationResponse:
    expected_revision = _expected_revision(request, state_manager)
    rollout = _routing_rollout_or_404(federation, rollout_id)
    if transition.checkpoint is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "routing_rollout_checkpoint_read_only",
                "message": "Backfill checkpoints are written only by the reconciler.",
            },
        )
    gates = None
    if transition.gates is not None:
        requested_gates = RoutingRolloutGates(**transition.gates.model_dump())
        if transition.target_phase != RolloutPhase.DUAL_WRITE:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "routing_rollout_evidence_read_only",
                    "message": (
                        "Backfill, parity, lag, and DLQ evidence are machine-owned."
                    ),
                },
            )
        if any(
            (
                requested_gates.backfill_complete,
                bool(requested_gates.source_barrier),
                requested_gates.parity_passed,
                bool(requested_gates.parity_digest),
                requested_gates.unresolved_dlq,
                requested_gates.kafka_lag,
                requested_gates.max_kafka_lag,
            )
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "routing_rollout_evidence_read_only",
                    "message": "Only validation and capacity attestations are operator input.",
                },
            )
        gates = RoutingRolloutGates(
            **{
                **rollout.gates.__dict__,
                "validation_passed": requested_gates.validation_passed,
                "capacity_passed": requested_gates.capacity_passed,
            }
        )
    if transition.target_phase in {RolloutPhase.CUTOVER, RolloutPhase.COMPLETED}:
        kafka = _durable_kafka_or_503(kafka)
        try:
            evidence = kafka.operational_evidence(
                collection_name=rollout.collection,
                consumer_group_id=settings.INDEXING_KAFKA_CONSUMER_GROUP_ID,
                dlq_group_id=settings.INDEXING_KAFKA_DLQ_GROUP_ID,
                timeout_ms=settings.KAFKA_OPERATION_TIMEOUT_MS,
            )
            pending_producer_events = kafka.event_store.pending_count()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "routing_operational_evidence_unavailable",
                    "message": "Kafka lag and DLQ evidence could not be measured.",
                },
            ) from exc
        gates = RoutingRolloutGates(
            **{
                **rollout.gates.__dict__,
                "unresolved_dlq": evidence.unresolved_dlq,
                "kafka_lag": evidence.consumer_lag + pending_producer_events,
                "max_kafka_lag": settings.ROUTING_CUTOVER_MAX_KAFKA_LAG,
            }
        )
    try:
        updated = rollout.transition(
            transition.target_phase,
            expected_version=transition.expected_version,
            gates=gates,
            failure_reason=transition.failure_reason,
        )
    except RolloutVersionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "routing_rollout_version_conflict",
                "expected_version": exc.expected_version,
                "current_version": exc.actual_version,
            },
        ) from exc
    except InvalidRolloutTransition as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "invalid_routing_rollout_transition", "message": str(exc)},
        ) from exc
    except RolloutGateError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "routing_rollout_gate_failed", "message": str(exc)},
        ) from exc

    rollback_snapshot = _federation_runtime_snapshot(federation)
    next_rollouts = copy.deepcopy(federation.routing_rollouts)
    next_rollouts[rollout_id] = updated.to_dict()
    next_rules = copy.deepcopy(federation.routing_rules)
    if updated.phase == RolloutPhase.COMPLETED:
        next_rules[updated.collection] = copy.deepcopy(updated.candidate_policy)
    federation.replace_runtime(
        routing_rules=next_rules,
        routing_rollouts=next_rollouts,
    )
    revision = _persist_state_or_500(
        state_manager,
        federation,
        rollback_snapshot,
        request=request,
        action="routing_rollout_transitioned",
        resource_type="routing_rollout",
        resource_id=rollout_id,
        details={
            "collection": updated.collection,
            "from_phase": rollout.phase.value,
            "to_phase": updated.phase.value,
            "rollout_version": updated.version,
        },
        event_type="routing.rollout.transitioned",
        expected_revision=expected_revision,
    )
    _notify_config_change(
        notifier,
        f"routing_rollout_transitioned:{rollout_id}",
        revision=revision,
    )
    _set_revision_headers(response, state_manager)
    return RoutingRolloutMutationResponse(
        rollout=_routing_rollout_view(updated),
        revision=revision,
    )


def _durable_kafka_or_503(kafka: KafkaService) -> KafkaService:
    if not isinstance(kafka, KafkaService) or kafka.event_store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "durable_indexing_store_required",
                "message": "Routing migration requires the durable indexing event store.",
            },
        )
    return kafka


@router.post(
    "/routing-rollouts/{rollout_id}/backfill/steps",
    response_model=RoutingBackfillStepResponse,
    summary="Run one durable, restart-safe routing backfill step",
    dependencies=[Depends(require_admin_write_key)],
)
def run_routing_backfill_step(
    request: Request,
    response: Response,
    payload: RoutingBackfillStepRequest,
    rollout_id: str = Path(..., pattern=NAME_PATTERN),
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
    kafka: KafkaService = Depends(get_kafka_service),
    notifier: SyncConfigNotifier = Depends(get_config_notifier),
) -> RoutingBackfillStepResponse:
    """Copy a bounded chunk, then replay concurrent mutations at higher sequence."""
    expected_revision = _expected_revision(request, state_manager)
    rollout = _routing_rollout_or_404(federation, rollout_id)
    kafka = _durable_kafka_or_503(kafka)
    if payload.expected_version != rollout.version:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "routing_rollout_version_conflict",
                "expected_version": payload.expected_version,
                "current_version": rollout.version,
            },
        )
    if rollout.phase != RolloutPhase.BACKFILL:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "routing_rollout_phase_conflict",
                "message": "Backfill steps require the backfill phase.",
                "current_phase": rollout.phase.value,
            },
        )

    executor = RoutingMigrationExecutor(federation)
    checkpoint = dict(rollout.backfill_checkpoint)
    source_barrier_position = int(
        checkpoint.get("source_barrier_position", kafka.high_water_mark())
    )
    checkpoint["source_barrier_position"] = source_barrier_position
    copied = 0
    source_documents = int(checkpoint.get("source_documents_seen", 0))

    try:
        if not checkpoint.get("raw_copy_complete", False):
            result = executor.run_step(
                rollout,
                checkpoint=checkpoint,
                max_documents=payload.max_documents,
            )
            checkpoint = dict(result.checkpoint)
            checkpoint["source_barrier_position"] = source_barrier_position
            checkpoint["raw_copy_complete"] = result.complete
            copied = result.copied
            source_documents = result.source_documents
            if result.complete:
                through = kafka.high_water_mark()
                checkpoint["repair_through_position"] = through
                checkpoint["repair_cursor_position"] = source_barrier_position
                checkpoint["repair_complete"] = source_barrier_position >= through
        elif not checkpoint.get("repair_complete", False):
            cursor = int(
                checkpoint.get("repair_cursor_position", source_barrier_position)
            )
            through = int(checkpoint["repair_through_position"])
            events = kafka.latest_events_by_identity(
                after_position=cursor,
                through_position=through,
                limit=payload.max_documents,
            )
            candidate_clusters = executor.candidate_cluster_names(rollout)
            repaired = 0
            for event in events:
                identity = event.payload.get("identity", {})
                if identity.get("collection") != rollout.collection:
                    continue
                kafka.publish_latest_repair(
                    LatestRepairDraft(
                        identity_hash=event.identity_hash,
                        idempotency_key=(
                            f"rollout-repair:{rollout.rollout_id}:"
                            f"{through}:{event.identity_hash}"
                        ),
                        target_clusters=candidate_clusters,
                        routing_revision=max(1, federation.applied_revision),
                        rollout_id=rollout.rollout_id,
                        trace={
                            "request_id": _request_id(request),
                            "traceparent": get_traceparent(request),
                        },
                    )
                )
                repaired += 1
            next_cursor = events[-1].global_position if events else through
            checkpoint["repair_cursor_position"] = next_cursor
            checkpoint["repair_events_published"] = int(
                checkpoint.get("repair_events_published", 0)
            ) + repaired
            checkpoint["repair_complete"] = next_cursor >= through
        complete = bool(
            checkpoint.get("raw_copy_complete")
            and checkpoint.get("repair_complete")
        )
        updated = rollout.record_backfill_progress(
            expected_version=payload.expected_version,
            checkpoint=checkpoint,
            source_barrier=f"indexing-outbox:{source_barrier_position}",
            complete=complete,
        )
    except RolloutVersionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "routing_rollout_version_conflict",
                "expected_version": exc.expected_version,
                "current_version": exc.actual_version,
            },
        ) from exc
    except (RoutingMigrationError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "routing_backfill_failed", "message": str(exc)},
        ) from exc

    revision = _commit_rollout_reconciler_update(
        request=request,
        federation=federation,
        state_manager=state_manager,
        notifier=notifier,
        rollout=rollout,
        updated=updated,
        expected_revision=expected_revision,
        action="routing_rollout_backfill_progressed",
        details={
            "copied": copied,
            "raw_copy_complete": bool(checkpoint.get("raw_copy_complete")),
            "repair_complete": bool(checkpoint.get("repair_complete")),
        },
    )
    _set_revision_headers(response, state_manager)
    return RoutingBackfillStepResponse(
        rollout=_routing_rollout_view(updated),
        revision=revision,
        copied=copied,
        raw_copy_complete=bool(checkpoint.get("raw_copy_complete")),
        source_documents=source_documents,
    )


@router.post(
    "/routing-rollouts/{rollout_id}/parity-verifications",
    response_model=RoutingParityVerificationResponse,
    summary="Record exact routing parity at a stable durable event barrier",
    dependencies=[Depends(require_admin_write_key)],
)
def verify_routing_rollout_parity(
    request: Request,
    response: Response,
    payload: RoutingParityVerificationRequest,
    rollout_id: str = Path(..., pattern=NAME_PATTERN),
    federation: FederationService = Depends(get_federation_service),
    state_manager: StateManager = Depends(get_state_manager),
    kafka: KafkaService = Depends(get_kafka_service),
    notifier: SyncConfigNotifier = Depends(get_config_notifier),
) -> RoutingParityVerificationResponse:
    expected_revision = _expected_revision(request, state_manager)
    rollout = _routing_rollout_or_404(federation, rollout_id)
    kafka = _durable_kafka_or_503(kafka)
    if payload.expected_version != rollout.version:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "routing_rollout_version_conflict",
                "expected_version": payload.expected_version,
                "current_version": rollout.version,
            },
        )
    if not rollout.backfill_checkpoint.get("repair_complete"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "routing_backfill_repair_incomplete",
                "message": "Complete snapshot copy and concurrent-event repair first.",
            },
        )

    start_barrier = kafka.high_water_mark()
    pending_before = kafka.event_store.pending_count()
    try:
        report = RoutingMigrationExecutor(federation).verify_parity(
            rollout,
            sample_limit=payload.sample_limit,
        )
    except RoutingMigrationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "routing_parity_failed", "message": str(exc)},
        ) from exc
    end_barrier = kafka.high_water_mark()
    pending_after = kafka.event_store.pending_count()
    stable_barrier = start_barrier == end_barrier
    passed = bool(
        report.passed
        and stable_barrier
        and pending_before == 0
        and pending_after == 0
    )
    checkpoint = {
        **rollout.backfill_checkpoint,
        "verification_start_barrier": start_barrier,
        "verification_end_barrier": end_barrier,
        "verification_pending_before": pending_before,
        "verification_pending_after": pending_after,
        "verification_source_documents": report.source_documents,
        "verification_candidate_documents": report.candidate_documents,
    }
    try:
        updated = rollout.record_parity_evidence(
            expected_version=payload.expected_version,
            passed=passed,
            digest=report.digest if passed else "",
            checkpoint=checkpoint,
        )
    except (RolloutVersionConflict, InvalidRolloutTransition, RolloutGateError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "routing_parity_state_conflict", "message": str(exc)},
        ) from exc
    revision = _commit_rollout_reconciler_update(
        request=request,
        federation=federation,
        state_manager=state_manager,
        notifier=notifier,
        rollout=rollout,
        updated=updated,
        expected_revision=expected_revision,
        action="routing_rollout_parity_recorded",
        details={
            "passed": passed,
            "source_documents": report.source_documents,
            "candidate_documents": report.candidate_documents,
            "stable_barrier": stable_barrier,
        },
    )
    _set_revision_headers(response, state_manager)
    return RoutingParityVerificationResponse(
        rollout=_routing_rollout_view(updated),
        revision=revision,
        passed=passed,
        digest=report.digest if passed else "",
        source_documents=report.source_documents,
        candidate_documents=report.candidate_documents,
        missing_ids=list(report.missing_ids),
        unexpected_ids=list(report.unexpected_ids),
        different_ids=list(report.different_ids),
        verification_start_barrier=start_barrier,
        verification_end_barrier=end_barrier,
    )


@router.post(
    "/routing-rules",
    status_code=201,
    summary="Set routing rules",
    dependencies=[Depends(require_admin_write_key)],
)
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
    expected_revision = _expected_revision(request, state_manager)
    if settings.DEPLOYMENT_PROFILE == "enterprise":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "safe_routing_rollout_required",
                "message": "Create and advance a routing rollout instead.",
            },
        )
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
        rules_list = [
            r.model_dump(exclude_none=True, exclude_defaults=True)
            for r in rules_config.rules
        ]
        federation.set_routing_rules(
            collection=collection_name,
            rules=rules_list,
            default_cluster=rules_config.default_cluster,
        )
        details = {
            "rules_count": len(rules_list),
            "default_cluster": rules_config.default_cluster,
        }
        revision = _persist_state_or_500(
            state_manager,
            federation,
            rollback_snapshot,
            request=request,
            action="routing_updated",
            resource_type="routing_rule",
            resource_id=collection_name,
            details=details,
            expected_revision=expected_revision,
        )
        _notify_config_change(
            notifier,
            f"routing_updated:{collection_name}",
            revision=revision,
        )
        return OperationResponse(
            message=f"Routing rules for '{collection_name}' have been updated.",
            revision=revision,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete(
    "/routing-rules/{collection_name}",
    summary="Delete routing rules",
    dependencies=[Depends(require_admin_write_key)],
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
    expected_revision = _expected_revision(request, state_manager)
    if settings.DEPLOYMENT_PROFILE == "enterprise":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "safe_routing_rollout_required",
                "message": "Create a routing rollout to reset the policy.",
            },
        )
    rollback_snapshot = _federation_runtime_snapshot(federation)
    federation.delete_routing_rules(collection_name)
    revision = _persist_state_or_500(
        state_manager,
        federation,
        rollback_snapshot,
        request=request,
        action="routing_deleted",
        resource_type="routing_rule",
        resource_id=collection_name,
        expected_revision=expected_revision,
    )
    _notify_config_change(
        notifier,
        f"routing_deleted:{collection_name}",
        revision=revision,
    )
    return OperationResponse(
        message=f"Routing rules for '{collection_name}' have been deleted.",
        revision=revision,
    )
