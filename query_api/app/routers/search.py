"""
Search Router for IMPOSBRO Search API.

This module contains endpoints for document ingestion and federated search.
Implements industry-standard scatter-gather search with correct deep pagination.
"""

import asyncio
import copy
import logging
import re
import time
import uuid
from functools import cmp_to_key
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, Response
from typing import Any, Dict, List, Optional, Tuple
from pydantic import ValidationError
from prometheus_client import Counter
import typesense

from indexing_events import IdempotencyConflictError

from auth import (
    authorize_delete_filter,
    authorize_ingest_document,
    authorize_search_request,
    can_read_document,
    event_tenant_identity,
)
from constants import DOCUMENT_ID_PATTERN, NAME_PATTERN
from deps import (
    get_federation_service,
    get_kafka_service,
    require_ingest_collection_api_key,
    require_search_collection_api_key,
)
from models import (
    BatchIngestItemResult,
    BatchIngestRequest,
    BatchIngestResponse,
    DeleteDocumentResponse,
    DocumentResponse,
    IngestResponse,
    IngestEventMetadata,
    SearchRequest,
)
from observability import get_request_id, get_traceparent
from services import FederationService, KafkaService
from settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Search & Ingestion"],
)

# Metrics
documents_ingested = Counter(
    "documents_ingested_total", "Total number of documents ingested.", ["collection"]
)
documents_deleted = Counter(
    "documents_deleted_total",
    "Total number of document delete requests accepted.",
    ["collection"],
)
documents_read = Counter(
    "documents_read_total",
    "Total number of document read/export requests by result.",
    ["collection", "result"],
)

# Configuration for deep pagination
MAX_DEEP_PAGINATION_DOCS = 10000  # Maximum documents to fetch for deep pagination
DEEP_PAGINATION_WARNING_PAGE = 10  # Warn user when page exceeds this
TYPESENSE_MAX_PER_PAGE = 250


SortField = Tuple[str, str]

OPTIONAL_TYPESENSE_SEARCH_PARAMS = (
    "query_by",
    "filter_by",
    "sort_by",
    "vector_query",
    "query_by_weights",
    "include_fields",
    "exclude_fields",
    "highlight_fields",
    "highlight_full_fields",
    "highlight_start_tag",
    "highlight_end_tag",
    "remote_embedding_timeout_ms",
    "remote_embedding_num_tries",
    "limit_hits",
    "search_cutoff_ms",
    "max_candidates",
    "exhaustive_search",
)


def _parse_sort_fields(
    sort_by: Optional[str],
    *,
    has_vector_query: bool = False,
) -> List[SortField]:
    """
    Parse the subset of Typesense sort expressions that can be merged globally.

    Complex expressions such as geo/function sorts are intentionally rejected
    instead of returning a silently incorrect cross-cluster order.
    """
    fields: List[SortField] = []
    for raw_field in (sort_by or "").split(","):
        raw_field = raw_field.strip()
        if not raw_field:
            continue
        if "(" in raw_field or ")" in raw_field:
            raise ValueError(
                "Federated global sort supports simple field:asc/desc expressions only."
            )
        name, direction = (
            raw_field.rsplit(":", 1) if ":" in raw_field else (raw_field, "desc")
        )
        name = name.strip()
        direction = direction.strip().lower()
        if not name or direction not in {"asc", "desc"}:
            raise ValueError(
                "sort_by must use field:asc or field:desc expressions."
            )
        fields.append((name, direction))

    if not fields:
        if has_vector_query:
            return [("_text_match", "desc"), ("_vector_distance", "asc")]
        return [("_text_match", "desc")]

    has_text_match = any(field in {"_text_match", "text_match"} for field, _ in fields)
    has_vector_distance = any(
        field in {"_vector_distance", "vector_distance"} for field, _ in fields
    )
    if has_vector_query and not has_vector_distance and len(fields) < 3:
        fields.append(("_vector_distance", "asc"))
    if not has_text_match and len(fields) < 3:
        fields.append(("_text_match", "desc"))
    return fields


def _get_document_value(document: Dict[str, Any], path: str) -> Any:
    """Read a possibly nested value from a document using dot notation."""
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _hit_sort_value(hit: Dict[str, Any], field: str) -> Any:
    if field in {"_text_match", "text_match"}:
        return hit.get("text_match")
    if field in {"_vector_distance", "vector_distance"}:
        return hit.get("vector_distance")
    return _get_document_value(hit.get("document", {}), field)


