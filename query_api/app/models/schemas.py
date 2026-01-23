"""
Pydantic schemas for IMPOSBRO Search API.

This module contains all the data models used for request/response validation
and serialization throughout the API.
"""

from pydantic import BaseModel, Field
from typing import List, Optional


class Cluster(BaseModel):
    """
    Represents a Typesense cluster configuration.
    
    Attributes:
        name: Unique identifier for the cluster (e.g., 'cluster-us', 'cluster-eu')
        host: Hostname or IP address of the cluster
        port: Port number (default: 8108 for Typesense)
        api_key: Authentication key for the cluster
    """
    name: str = Field(..., description="Unique cluster identifier")
    host: str = Field(..., description="Cluster hostname or IP")
    port: int = Field(default=8108, description="Cluster port")
    api_key: str = Field(..., description="Cluster API key")


class CollectionField(BaseModel):
    """
    Represents a field in a Typesense collection schema.
    
    Attributes:
        name: Field name
        type: Field type (string, int32, float, bool, string[], etc.)
        facet: Whether this field should be facetable for filtering
    """
    name: str = Field(..., description="Field name")
    type: str = Field(..., description="Field type (string, int32, float, bool, string[])")
    facet: bool = Field(default=False, description="Enable faceting for this field")


class CollectionSchema(BaseModel):
    """
    Schema definition for creating a new Typesense collection.
    
    Attributes:
        name: Collection name
        fields: List of field definitions
        default_sorting_field: Optional field to sort by default
    """
    name: str = Field(..., description="Collection name")
    fields: List[CollectionField] = Field(..., description="Collection field definitions")
    default_sorting_field: Optional[str] = Field(
        default=None, 
        description="Default field for sorting results"
    )


class FieldRule(BaseModel):
    """
    A single routing rule that maps a field value to a target cluster.
    
    Example: If field='region' and value='EU', route to cluster='cluster-eu'
    
    Attributes:
        field: Document field to evaluate
        value: Value that triggers this rule
        cluster: Target cluster name when rule matches
    """
    field: str = Field(..., description="Document field to check")
    value: str = Field(..., description="Value that triggers this routing rule")
    cluster: str = Field(..., description="Target cluster for matching documents")


class RoutingRules(BaseModel):
    """
    Complete routing configuration for a collection.
    
    Documents are evaluated against rules in order. The first matching rule
    determines the target cluster. If no rules match, the default_cluster is used.
    
    Attributes:
        collection: Collection name these rules apply to
        rules: List of field-based routing rules
        default_cluster: Fallback cluster when no rules match
    """
    collection: str = Field(..., description="Collection name")
    rules: List[FieldRule] = Field(default_factory=list, description="Ordered list of routing rules")
    default_cluster: str = Field(default="default", description="Fallback cluster")


class IngestResponse(BaseModel):
    """Response model for document ingestion."""
    status: str = Field(..., description="Operation status")
    document_id: str = Field(..., description="ID of ingested document")
    routed_to: str = Field(..., description="Target cluster name")


class SearchHit(BaseModel):
    """A single search result hit."""
    document: dict = Field(..., description="The matched document")
    text_match: Optional[float] = Field(None, description="Text match score")


class SearchResponse(BaseModel):
    """Response model for federated search."""
    found: int = Field(..., description="Total documents found across all clusters")
    page: int = Field(..., description="Current page number")
    hits: List[dict] = Field(..., description="Search result hits")


class OperationResponse(BaseModel):
    """Generic response for admin operations."""
    status: str = Field(default="ok", description="Operation status")
    message: str = Field(..., description="Operation result message")
