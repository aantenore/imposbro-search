"""
Search Router for IMPOSBRO Search API.

This module contains endpoints for document ingestion and federated search.
"""

import asyncio
import logging
from fastapi import APIRouter, HTTPException, Query, Depends
from typing import Dict, Any, Optional, List
from prometheus_client import Counter

from models import IngestResponse, SearchResponse
from services import FederationService, KafkaService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Search & Ingestion"])

# Metrics
documents_ingested = Counter(
    "documents_ingested_total", "Total number of documents ingested.", ["collection"]
)


def get_federation_service() -> FederationService:
    """Dependency injection placeholder - set at startup."""
    raise NotImplementedError("Federation service not initialized")


def get_kafka_service() -> KafkaService:
    """Dependency injection placeholder - set at startup."""
    raise NotImplementedError("Kafka service not initialized")


@router.post(
    "/ingest/{collection_name}",
    response_model=IngestResponse,
    summary="Ingest a document",
)
def ingest_document(
    collection_name: str,
    document: Dict[str, Any],
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

    # Determine target cluster based on routing rules
    _, target_cluster_name = federation.get_client_for_document(
        collection_name, document
    )

    if not target_cluster_name:
        raise HTTPException(
            status_code=500, detail="Could not determine target cluster for document."
        )

    try:
        kafka.publish_document(
            collection_name=collection_name,
            document=document,
            target_cluster=target_cluster_name,
        )

        documents_ingested.labels(collection=collection_name).inc()

        return IngestResponse(
            status="ok", document_id=str(doc_id), routed_to=target_cluster_name
        )
    except Exception as e:
        logger.error(f"Failed to publish document to Kafka: {e}")
        raise HTTPException(status_code=500, detail=f"Kafka producer error: {str(e)}")


@router.get("/search/{collection_name}", summary="Federated search")
async def search(
    collection_name: str,
    q: str = Query(..., min_length=1, description="Search query"),
    query_by: str = Query(..., description="Comma-separated fields to search"),
    filter_by: Optional[str] = Query(None, description="Filter expression"),
    sort_by: Optional[str] = Query(None, description="Sort expression"),
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(10, ge=1, le=250, description="Results per page"),
    federation: FederationService = Depends(get_federation_service),
) -> Dict[str, Any]:
    """
    Perform a federated search across all relevant clusters.

    This endpoint implements scatter-gather search:
    1. Scatter: Send query to all clusters that may contain matching documents
    2. Gather: Collect results from all clusters
    3. Merge: Deduplicate and re-rank results by relevance

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

    # Build search parameters
    search_params = {"q": q, "query_by": query_by, "page": page, "per_page": per_page}
    if filter_by:
        search_params["filter_by"] = filter_by
    if sort_by:
        search_params["sort_by"] = sort_by

    async def search_cluster(client) -> Optional[Dict]:
        """Execute search on a single cluster."""
        try:
            return await asyncio.to_thread(
                client.collections[collection_name].documents.search, search_params
            )
        except Exception as e:
            logger.warning(f"Search failed for one cluster: {e}")
            return None

    # Scatter: Execute searches in parallel
    tasks = [search_cluster(client) for client in clients]
    results_list = await asyncio.gather(*tasks)

    # Gather: Collect all hits
    all_hits: List[Dict] = []
    total_found = 0

    for result in results_list:
        if result:
            all_hits.extend(result.get("hits", []))
            total_found += result.get("found", 0)

    # Merge: Deduplicate by document ID and sort by relevance
    unique_hits_map = {}
    for hit in all_hits:
        doc_id = hit.get("document", {}).get("id")
        if doc_id and doc_id not in unique_hits_map:
            unique_hits_map[doc_id] = hit

    # Sort by text_match score (lower is better in Typesense)
    sorted_hits = sorted(
        unique_hits_map.values(), key=lambda x: x.get("text_match", float("inf"))
    )

    return {"found": total_found, "page": page, "hits": sorted_hits}