def _compare_scalar(left: Any, right: Any) -> int:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return (left > right) - (left < right)
    left_value = str(left).casefold()
    right_value = str(right).casefold()
    return (left_value > right_value) - (left_value < right_value)


def _compare_hits(left: Dict[str, Any], right: Dict[str, Any], fields: List[SortField]) -> int:
    """Return -1 when left should be ordered before right."""
    for field, direction in fields:
        left_value = _hit_sort_value(left, field)
        right_value = _hit_sort_value(right, field)
        left_missing = left_value is None
        right_missing = right_value is None

        if left_missing and right_missing:
            continue
        if left_missing:
            return 1
        if right_missing:
            return -1

        result = _compare_scalar(left_value, right_value)
        if result:
            return -result if direction == "desc" else result

    left_id = str(left.get("document", {}).get("id", ""))
    right_id = str(right.get("document", {}).get("id", ""))
    return (left_id > right_id) - (left_id < right_id)


def _split_top_level_csv(value: Optional[str]) -> List[str]:
    """Split a Typesense field list without breaking join expressions."""
    if not value:
        return []

    parts: List[str] = []
    current: List[str] = []
    depth = 0
    for character in value:
        if character == "(":
            depth += 1
        elif character == ")" and depth > 0:
            depth -= 1
        if character == "," and depth == 0:
            token = "".join(current).strip()
            if token:
                parts.append(token)
            current = []
            continue
        current.append(character)
    token = "".join(current).strip()
    if token:
        parts.append(token)
    return parts


def _projection_plan(
    request: SearchRequest,
    sort_fields: List[SortField],
) -> Tuple[Optional[str], Optional[str], List[str]]:
    """
    Keep merge keys in shard responses, then hide internal-only fields later.

    Typesense applies include/exclude projections before returning each hit. The
    gateway still needs the document id for deduplication and simple document
    sort fields for a correct global order, so those fields are added only to the
    downstream request and removed again from the public response when needed.
    """
    required_fields = ["id"]
    for field, _direction in sort_fields:
        if field in {
            "_text_match",
            "text_match",
            "_vector_distance",
            "vector_distance",
        }:
            continue
        if field not in required_fields:
            required_fields.append(field)

    include_fields = _split_top_level_csv(request.include_fields)
    exclude_fields = _split_top_level_csv(request.exclude_fields)
    include_set = set(include_fields)
    exclude_set = set(exclude_fields)
    include_all = not include_fields or "*" in include_set
    internal_only_fields: List[str] = []

    for field in required_fields:
        if (not include_all and field not in include_set) or field in exclude_set:
            internal_only_fields.append(field)
        if include_fields and field not in include_set:
            include_fields.append(field)
            include_set.add(field)

    required_set = set(required_fields)
    forwarded_exclude_fields = [
        field for field in exclude_fields if field not in required_set
    ]
    forwarded_include = ",".join(include_fields) if include_fields else None
    forwarded_exclude = (
        ",".join(forwarded_exclude_fields) if forwarded_exclude_fields else None
    )
    return forwarded_include, forwarded_exclude, internal_only_fields


def _build_search_params(
    request: SearchRequest,
    *,
    page: int,
    per_page: int,
    forwarded_sort_by: Optional[str],
    forwarded_include_fields: Optional[str],
    forwarded_exclude_fields: Optional[str],
) -> Dict[str, Any]:
    """Build allowlisted Typesense params for one bounded shard page."""
    params: Dict[str, Any] = {
        "q": request.q,
        "page": page,
        "per_page": per_page,
    }
    for param_name in OPTIONAL_TYPESENSE_SEARCH_PARAMS:
        if param_name in {"sort_by", "include_fields", "exclude_fields"}:
            continue
        value = getattr(request, param_name)
        if isinstance(value, str):
            value = value.strip()
        if value is not None and value != "":
            params[param_name] = value
    if forwarded_sort_by:
        params["sort_by"] = forwarded_sort_by
    if forwarded_include_fields:
        params["include_fields"] = forwarded_include_fields
    if forwarded_exclude_fields:
        params["exclude_fields"] = forwarded_exclude_fields
    return params


