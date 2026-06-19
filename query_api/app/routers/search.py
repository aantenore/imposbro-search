"""
Search Router for IMPOSBRO Search API.

This module contains endpoints for document ingestion and federated search.
Implements industry-standard scatter-gather search with correct deep pagination.
"""

import asyncio
import logging
from functools import cmp_to_key
from fastapi import APIRouter, HTTPException, Query, Depends, Response, Path, Body
from typing import Dict, Any, Optional, List, Tuple
from prometheus_client import Counter

from constants import NAME_PATTERN
from deps import get_federation_service, get_kafka_service, require_data_api_key
from models import IngestResponse
from services import FederationService, KafkaService

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Search & Ingestion"],
    dependencies=[Depends(require_data_api_key)],
)

# Metrics
documents_ingested = Counter(
    "documents_ingested_total", "Total number of documents ingested.", ["collection"]
)

# Configuration for deep pagination
MAX_DEEP_PAGINATION_DOCS = 10000  # Maximum documents to fetch for deep pagination
DEEP_PAGINATION_WARNING_PAGE = 10  # Warn user when page exceeds this


SortField = Tuple[str, str]


def _parse_sort_fields(sort_by: Optional[str]) -> List[SortField]:
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
        return [("_text_match", "desc")]

    has_text_match = any(field in {"_text_match", "text_match"} for field, _ in fields)
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


@router.post(
    "/ingest/{collection_name}",
    response_model=IngestResponse,
    summary="Ingest a document",
)
def ingest_document(
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
            )
        routed_to = ",".join(name for _, name in targets)
        documents_ingested.labels(collection=collection_name).inc()

        return IngestResponse(
            status="ok", document_id=str(doc_id), routed_to=routed_to
        )
    except Exception as e:
        logger.error(f"Failed to publish document to Kafka: {e}")
        raise HTTPException(status_code=500, detail=f"Kafka producer error: {str(e)}")


@router.get(
    "/search/{collection_name}",
    summary="Federated search",
)
async def search(
    response: Response,
    collection_name: str = Path(..., pattern=NAME_PATTERN, description="Collection name"),
    q: str = Query(..., min_length=1, description="Search query"),
    query_by: str = Query(..., description="Comma-separated fields to search"),
    filter_by: Optional[str] = Query(None, description="Filter expression"),
    sort_by: Optional[str] = Query(None, description="Sort expression"),
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
    named_clients = federation.get_named_clients_for_search(collection_name)

    if not named_clients:
        raise HTTPException(
            status_code=404,
            detail=f"Collection '{collection_name}' not found on any registered cluster.",
        )

    try:
        sort_fields = _parse_sort_fields(sort_by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Cursor-style: use offset/limit when provided; otherwise page/per_page
    use_offset = offset is not None and limit is not None
    if use_offset:
        start_idx = offset
        page_size = limit
        total_needed = offset + limit
        effective_page = (offset // limit) + 1
    else:
        start_idx = (page - 1) * per_page
        page_size = per_page
        total_needed = page * per_page
        effective_page = page

    # Deep pagination: fetch enough from each cluster to cover requested range

    # Cap to prevent memory issues
    if total_needed > MAX_DEEP_PAGINATION_DOCS:
        total_needed = MAX_DEEP_PAGINATION_DOCS
        response.headers["X-Pagination-Warning"] = (
            f"Deep pagination capped at {MAX_DEEP_PAGINATION_DOCS} documents. "
            "Results may be incomplete for very deep pages."
        )

    # Add warning header for deep pagination
    if page > DEEP_PAGINATION_WARNING_PAGE:
        response.headers["X-Pagination-Info"] = (
            f"Deep pagination (page {page}) requires fetching {total_needed} docs "
            f"from each of {len(named_clients)} clusters. Consider using filters to narrow results."
        )

    # Build search parameters - fetch all needed docs from each cluster
    search_params = {
        "q": q,
        "query_by": query_by,
        "page": 1,  # Always fetch from page 1
        "per_page": total_needed,  # Fetch enough to cover requested page
    }
    if filter_by:
        search_params["filter_by"] = filter_by
    if sort_by:
        search_params["sort_by"] = sort_by

    async def search_cluster(
        cluster_name: str, client
    ) -> Tuple[str, Optional[Dict], Optional[str]]:
        """Execute search on a single cluster."""
        try:
            result = await asyncio.to_thread(
                client.collections[collection_name].documents.search, search_params
            )
            return cluster_name, result, None
        except Exception as e:
            logger.warning("Search failed for cluster %s: %s", cluster_name, e)
            return cluster_name, None, str(e)

    # Scatter: Execute searches in parallel across all clusters
    tasks = [
        search_cluster(cluster_name, client)
        for cluster_name, client in named_clients
    ]
    results_list = await asyncio.gather(*tasks)

    # Gather: Collect all hits from successful responses
    all_hits: List[Dict] = []
    total_found = 0
    successful_clusters = 0
    failed_clusters: List[str] = []

    for cluster_name, result, _error in results_list:
        if result is not None:
            all_hits.extend(result.get("hits", []))
            total_found += result.get("found", 0)
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

    # Merge: Deduplicate by document ID using the same comparator used globally.
    unique_hits_map: Dict[str, Dict] = {}
    for hit in all_hits:
        doc_id = hit.get("document", {}).get("id")
        if not doc_id:
            continue
        existing_hit = unique_hits_map.get(doc_id)
        if existing_hit is None or _compare_hits(hit, existing_hit, sort_fields) < 0:
            unique_hits_map[doc_id] = hit

    # Sort: Re-rank all unique hits with Typesense-compatible simple sort fields.
    sorted_hits = sorted(
        unique_hits_map.values(),
        key=cmp_to_key(lambda left, right: _compare_hits(left, right, sort_fields)),
    )

    # Slice: Return only the requested page or offset window
    end_idx = start_idx + page_size
    page_hits = sorted_hits[start_idx:end_idx]
    has_more = end_idx < len(sorted_hits)
    next_offset = offset + limit if (use_offset and has_more) else None

    out = {
        "found": total_found,
        "page": effective_page,
        "per_page": page_size,
        "hits": page_hits,
        "out_of": len(sorted_hits),
        "clusters_queried": len(named_clients),
        "clusters_responded": successful_clusters,
        "failed_clusters": failed_clusters,
        "partial": bool(failed_clusters),
        "has_more": has_more,
    }
    if use_offset:
        out["offset"] = offset
        out["next_offset"] = next_offset
    return out
