"""
Search Router for IMPOSBRO Search API.

This module contains endpoints for document ingestion and federated search.
Implements industry-standard scatter-gather search with correct deep pagination.
"""

import asyncio
import logging
from fastapi import APIRouter, HTTPException, Query, Depends, Response, Path, Body
from typing import Dict, Any, Optional, List
from prometheus_client import Counter

from constants import NAME_PATTERN
from deps import get_federation_service, get_kafka_service
from models import IngestResponse
from services import FederationService, KafkaService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Search & Ingestion"])

# Metrics
documents_ingested = Counter(
    "documents_ingested_total", "Total number of documents ingested.", ["collection"]
)

# Configuration for deep pagination
MAX_DEEP_PAGINATION_DOCS = 10000  # Maximum documents to fetch for deep pagination
DEEP_PAGINATION_WARNING_PAGE = 10  # Warn user when page exceeds this


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
    clients = federation.get_clients_for_search(collection_name)

    if not clients:
        raise HTTPException(
            status_code=404,
            detail=f"Collection '{collection_name}' not found on any registered cluster.",
        )

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
            f"from each of {len(clients)} clusters. Consider using filters to narrow results."
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

    async def search_cluster(client, cluster_idx: int) -> Optional[Dict]:
        """Execute search on a single cluster."""
        try:
            result = await asyncio.to_thread(
                client.collections[collection_name].documents.search, search_params
            )
            return result
        except Exception as e:
            logger.warning(f"Search failed for cluster {cluster_idx}: {e}")
            return None

    # Scatter: Execute searches in parallel across all clusters
    tasks = [search_cluster(client, i) for i, client in enumerate(clients)]
    results_list = await asyncio.gather(*tasks)

    # Gather: Collect all hits from successful responses
    all_hits: List[Dict] = []
    total_found = 0
    successful_clusters = 0

    for result in results_list:
        if result:
            all_hits.extend(result.get("hits", []))
            total_found += result.get("found", 0)
            successful_clusters += 1

    # Merge: Deduplicate by document ID (keep first occurrence = highest score)
    unique_hits_map: Dict[str, Dict] = {}
    for hit in all_hits:
        doc_id = hit.get("document", {}).get("id")
        if doc_id and doc_id not in unique_hits_map:
            unique_hits_map[doc_id] = hit

    # Sort: Re-rank all unique hits by relevance score
    # text_match in Typesense: lower = better match
    sorted_hits = sorted(
        unique_hits_map.values(), key=lambda x: x.get("text_match", float("inf"))
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
        "clusters_queried": len(clients),
        "clusters_responded": successful_clusters,
        "has_more": has_more,
    }
    if use_offset:
        out["offset"] = offset
        out["next_offset"] = next_offset
    return out