def _execute_typesense_search(
    client,
    collection_name: str,
    search_params: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Execute one Typesense search.

    Vector queries are sent via Typesense Multi Search so large vectors travel in
    a POST body instead of a query string between the gateway and data cluster.
    """
    if search_params.get("vector_query") and hasattr(client, "multi_search"):
        result = client.multi_search.perform(
            {"searches": [{"collection": collection_name, **search_params}]}
        )
        results = result.get("results", [])
        if not results:
            return {"found": 0, "hits": []}
        search_result = results[0]
        error_code = int(search_result.get("code", 0) or 0)
        if search_result.get("error") or error_code >= 400:
            raise RuntimeError(
                "Typesense multi-search shard failed: "
                f"{search_result.get('error', f'HTTP {error_code}')}"
            )
        return search_result
    return client.collections[collection_name].documents.search(search_params)


def _execute_typesense_search_window(
    client,
    collection_name: str,
    request: SearchRequest,
    total_needed: int,
    forwarded_sort_by: Optional[str],
    forwarded_include_fields: Optional[str],
    forwarded_exclude_fields: Optional[str],
) -> Dict[str, Any]:
    """Fetch a globally mergeable window without exceeding Typesense's page cap."""
    per_page = min(total_needed, TYPESENSE_MAX_PER_PAGE)
    hits: List[Dict[str, Any]] = []
    total_found: Optional[int] = None
    page = 1

    while len(hits) < total_needed:
        search_params = _build_search_params(
            request,
            page=page,
            per_page=per_page,
            forwarded_sort_by=forwarded_sort_by,
            forwarded_include_fields=forwarded_include_fields,
            forwarded_exclude_fields=forwarded_exclude_fields,
        )
        result = _execute_typesense_search(client, collection_name, search_params)
        if total_found is None:
            total_found = int(result.get("found", 0) or 0)
        page_hits = result.get("hits", []) or []
        hits.extend(page_hits)

        if (
            not page_hits
            or len(page_hits) < per_page
            or len(hits) >= total_found
        ):
            break
        page += 1

    return {
        "found": total_found or 0,
        "hits": hits[:total_needed],
    }


def _remove_document_path(document: Dict[str, Any], path: str) -> None:
    """Remove a possibly nested field from a copied result document."""
    parts = path.split(".")
    current: Any = document
    for part in parts[:-1]:
        if not isinstance(current, dict):
            return
        current = current.get(part)
    if isinstance(current, dict):
        current.pop(parts[-1], None)


def _hide_internal_projection_fields(
    hit: Dict[str, Any],
    internal_only_fields: List[str],
) -> Dict[str, Any]:
    if not internal_only_fields:
        return hit
    projected_hit = dict(hit)
    projected_document = copy.deepcopy(hit.get("document", {}))
    for field in internal_only_fields:
        _remove_document_path(projected_document, field)
    projected_hit["document"] = projected_document
    return projected_hit


def _publish_ingest_document(
    *,
    request: Request,
    collection_name: str,
    document: Dict[str, Any],
    federation: FederationService,
    kafka: KafkaService,
    request_id: str,
    event_metadata: Optional[IngestEventMetadata] = None,
) -> Tuple[str, str]:
    doc_id = document.get("id")
    if not isinstance(doc_id, str) or not re.fullmatch(DOCUMENT_ID_PATTERN, doc_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "Document 'id' must be a string containing 1-256 letters, numbers, "
                "underscore, dot, or hyphen characters."
            ),
        )

    document = authorize_ingest_document(request, collection_name, document)
    if isinstance(federation, FederationService):
        targets, routing_revision, rollout_id = federation.get_indexing_route(
            collection_name,
            document,
        )
    else:
        targets = federation.get_targets_for_document(collection_name, document)
        routing_revision = int(getattr(federation, "applied_revision", 0) or 0)
        rollout_id = None
    if not targets:
        raise HTTPException(
            status_code=503,
            detail=(
                "No target cluster available for document. "
                "Ensure at least one data cluster is registered and routing is configured."
            ),
        )

    document_version, sequence, idempotency_key = _indexing_event_metadata(
        request,
        event_metadata,
        durable=isinstance(kafka, KafkaService) and kafka.durable_events,
    )
    kafka.publish_document(
        collection_name=collection_name,
        document=document,
        target_clusters=[name for _, name in targets],
        tenant_id=event_tenant_identity(
            request,
            collection_name,
            document,
        ),
        document_version=document_version,
        sequence=sequence,
        routing_revision=max(1, routing_revision),
        rollout_id=rollout_id,
        idempotency_key=idempotency_key,
        request_id=request_id,
        traceparent=get_traceparent(request),
    )
    routed_to = ",".join(name for _, name in targets)
    documents_ingested.labels(collection=collection_name).inc()
    return str(doc_id), routed_to


