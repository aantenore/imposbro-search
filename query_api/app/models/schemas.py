"""
Pydantic schemas for IMPOSBRO Search API.

This module contains all the data models used for request/response validation
and serialization throughout the API.
"""

import re

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Any, Dict, List, Literal, Optional

from constants import DOCUMENT_ID_PATTERN, NAME_PATTERN, TYPESENSE_DEFAULT_PORT

try:
    from typing import Self, Annotated
except ImportError:
    from typing_extensions import Self, Annotated


NameString = Annotated[str, Field(pattern=NAME_PATTERN)]
FIELD_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
TYPESENSE_FIELD_TYPES = {
    "string",
    "int32",
    "int64",
    "float",
    "bool",
    "string[]",
    "int32[]",
    "int64[]",
    "float[]",
    "bool[]",
    "geopoint",
    "geopoint[]",
    "object",
    "object[]",
    "auto",
}
DEFAULT_SORTING_TYPES = {"int32", "int64", "float"}


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
    num_dim: Optional[int] = Field(
        default=None,
        ge=1,
        description="Vector dimensions for float[] embedding fields",
    )
    embed: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Typesense auto-embedding configuration for float[] fields",
    )

    @field_validator("name")
    @classmethod
    def validate_field_name(cls, value: str) -> str:
        if not FIELD_NAME_PATTERN.fullmatch(value):
            raise ValueError(
                "Field names may contain only letters, numbers, underscore, hyphen, and dot"
            )
        return value

    @field_validator("type")
    @classmethod
    def validate_field_type(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in TYPESENSE_FIELD_TYPES:
            raise ValueError(f"Unsupported Typesense field type '{value}'")
        return normalized

    @model_validator(mode="after")
    def validate_vector_metadata(self) -> Self:
        if self.num_dim is not None and self.type != "float[]":
            raise ValueError("num_dim is supported only for float[] vector fields")
        if self.embed is not None and self.type != "float[]":
            raise ValueError("embed is supported only for float[] vector fields")
        return self


class CollectionSchema(BaseModel):
    """
    Schema definition for creating a new Typesense collection.
    
    Attributes:
        name: Collection name
        fields: List of field definitions
        default_sorting_field: Optional field to sort by default
    """
    name: str = Field(..., pattern=NAME_PATTERN, description="Collection name")
    fields: List[CollectionField] = Field(
        ...,
        min_length=1,
        description="Collection field definitions",
    )
    default_sorting_field: Optional[str] = Field(
        default=None,
        description="Default field for sorting results"
    )

    @model_validator(mode="after")
    def validate_default_sorting_field(self) -> Self:
        if not self.default_sorting_field:
            return self
        fields_by_name = {field.name: field for field in self.fields}
        sort_field = fields_by_name.get(self.default_sorting_field)
        if sort_field is None:
            raise ValueError("default_sorting_field must reference a declared field")
        if sort_field.type not in DEFAULT_SORTING_TYPES:
            raise ValueError("default_sorting_field must reference a numeric field")
        return self


class FieldRule(BaseModel):
    """
    A single routing rule that maps a field value to one or more target clusters.
    
    Use 'cluster' for single-cluster routing, or 'clusters' for fan-out (replicate
    document to multiple clusters, e.g. for multi-region).
    
    Attributes:
        field: Document field to evaluate
        operator: Match operator. Defaults to legacy equals.
        value: Value that triggers legacy/equals routing
        values: Values that trigger in routing
        pattern: Glob pattern that triggers glob routing
        min/max: Inclusive numeric range bounds for range routing
        priority: Lower values evaluate first; ties keep input order
        cluster: Target cluster when rule matches (use this OR clusters)
        clusters: Target clusters for fan-out (use this OR cluster)
    """
    field: str = Field(..., description="Document field to check")
    operator: Literal["equals", "in", "glob", "range"] = Field(
        default="equals",
        description="Routing match operator",
    )
    value: Optional[str] = Field(None, description="Value that triggers equals routing")
    values: Optional[List[str]] = Field(None, description="Values that trigger in routing")
    pattern: Optional[str] = Field(None, description="Glob pattern that triggers routing")
    min: Optional[float] = Field(None, description="Inclusive minimum for range routing")
    max: Optional[float] = Field(None, description="Inclusive maximum for range routing")
    priority: Optional[int] = Field(
        None,
        ge=0,
        description="Lower values evaluate first; ties keep input order",
    )
    cluster: Optional[NameString] = Field(None, description="Single target cluster")
    clusters: Optional[List[NameString]] = Field(
        None, description="Target clusters for fan-out (replication)"
    )

    @field_validator("field")
    @classmethod
    def validate_rule_field_name(cls, value: str) -> str:
        if not FIELD_NAME_PATTERN.fullmatch(value):
            raise ValueError(
                "Routing field names may contain only letters, numbers, underscore, hyphen, and dot"
            )
        return value

    @model_validator(mode="after")
    def validate_rule(self) -> Self:
        if (self.cluster is None) == (self.clusters is None):
            raise ValueError("Set exactly one of 'cluster' or 'clusters'")
        if self.clusters is not None and len(self.clusters) == 0:
            raise ValueError("'clusters' must not be empty")
        if self.operator == "equals" and self.value is None:
            raise ValueError("'value' is required when operator is equals")
        if self.operator == "in" and not self.values:
            raise ValueError("'values' must not be empty when operator is in")
        if self.operator == "glob" and not (self.pattern or self.value):
            raise ValueError("'pattern' is required when operator is glob")
        if self.operator == "range" and self.min is None and self.max is None:
            raise ValueError("'min' or 'max' is required when operator is range")
        if (
            self.operator == "range"
            and self.min is not None
            and self.max is not None
            and self.min > self.max
        ):
            raise ValueError("'min' must be less than or equal to 'max'")
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


class RoutingPreviewRequest(BaseModel):
    """Request model for dry-running routing policy decisions."""
    collection: str = Field(..., pattern=NAME_PATTERN, description="Collection name")
    document: Dict[str, Any] = Field(..., description="Document payload to evaluate")
    rules: Optional[List[FieldRule]] = Field(
        default=None,
        description="Optional draft rules; persisted rules are used when omitted",
    )
    default_cluster: NameString = Field(
        default="default",
        description="Fallback cluster for draft rules",
    )


class RoutingPreviewResponse(BaseModel):
    """Response model for routing dry-run decisions."""
    collection: str = Field(..., pattern=NAME_PATTERN, description="Collection name")
    matched: bool = Field(..., description="Whether a rule matched")
    matched_rule: Optional[Dict[str, Any]] = Field(None, description="Matched rule")
    matched_rule_index: Optional[int] = Field(None, ge=0, description="Input rule index")
    used_default: bool = Field(..., description="Whether fallback routing was used")
    routed_to: List[NameString] = Field(default_factory=list, description="Resolved clusters")
    target_clusters: List[str] = Field(
        default_factory=list,
        description="Configured target cluster identifiers before resolution",
    )


class IngestResponse(BaseModel):
    """Response model for document ingestion."""
    status: str = Field(..., description="Operation status")
    document_id: str = Field(..., description="ID of ingested document")
    routed_to: str = Field(..., description="Target cluster name")


class BatchIngestRequest(BaseModel):
    """Request model for batch document ingestion."""
    documents: List[Dict[str, Any]] = Field(
        ...,
        min_length=1,
        description="Documents to ingest; each document must include id",
    )


class BatchIngestItemResult(BaseModel):
    """Per-document result for batch ingestion."""
    index: int = Field(..., ge=0, description="Zero-based input document index")
    document_id: Optional[str] = Field(None, description="Input document id when present")
    status: str = Field(..., description="ok or rejected")
    routed_to: Optional[str] = Field(None, description="Comma-separated target clusters")
    error: Optional[str] = Field(None, description="Rejection or publish error")


class BatchIngestResponse(BaseModel):
    """Response model for batch document ingestion."""
    status: str = Field(..., description="ok, partial, or rejected")
    requested: int = Field(..., ge=0, description="Number of requested documents")
    accepted: int = Field(..., ge=0, description="Number of documents accepted")
    rejected: int = Field(..., ge=0, description="Number of documents rejected")
    request_id: str = Field(..., description="Request id propagated to Kafka messages")
    items: List[BatchIngestItemResult] = Field(..., description="Per-document results")


class DeleteDocumentResponse(BaseModel):
    """Response model for asynchronous document deletion."""
    status: str = Field(..., description="Operation status")
    document_id: str = Field(
        ...,
        pattern=DOCUMENT_ID_PATTERN,
        description="ID of the document queued for deletion",
    )
    routed_to: str = Field(..., description="Comma-separated target cluster names")


class DocumentResponse(BaseModel):
    """Response model for document retrieval/export."""
    status: str = Field(..., description="Operation status")
    collection: str = Field(..., pattern=NAME_PATTERN, description="Collection name")
    document_id: str = Field(
        ...,
        pattern=DOCUMENT_ID_PATTERN,
        description="Retrieved document id",
    )
    found_in: str = Field(..., description="Cluster that returned the document")
    document: Dict[str, Any] = Field(..., description="Retrieved document payload")


class SearchRequest(BaseModel):
    """
    Federated search request.

    Supports keyword, semantic, and vector/hybrid Typesense searches. `query_by`
    is optional only when `vector_query` is supplied for manual vector search.
    """
    q: str = Field(..., min_length=1, description="Search query text, or '*'")
    query_by: Optional[str] = Field(
        default=None,
        description="Comma-separated fields to search; required without vector_query",
    )
    filter_by: Optional[str] = Field(default=None, description="Typesense filter expression")
    sort_by: Optional[str] = Field(default=None, description="Typesense sort expression")
    vector_query: Optional[str] = Field(
        default=None,
        description="Typesense vector_query for semantic/vector/hybrid search",
    )
    query_by_weights: Optional[str] = Field(
        default=None,
        description="Optional Typesense query_by_weights expression",
    )
    include_fields: Optional[str] = Field(default=None, description="Fields to include")
    exclude_fields: Optional[str] = Field(default=None, description="Fields to exclude")
    highlight_fields: Optional[str] = Field(default=None, description="Fields to highlight")
    highlight_full_fields: Optional[str] = Field(
        default=None,
        description="Fields to fully highlight",
    )
    highlight_start_tag: Optional[str] = Field(default=None, description="Highlight start tag")
    highlight_end_tag: Optional[str] = Field(default=None, description="Highlight end tag")
    remote_embedding_timeout_ms: Optional[int] = Field(default=None, ge=1)
    remote_embedding_num_tries: Optional[int] = Field(default=None, ge=1)
    limit_hits: Optional[int] = Field(default=None, ge=1)
    search_cutoff_ms: Optional[int] = Field(default=None, ge=1)
    max_candidates: Optional[int] = Field(default=None, ge=1)
    exhaustive_search: Optional[bool] = Field(default=None)
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=10, ge=1, le=250)
    offset: Optional[int] = Field(default=None, ge=0)
    limit: Optional[int] = Field(default=None, ge=1, le=250)

    @model_validator(mode="after")
    def require_query_by_or_vector_query(self) -> Self:
        if not (self.query_by and self.query_by.strip()) and not (
            self.vector_query and self.vector_query.strip()
        ):
            raise ValueError("Set query_by for keyword search or vector_query for vector search")
        return self


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


class ControlPlaneStateSnapshot(BaseModel):
    """Portable backup snapshot for the Query API control-plane state."""
    version: str = Field(default="imposbro.state.v1", description="Snapshot format")
    exported_at: Optional[str] = Field(default=None, description="UTC export timestamp")
    secrets_included: bool = Field(
        default=False,
        description="Whether cluster API keys are raw secrets or masked placeholders",
    )
    federation_clusters_config: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Registered federated data clusters",
    )
    collection_routing_rules: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-collection routing rules",
    )
    collection_schemas: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Desired Typesense collection schemas for reconciliation",
    )
    collection_aliases: Dict[str, Dict[str, Dict[str, str]]] = Field(
        default_factory=dict,
        description="Desired collection aliases keyed by cluster name",
    )
