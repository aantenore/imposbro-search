"""
Pydantic schemas for IMPOSBRO Search API.

This module contains all the data models used for request/response validation
and serialization throughout the API.
"""

from pydantic import BaseModel, Field, model_validator
from typing import Any, Dict, List, Optional

from constants import NAME_PATTERN, TYPESENSE_DEFAULT_PORT

try:
    from typing import Self, Annotated
except ImportError:
    from typing_extensions import Self, Annotated


NameString = Annotated[str, Field(pattern=NAME_PATTERN)]


class Cluster(BaseModel):
    """
    Represents a Typesense cluster configuration.
    
    Attributes:
        name: Unique identifier for the cluster (e.g., 'cluster-us', 'cluster-eu')
        host: Hostname or IP address of the cluster
        port: Port number (default: 8108 for Typesense)
        api_key: Authentication key for the cluster
    """
    name: str = Field(
        ...,
        pattern=NAME_PATTERN,
        description="Unique cluster identifier",
    )
    host: str = Field(..., description="Cluster hostname or IP")
    port: int = Field(
        default=TYPESENSE_DEFAULT_PORT,
        ge=1,
        le=65535,
        description="Cluster port",
    )
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
    name: str = Field(..., pattern=NAME_PATTERN, description="Collection name")
    fields: List[CollectionField] = Field(..., description="Collection field definitions")
    default_sorting_field: Optional[str] = Field(
        default=None, 
        description="Default field for sorting results"
    )


class FieldRule(BaseModel):
    """
    A single routing rule that maps a field value to one or more target clusters.
    
    Use 'cluster' for single-cluster routing, or 'clusters' for fan-out (replicate
    document to multiple clusters, e.g. for multi-region).
    
    Attributes:
        field: Document field to evaluate
        value: Value that triggers this rule
        cluster: Target cluster when rule matches (use this OR clusters)
        clusters: Target clusters for fan-out (use this OR cluster)
    """
    field: str = Field(..., description="Document field to check")
    value: str = Field(..., description="Value that triggers this routing rule")
    cluster: Optional[NameString] = Field(None, description="Single target cluster")
    clusters: Optional[List[NameString]] = Field(
        None, description="Target clusters for fan-out (replication)"
    )

    @model_validator(mode="after")
    def require_cluster_or_clusters(self) -> Self:
        if (self.cluster is None) == (self.clusters is None):
            raise ValueError("Set exactly one of 'cluster' or 'clusters'")
        if self.clusters is not None and len(self.clusters) == 0:
            raise ValueError("'clusters' must not be empty")
        return self


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
    collection: str = Field(..., pattern=NAME_PATTERN, description="Collection name")
    rules: List[FieldRule] = Field(default_factory=list, description="Ordered list of routing rules")
    default_cluster: NameString = Field(default="default", description="Fallback cluster")


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


class AuditLogEntry(BaseModel):
    """A safe, user-visible admin audit event."""
    id: str = Field(..., description="Audit event identifier")
    timestamp_ms: int = Field(..., description="Unix timestamp in milliseconds")
    timestamp: str = Field(..., description="UTC ISO-8601 timestamp")
    actor: str = Field(..., description="Hashed or non-sensitive actor identifier")
    action: str = Field(..., description="Admin action")
    resource_type: str = Field(..., description="Resource type affected")
    resource_id: str = Field(..., description="Resource identifier affected")
    status: str = Field(default="success", description="Outcome status")
    details: Dict[str, Any] = Field(default_factory=dict, description="Safe metadata")


class AuditLogResponse(BaseModel):
    """Recent admin audit events."""
    entries: List[AuditLogEntry]