def _positive_event_integer(raw_value: Any, field_name: str) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be a positive integer",
        ) from exc
    if value < 1 or value > (1 << 63) - 1:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be an integer between 1 and INT64_MAX",
        )
    return value


def _indexing_event_metadata(
    request: Request,
    supplied: Optional[IngestEventMetadata] = None,
    *,
    durable: bool = False,
) -> Tuple[int, int, str]:
    """Resolve caller-owned ordering metadata, failing closed in enterprise."""
    raw_version = (
        supplied.document_version
        if supplied is not None
        else request.headers.get(settings.DOCUMENT_VERSION_HEADER)
    )
    raw_sequence = (
        supplied.sequence
        if supplied is not None
        else request.headers.get(settings.EVENT_SEQUENCE_HEADER)
    )
    idempotency_key = (
        supplied.idempotency_key
        if supplied is not None
        else request.headers.get(settings.IDEMPOTENCY_KEY_HEADER, "").strip()
    )
    if raw_version in (None, ""):
        if settings.DEPLOYMENT_PROFILE == "enterprise" and not durable:
            raise HTTPException(
                status_code=428,
                detail={
                    "code": "document_version_required",
                    "header": settings.DOCUMENT_VERSION_HEADER,
                },
            )
        raw_version = 1 if durable else time.time_ns()
    version = _positive_event_integer(raw_version, "document_version")
    sequence = _positive_event_integer(
        raw_sequence if raw_sequence not in (None, "") else version,
        "sequence",
    )
    if not idempotency_key:
        if settings.DEPLOYMENT_PROFILE == "enterprise":
            raise HTTPException(
                status_code=428,
                detail={
                    "code": "idempotency_key_required",
                    "header": settings.IDEMPOTENCY_KEY_HEADER,
                },
            )
        idempotency_key = uuid.uuid4().hex
    if (
        len(idempotency_key) < 8
        or len(idempotency_key) > 256
        or any(ord(character) < 32 for character in idempotency_key)
    ):
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key must contain 8-256 printable characters",
        )
    return version, sequence, idempotency_key


