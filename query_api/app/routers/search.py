"""
Search Router for IMPOSBRO Search API.

This module contains endpoints for document ingestion and federated search.
Implements industry-standard scatter-gather search with correct deep pagination.
"""

import asyncio
import logging
from functools import cmp_to_key
from fastapi import APIRouter, HTTPException, Query, Depends, Response, Path, Body, Request
from typing import Dict, Any, Optional, List, Tuple
from pydantic import ValidationError
from prometheus_client import Counter

from auth import authorize_ingest_document, authorize_search_request
from constants import NAME_PATTERN
from deps import (
    get_federation_service,
    get_kafka_service,
    require_ingest_collection_api_key,
    require_search_collection_api_key,
)
from models import IngestResponse, SearchRequest
from observability import get_request_id
from services import FederationService, KafkaService

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Search & Ingestion"],
)

# Metrics
documents_ingested = Counter(
    "documents_ingested_total", "Total number of documents ingested.", ["collection"]
)

# Configuration for deep pagination
MAX_DEEP_PAGINATION_DOCS = 10000  # Maximum documents to fetch for deep pagination
DEEP_PAGINATION_WARNING_PAGE = 10  # Warn user when page exceeds this


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


def _build_search_params(request: SearchRequest, total_needed: int) -> Dict[str, Any]:
    """Build the allowlisted Typesense search params for one cluster request."""
    params: Dict[str, Any] = {
        "q": request.q,
        "page": 1,  # Always fetch from page 1 so the gateway can merge globally.
        "per_page": total_needed,
    }
    for param_name in OPTIONAL_TYPESENSE_SEARCH_PARAMS:
        value = getattr(request, param_name)
        if isinstance(value, str):
            value = value.strip()
        if value is not None and value != "":
            params[param_name] = value
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
        return results[0] if results else {"found": 0, "hits": []}
    return client.collections[collection_name].documents.search(search_params)


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
    try:
        sort_fields = _parse_sort_fields(
            request.sort_by,
            has_vector_query=has_vector_query,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

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

    fetch_needed = total_needed + 1
    if fetch_needed > MAX_DEEP_PAGINATION_DOCS:
        fetch_needed = MAX_DEEP_PAGINATION_DOCS
        response.headers["X-Pagination-Warning"] = (
            f"Deep pagination capped at {MAX_DEEP_PAGINATION_DOCS} documents. "
            "Results may be incomplete for very deep pages."
        )

    if request.page > DEEP_PAGINATION_WARNING_PAGE:
        response.headers["X-Pagination-Info"] = (
            f"Deep pagination (page {request.page}) requires fetching {fetch_needed} docs "
            f"from each of {len(named_clients)} clusters. Consider using filters to narrow results."
        )

    search_params = _build_search_params(request, fetch_needed)

    async def search_cluster(
        cluster_name: str, client
    ) -> Tuple[str, Optional[Dict], Optional[str]]:
        """Execute search on a single cluster."""
        try:
            result = await asyncio.to_thread(
                _execute_typesense_search,
                client,
                collection_name,
                search_params,
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
    page_hits = sorted_hits[start_idx:end_idx]
    duplicates_seen = any(count > 1 for count in seen_hit_count_by_id.values())
    has_more = end_idx < len(sorted_hits) or (
        not duplicates_seen and total_found > len(sorted_hits)
    )
    next_offset = request.offset + request.limit if (use_offset and has_more) else None
    deduplicated_found = len(sorted_hits)
    found = deduplicated_found if duplicates_seen else total_found

    out = {
        "found": found,
        "page": effective_page,
        "per_page": page_size,
        "hits": page_hits,
        "out_of": len(sorted_hits),
        "raw_found": total_found,
        "deduplicated_found": deduplicated_found,
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
    collection_name: str = Path(..., pattern=NAME_PATTERN, description="Collection name"),
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
    doc_id = document.get("id")
    if not doc_id:
        raise HTTPException(status_code=400, detail="Document must have an 'id' field.")
    document = authorize_ingest_document(request, collection_name, document)

    # Get all targets (single or fan-out to multiple clusters)
    targets = federation.get_targets_for_document(collection_name, document)

    if not targets:
        raise HTTPException(
            status_code=503,
            detail=(
                "No target cluster available for document. "
                "Ensure at least one data cluster is registered and routing is configured."
            ),
        )

    try:
        for _client, target_cluster_name in targets:
            kafka.publish_document(
                collection_name=collection_name,
                document=document,
                target_cluster=target_cluster_name,
                request_id=get_request_id(request),
            )
        routed_to = ",".join(name for _, name in targets)
        documents_ingested.labels(collection=collection_name).inc()

        return IngestResponse(
            status="ok", document_id=str(doc_id), routed_to=routed_to
        )
    except Exception as e:
        logger.error(
            "Failed to publish document to Kafka request_id=%s: %s",
            get_request_id(request),
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