async def _perform_federated_search(
    response: Response,
    collection_name: str,
    request: SearchRequest,
    federation: FederationService,
) -> Dict[str, Any]:
    """
    Perform federated scatter-gather search across all relevant clusters.

    Fetches enough hits from each cluster to merge, deduplicate, and paginate in
    the gateway. Supports keyword, semantic, and vector/hybrid Typesense params.
    """
    named_clients = federation.get_named_clients_for_search(collection_name)

    if not named_clients:
        raise HTTPException(
            status_code=404,
            detail=f"Collection '{collection_name}' not found on any registered cluster.",
        )

    has_vector_query = bool(request.vector_query and request.vector_query.strip())
    forwarded_sort_by = request.sort_by
    if not forwarded_sort_by and not has_vector_query:
        stored_schema = getattr(federation, "collection_schemas", {}).get(
            collection_name,
            {},
        )
        default_sorting_field = stored_schema.get("default_sorting_field")
        forwarded_sort_by = "_text_match:desc"
        if default_sorting_field:
            forwarded_sort_by += f",{default_sorting_field}:desc"
    try:
        sort_fields = _parse_sort_fields(
            forwarded_sort_by,
            has_vector_query=has_vector_query,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    (
        forwarded_include_fields,
        forwarded_exclude_fields,
        internal_only_fields,
    ) = _projection_plan(request, sort_fields)

    # Cursor-style: use offset/limit when provided; otherwise page/per_page.
    use_offset = request.offset is not None and request.limit is not None
    if use_offset:
        start_idx = request.offset
        page_size = request.limit
        total_needed = request.offset + request.limit
        effective_page = (request.offset // request.limit) + 1
    else:
        start_idx = (request.page - 1) * request.per_page
        page_size = request.per_page
        total_needed = request.page * request.per_page
        effective_page = request.page

    if total_needed > MAX_DEEP_PAGINATION_DOCS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Requested pagination window ({total_needed}) exceeds the configured "
                f"maximum of {MAX_DEEP_PAGINATION_DOCS} documents."
            ),
        )

    fetch_needed = min(total_needed + 1, MAX_DEEP_PAGINATION_DOCS)
    if fetch_needed == MAX_DEEP_PAGINATION_DOCS:
        response.headers["X-Pagination-Warning"] = (
            f"Deep pagination reached the {MAX_DEEP_PAGINATION_DOCS}-document window."
        )

    if effective_page > DEEP_PAGINATION_WARNING_PAGE:
        response.headers["X-Pagination-Info"] = (
            f"Deep pagination (page {effective_page}) requires fetching {fetch_needed} docs "
            f"from each of {len(named_clients)} clusters. Consider using filters to narrow results."
        )

    async def search_cluster(
        cluster_name: str, client
    ) -> Tuple[str, Optional[Dict], Optional[str]]:
        """Execute search on a single cluster."""
        try:
            result = await asyncio.to_thread(
                _execute_typesense_search_window,
                client,
                collection_name,
                request,
                fetch_needed,
                forwarded_sort_by,
                forwarded_include_fields,
                forwarded_exclude_fields,
            )
            return cluster_name, result, None
        except Exception as e:
            logger.warning("Search failed for cluster %s: %s", cluster_name, e)
            return cluster_name, None, str(e)

    tasks = [
        search_cluster(cluster_name, client)
        for cluster_name, client in named_clients
    ]
    results_list = await asyncio.gather(*tasks)

    all_hits: List[Dict] = []
    total_found = 0
    successful_clusters = 0
    failed_clusters: List[str] = []

    for cluster_name, result, _error in results_list:
        if result is not None:
            all_hits.extend(result.get("hits", []))
            cluster_found = int(result.get("found", 0) or 0)
            total_found += cluster_found
            successful_clusters += 1
        else:
            failed_clusters.append(cluster_name)

    if successful_clusters == 0:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Search failed on all {len(named_clients)} cluster(s) for "
                f"collection '{collection_name}'."
            ),
        )

    unique_hits_map: Dict[str, Dict] = {}
    seen_hit_count_by_id: Dict[str, int] = {}
    for hit in all_hits:
        doc_id = hit.get("document", {}).get("id")
        if not doc_id:
            continue
        seen_hit_count_by_id[doc_id] = seen_hit_count_by_id.get(doc_id, 0) + 1
        existing_hit = unique_hits_map.get(doc_id)
        if existing_hit is None or _compare_hits(hit, existing_hit, sort_fields) < 0:
            unique_hits_map[doc_id] = hit

    sorted_hits = sorted(
        unique_hits_map.values(),
        key=cmp_to_key(lambda left, right: _compare_hits(left, right, sort_fields)),
    )

    end_idx = start_idx + page_size
    page_hits = [
        _hide_internal_projection_fields(hit, internal_only_fields)
        for hit in sorted_hits[start_idx:end_idx]
    ]
    duplicates_seen = any(count > 1 for count in seen_hit_count_by_id.values())
    has_more = end_idx < len(sorted_hits) or total_found > len(all_hits)
    next_offset = None
    if use_offset and has_more:
        candidate_offset = request.offset + request.limit
        if candidate_offset + request.limit <= MAX_DEEP_PAGINATION_DOCS:
            next_offset = candidate_offset
    deduplicated_found = len(sorted_hits)
    found = deduplicated_found if duplicates_seen else total_found
    if duplicates_seen:
        found_relation = "window_lower_bound"
    elif len(named_clients) == 1:
        found_relation = "exact"
    else:
        found_relation = "upper_bound"

    out = {
        "found": found,
        "found_relation": found_relation,
        "page": effective_page,
        "per_page": page_size,
        "hits": page_hits,
        "out_of": len(sorted_hits),
        "raw_found": total_found,
        "deduplicated_found": deduplicated_found,
        "deduplicated_found_window": deduplicated_found,
        "clusters_queried": len(named_clients),
        "clusters_responded": successful_clusters,
        "failed_clusters": failed_clusters,
        "partial": bool(failed_clusters),
        "has_more": has_more,
    }
    if use_offset:
        out["offset"] = request.offset
        out["next_offset"] = next_offset
    return out


@router.post(
    "/ingest/{collection_name}",
    response_model=IngestResponse,
    summary="Ingest a document",
    dependencies=[Depends(require_ingest_collection_api_key)],
)
def ingest_document(
    request: Request,
    collection_name: str = Path(
        ...,
        pattern=NAME_PATTERN,
        description="Collection name",
    ),
    document: Dict[str, Any] = Body(...),
    federation: FederationService = Depends(get_federation_service),
    kafka: KafkaService = Depends(get_kafka_service),
) -> IngestResponse:
    """
    Ingest a document into the federated search system.

    The document is routed to the appropriate cluster based on routing rules
    and published to Kafka for asynchronous indexing.

    Args:
        collection_name: Target collection name
        document: Document to ingest (must have an 'id' field)

    Returns:
        IngestResponse with status and routing information
    """
    request_id = get_request_id(request)
    try:
        doc_id, routed_to = _publish_ingest_document(
            request=request,
            collection_name=collection_name,
            document=document,
            federation=federation,
            kafka=kafka,
            request_id=request_id,
        )
        return IngestResponse(
            status="ok", document_id=doc_id, routed_to=routed_to
        )
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        if isinstance(e, IdempotencyConflictError):
            raise HTTPException(
                status_code=409,
                detail={"code": "idempotency_key_conflict", "message": str(e)},
            ) from e
        logger.error(
            "Failed to publish document to Kafka request_id=%s: %s",
            request_id,
            e,
        )
        raise HTTPException(status_code=500, detail=f"Kafka producer error: {str(e)}")


@router.post(
    "/ingest/{collection_name}/batch",
    response_model=BatchIngestResponse,
    summary="Ingest multiple documents",
    dependencies=[Depends(require_ingest_collection_api_key)],
)
def ingest_documents_batch(
    request: Request,
    collection_name: str = Path(
        ...,
        pattern=NAME_PATTERN,
        description="Collection name",
    ),
    payload: BatchIngestRequest = Body(...),
    federation: FederationService = Depends(get_federation_service),
    kafka: KafkaService = Depends(get_kafka_service),
) -> BatchIngestResponse:
    """
    Ingest multiple documents through the same routing and Kafka path as single ingest.

    Each accepted document becomes one versioned logical event carrying every
    physical target; per-target worker checkpoints make fan-out retries resumable.
    """
    requested = len(payload.documents)
    if requested > settings.INGEST_BATCH_MAX_DOCUMENTS:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Batch contains {requested} documents; "
                f"maximum is {settings.INGEST_BATCH_MAX_DOCUMENTS}."
            ),
        )

    request_id = get_request_id(request)
    items: List[BatchIngestItemResult] = []
    accepted = 0
    for index, document in enumerate(payload.documents):
        document_id = document.get("id")
        try:
            accepted_document_id, routed_to = _publish_ingest_document(
                request=request,
                collection_name=collection_name,
                document=document,
                federation=federation,
                kafka=kafka,
                request_id=request_id,
                event_metadata=payload.event_metadata.get(str(document_id)),
            )
            accepted += 1
            items.append(
                BatchIngestItemResult(
                    index=index,
                    document_id=accepted_document_id,
                    status="ok",
                    routed_to=routed_to,
                )
            )
        except HTTPException as exc:
            items.append(
                BatchIngestItemResult(
                    index=index,
                    document_id=str(document_id) if document_id is not None else None,
                    status="rejected",
                    error=str(exc.detail),
                )
            )
        except Exception as exc:
            logger.error(
                "Failed to publish batch document to Kafka request_id=%s index=%s: %s",
                request_id,
                index,
                exc,
            )
            items.append(
                BatchIngestItemResult(
                    index=index,
                    document_id=str(document_id) if document_id is not None else None,
                    status="rejected",
                    error=(
                        f"Idempotency conflict: {exc}"
                        if isinstance(exc, IdempotencyConflictError)
                        else f"Kafka producer error: {str(exc)}"
                    ),
                )
            )

    rejected = requested - accepted
    status = "ok" if rejected == 0 else "rejected" if accepted == 0 else "partial"
    return BatchIngestResponse(
        status=status,
        requested=requested,
        accepted=accepted,
        rejected=rejected,
        request_id=request_id,
        items=items,
    )


@router.get(
    "/documents/{collection_name}/{document_id}",
    response_model=DocumentResponse,
    summary="Retrieve a document",
    dependencies=[Depends(require_search_collection_api_key)],
)
def get_document(
    request: Request,
    collection_name: str = Path(
        ...,
        pattern=NAME_PATTERN,
        description="Collection name",
    ),
    document_id: str = Path(
        ...,
        pattern=DOCUMENT_ID_PATTERN,
        description="Document id to retrieve",
    ),
    federation: FederationService = Depends(get_federation_service),
) -> DocumentResponse:
    """
    Retrieve one document from any cluster that may contain the collection.

    Tenant policy is enforced after retrieval and unauthorized matches are
    reported as not found so cross-tenant callers do not receive document data.
    """
    targets = federation.get_named_clients_for_search(collection_name)
    if not targets:
        documents_read.labels(collection=collection_name, result="not_found").inc()
        raise HTTPException(
            status_code=404,
            detail=f"Collection '{collection_name}' not found on any registered cluster.",
        )

    failed_clusters: List[str] = []
    for target_cluster_name, client in targets:
        try:
            document = client.collections[collection_name].documents[document_id].retrieve()
        except typesense.exceptions.ObjectNotFound:
            continue
        except Exception as exc:
            failed_clusters.append(target_cluster_name)
            logger.warning(
                "Document read failed for %s/%s on cluster %s request_id=%s: %s",
                collection_name,
                document_id,
                target_cluster_name,
                get_request_id(request),
                exc,
            )
            continue

        if can_read_document(request, collection_name, document):
            documents_read.labels(collection=collection_name, result="found").inc()
            return DocumentResponse(
                status="ok",
                collection=collection_name,
                document_id=document_id,
                found_in=target_cluster_name,
                document=document,
            )

    if failed_clusters:
        documents_read.labels(collection=collection_name, result="failed").inc()
        raise HTTPException(
            status_code=503,
            detail=(
                f"Document lookup failed on cluster(s): {', '.join(failed_clusters)}."
            ),
        )

    documents_read.labels(collection=collection_name, result="not_found").inc()
    raise HTTPException(status_code=404, detail="Document not found.")


@router.delete(
    "/documents/{collection_name}/{document_id}",
    response_model=DeleteDocumentResponse,
    summary="Delete a document asynchronously",
    dependencies=[Depends(require_ingest_collection_api_key)],
)
def delete_document(
    request: Request,
    collection_name: str = Path(
        ...,
        pattern=NAME_PATTERN,
        description="Collection name",
    ),
    document_id: str = Path(
        ...,
        pattern=DOCUMENT_ID_PATTERN,
        description="Document id to delete",
    ),
    federation: FederationService = Depends(get_federation_service),
    kafka: KafkaService = Depends(get_kafka_service),
) -> DeleteDocumentResponse:
    """
    Queue a document deletion across every cluster that may contain the collection.

    Deletion is asynchronous and idempotent. The indexing worker treats missing
    documents as successful no-ops so clients can safely retry requests.
    """
    if isinstance(federation, FederationService):
        targets, routing_revision, rollout_id = federation.get_delete_route(
            collection_name
        )
    else:
        targets = federation.get_named_clients_for_delete(collection_name)
        routing_revision = int(getattr(federation, "applied_revision", 0) or 0)
        rollout_id = None
    if not targets:
        raise HTTPException(
            status_code=503,
            detail=(
                "No target cluster available for document deletion. "
                "Ensure at least one data cluster is registered and routing is configured."
            ),
        )

    request_id = get_request_id(request)
    filter_by = authorize_delete_filter(request, collection_name, document_id)
    document_version, sequence, idempotency_key = _indexing_event_metadata(
        request,
        durable=isinstance(kafka, KafkaService) and kafka.durable_events,
    )
    try:
        kafka.publish_delete_document(
            collection_name=collection_name,
            document_id=document_id,
            target_clusters=[name for name, _ in targets],
            tenant_id=event_tenant_identity(request, collection_name),
            document_version=document_version,
            sequence=sequence,
            routing_revision=max(1, routing_revision),
            rollout_id=rollout_id,
            idempotency_key=idempotency_key,
            request_id=request_id,
            traceparent=get_traceparent(request),
            filter_by=filter_by,
        )
        routed_to = ",".join(name for name, _ in targets)
        documents_deleted.labels(collection=collection_name).inc()
        return DeleteDocumentResponse(
            status="ok",
            document_id=document_id,
            routed_to=routed_to,
        )
    except Exception as e:
        if isinstance(e, IdempotencyConflictError):
            raise HTTPException(
                status_code=409,
                detail={"code": "idempotency_key_conflict", "message": str(e)},
            ) from e
        logger.error(
            "Failed to publish document delete to Kafka request_id=%s: %s",
            request_id,
            e,
        )
        raise HTTPException(status_code=500, detail=f"Kafka producer error: {str(e)}")


@router.get(
    "/search/{collection_name}",
    summary="Federated search",
    dependencies=[Depends(require_search_collection_api_key)],
)
async def search(
    http_request: Request,
    response: Response,
    collection_name: str = Path(..., pattern=NAME_PATTERN, description="Collection name"),
    q: str = Query(..., min_length=1, description="Search query"),
    query_by: Optional[str] = Query(None, description="Comma-separated fields to search"),
    filter_by: Optional[str] = Query(None, description="Filter expression"),
    sort_by: Optional[str] = Query(None, description="Sort expression"),
    vector_query: Optional[str] = Query(None, description="Typesense vector_query"),
    query_by_weights: Optional[str] = Query(None, description="Typesense query_by_weights"),
    include_fields: Optional[str] = Query(None, description="Fields to include"),
    exclude_fields: Optional[str] = Query(None, description="Fields to exclude"),
    highlight_fields: Optional[str] = Query(None, description="Fields to highlight"),
    highlight_full_fields: Optional[str] = Query(None, description="Fields to fully highlight"),
    highlight_start_tag: Optional[str] = Query(None, description="Highlight start tag"),
    highlight_end_tag: Optional[str] = Query(None, description="Highlight end tag"),
    remote_embedding_timeout_ms: Optional[int] = Query(None, ge=1),
    remote_embedding_num_tries: Optional[int] = Query(None, ge=1),
    limit_hits: Optional[int] = Query(None, ge=1),
    search_cutoff_ms: Optional[int] = Query(None, ge=1),
    max_candidates: Optional[int] = Query(None, ge=1),
    exhaustive_search: Optional[bool] = Query(None),
    page: int = Query(1, ge=1, description="Page number (ignored if offset is set)"),
    per_page: int = Query(10, ge=1, le=250, description="Results per page"),
    offset: Optional[int] = Query(None, ge=0, description="Cursor-style offset (use with limit for deep pagination)"),
    limit: Optional[int] = Query(None, ge=1, le=250, description="Page size when using offset"),
    federation: FederationService = Depends(get_federation_service),
) -> Dict[str, Any]:
    """
    Perform a federated search across all relevant clusters.

    This endpoint implements scatter-gather search with CORRECT deep pagination:

    1. Scatter: Calculate total docs needed (page * per_page)
    2. Fetch that many results from EACH cluster (not just the target page)
    3. Gather: Collect all results from all clusters
    4. Merge: Deduplicate by document ID
    5. Sort: Re-rank by relevance score
    6. Slice: Return only the requested page

    This ensures mathematically correct pagination across sharded data.

    Args:
        collection_name: Collection to search
        q: Search query string
        query_by: Fields to search (comma-separated)
        filter_by: Optional filter expression
        sort_by: Optional sort expression
        page: Page number (1-indexed)
        per_page: Results per page (max 250)

    Returns:
        Merged search results from all clusters
    """
    try:
        search_request = SearchRequest(
            q=q,
            query_by=query_by,
            filter_by=filter_by,
            sort_by=sort_by,
            vector_query=vector_query,
            query_by_weights=query_by_weights,
            include_fields=include_fields,
            exclude_fields=exclude_fields,
            highlight_fields=highlight_fields,
            highlight_full_fields=highlight_full_fields,
            highlight_start_tag=highlight_start_tag,
            highlight_end_tag=highlight_end_tag,
            remote_embedding_timeout_ms=remote_embedding_timeout_ms,
            remote_embedding_num_tries=remote_embedding_num_tries,
            limit_hits=limit_hits,
            search_cutoff_ms=search_cutoff_ms,
            max_candidates=max_candidates,
            exhaustive_search=exhaustive_search,
            page=page,
            per_page=per_page,
            offset=offset,
            limit=limit,
        )
    except ValidationError as exc:
        message = exc.errors()[0].get("msg", str(exc)) if exc.errors() else str(exc)
        raise HTTPException(status_code=400, detail=message)
    search_request = authorize_search_request(
        http_request,
        collection_name,
        search_request,
    )
    return await _perform_federated_search(
        response,
        collection_name,
        search_request,
        federation,
    )


@router.post(
    "/search/{collection_name}",
    summary="Federated search with JSON body",
    dependencies=[Depends(require_search_collection_api_key)],
)
async def search_with_body(
    http_request: Request,
    response: Response,
    collection_name: str = Path(..., pattern=NAME_PATTERN, description="Collection name"),
    request: SearchRequest = Body(...),
    federation: FederationService = Depends(get_federation_service),
) -> Dict[str, Any]:
    """
    Perform federated search with a JSON payload.

    Prefer this endpoint for semantic, vector, or hybrid searches where
    `vector_query` can be too long or too sensitive for URL query strings.
    """
    request = authorize_search_request(http_request, collection_name, request)
    return await _perform_federated_search(
        response,
        collection_name,
        request,
        federation,
    )
