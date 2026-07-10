"""SQLAlchemy Core schema shared by the PostgreSQL adapter and Alembic."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from .contracts import CURRENT_STATE_SCHEMA_VERSION


metadata = MetaData()
json_type = JSON().with_variant(JSONB(), "postgresql")

STATE_ROW_ID = "current"
AUDIT_HEAD_ID = "head"
ALEMBIC_HEAD_REVISION = "0003_audit_delivery_deletion"

control_plane_state = Table(
    "control_plane_state",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("revision", BigInteger, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("state_json", json_type, nullable=False),
    Column("state_digest", String(64), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("id = 'current'", name="ck_control_plane_state_singleton"),
    CheckConstraint("revision >= 1", name="ck_control_plane_state_revision"),
    CheckConstraint("schema_version >= 1", name="ck_control_plane_state_schema_version"),
    CheckConstraint("length(state_digest) = 64", name="ck_control_plane_state_digest"),
)

control_plane_audit_head = Table(
    "control_plane_audit_head",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("sequence", BigInteger, nullable=False),
    Column("event_hash", String(64), nullable=False),
    CheckConstraint("id = 'head'", name="ck_control_plane_audit_head_singleton"),
    CheckConstraint("sequence >= 0", name="ck_control_plane_audit_head_sequence"),
    CheckConstraint("length(event_hash) = 64", name="ck_control_plane_audit_head_hash"),
)

control_plane_audit = Table(
    "control_plane_audit",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("sequence", BigInteger, nullable=False, unique=True),
    Column("revision", BigInteger, nullable=True),
    Column("timestamp", DateTime(timezone=True), nullable=False),
    Column("actor", String(256), nullable=False),
    Column("action", String(128), nullable=False),
    Column("resource_type", String(128), nullable=False),
    Column("resource_id", String(512), nullable=False),
    Column("status", String(64), nullable=False),
    Column("request_id", String(128), nullable=False, default=""),
    Column("details_json", json_type, nullable=False),
    Column("previous_hash", String(64), nullable=False),
    Column("event_hash", String(64), nullable=False),
    CheckConstraint("sequence >= 1", name="ck_control_plane_audit_sequence"),
    CheckConstraint("length(previous_hash) = 64", name="ck_control_plane_audit_previous_hash"),
    CheckConstraint("length(event_hash) = 64", name="ck_control_plane_audit_event_hash"),
)
Index("ix_control_plane_audit_timestamp", control_plane_audit.c.timestamp)
Index("ix_control_plane_audit_action", control_plane_audit.c.action)
Index("ix_control_plane_audit_resource_type", control_plane_audit.c.resource_type)

control_plane_outbox = Table(
    "control_plane_outbox",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("revision", BigInteger, nullable=False, unique=True),
    Column("event_type", String(128), nullable=False),
    Column("payload_json", json_type, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("publish_attempts", Integer, nullable=False, default=0),
    CheckConstraint("revision >= 1", name="ck_control_plane_outbox_revision"),
    CheckConstraint("publish_attempts >= 0", name="ck_control_plane_outbox_attempts"),
)
Index("ix_control_plane_outbox_unpublished", control_plane_outbox.c.published_at)

indexing_event_heads = Table(
    "indexing_event_heads",
    metadata,
    Column("identity_hash", String(64), primary_key=True),
    Column("tenant_id", String(256), nullable=False),
    Column("collection", String(128), nullable=False),
    Column("document_id", String(256), nullable=False),
    Column("last_sequence", BigInteger, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("length(identity_hash) = 64", name="ck_indexing_head_identity_hash"),
    CheckConstraint("last_sequence >= 0", name="ck_indexing_head_sequence"),
)

indexing_event_outbox = Table(
    "indexing_event_outbox",
    metadata,
    Column("event_id", String(68), primary_key=True),
    Column(
        "global_position",
        BigInteger,
        Identity(always=True, start=1),
        nullable=False,
    ),
    Column("idempotency_key_hash", String(64), nullable=False, unique=True),
    Column("request_digest", String(64), nullable=False),
    Column("identity_hash", String(64), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("payload_json", json_type, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("publish_attempts", Integer, nullable=False, default=0),
    CheckConstraint("length(idempotency_key_hash) = 64", name="ck_indexing_outbox_key_hash"),
    CheckConstraint("length(request_digest) = 64", name="ck_indexing_outbox_request_digest"),
    CheckConstraint("length(identity_hash) = 64", name="ck_indexing_outbox_identity_hash"),
    CheckConstraint("global_position >= 1", name="ck_indexing_outbox_global_position"),
    CheckConstraint("sequence >= 1", name="ck_indexing_outbox_sequence"),
    CheckConstraint("publish_attempts >= 0", name="ck_indexing_outbox_attempts"),
)
Index(
    "uq_indexing_event_identity_sequence",
    indexing_event_outbox.c.identity_hash,
    indexing_event_outbox.c.sequence,
    unique=True,
)
Index(
    "uq_indexing_event_global_position",
    indexing_event_outbox.c.global_position,
    unique=True,
)
Index("ix_indexing_event_outbox_unpublished", indexing_event_outbox.c.published_at)
Index("ix_indexing_event_outbox_created", indexing_event_outbox.c.created_at)

indexing_checkpoints = Table(
    "indexing_checkpoints",
    metadata,
    Column("checkpoint_key", String(64), primary_key=True),
    Column("identity_key", Text, nullable=False),
    Column("tenant_id", String(256), nullable=False),
    Column("collection", String(128), nullable=False),
    Column("document_id", String(256), nullable=False),
    Column("target_cluster", String(128), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("document_version", BigInteger, nullable=False),
    Column("operation", String(16), nullable=False),
    Column("event_id", String(128), nullable=False),
    Column("event_digest", String(64), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("applied_at_ms", BigInteger, nullable=False),
    Column("tombstone", Boolean, nullable=False),
    Column("routing_revision", BigInteger, nullable=False),
    Column("rollout_id", String(128), nullable=False, default=""),
    UniqueConstraint(
        "identity_key",
        "target_cluster",
        name="uq_indexing_checkpoint_identity_target",
    ),
    CheckConstraint(
        "length(checkpoint_key) = 64",
        name="ck_indexing_checkpoint_key_hash",
    ),
    CheckConstraint("sequence >= 1", name="ck_indexing_checkpoint_sequence"),
    CheckConstraint(
        "document_version >= 1",
        name="ck_indexing_checkpoint_document_version",
    ),
    CheckConstraint(
        "routing_revision >= 1",
        name="ck_indexing_checkpoint_routing_revision",
    ),
    CheckConstraint(
        "applied_at_ms >= 0",
        name="ck_indexing_checkpoint_applied_at",
    ),
    CheckConstraint(
        "operation IN ('upsert', 'delete', 'tombstone')",
        name="ck_indexing_checkpoint_operation",
    ),
    CheckConstraint(
        "length(event_digest) = 64",
        name="ck_indexing_checkpoint_event_digest",
    ),
    CheckConstraint(
        "((operation = 'upsert' AND tombstone = false) OR "
        "(operation IN ('delete', 'tombstone') AND tombstone = true))",
        name="ck_indexing_checkpoint_tombstone",
    ),
)
Index(
    "ix_indexing_checkpoints_tenant_collection",
    indexing_checkpoints.c.tenant_id,
    indexing_checkpoints.c.collection,
)

audit_delivery_checkpoints = Table(
    "audit_delivery_checkpoints",
    metadata,
    Column("destination_id", String(64), primary_key=True),
    Column("last_sequence", BigInteger, nullable=False),
    Column("last_event_hash", String(64), nullable=False),
    Column("last_success_at", DateTime(timezone=True), nullable=True),
    Column("failure_attempts", Integer, nullable=False),
    Column("last_failure_at", DateTime(timezone=True), nullable=True),
    Column("last_error_code", String(64), nullable=True),
    Column("next_retry_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("last_sequence >= 0", name="ck_audit_delivery_sequence"),
    CheckConstraint(
        "length(last_event_hash) = 64",
        name="ck_audit_delivery_event_hash",
    ),
    CheckConstraint(
        "failure_attempts >= 0",
        name="ck_audit_delivery_failure_attempts",
    ),
)
Index(
    "ix_audit_delivery_next_retry",
    audit_delivery_checkpoints.c.next_retry_at,
)

deletion_ledger = Table(
    "deletion_ledger",
    metadata,
    Column("identity_hash", String(64), primary_key=True),
    Column("tenant_id", String(256), nullable=False),
    Column("collection", String(128), nullable=False),
    Column("document_id", String(256), nullable=False),
    Column("tombstone_sequence", BigInteger, nullable=False),
    Column("document_version", BigInteger, nullable=False),
    Column("event_id", String(128), nullable=False),
    Column("event_digest", String(64), nullable=False),
    Column("routing_revision", BigInteger, nullable=False),
    Column("rollout_id", String(128), nullable=False, default=""),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("retention_until", DateTime(timezone=True), nullable=True),
    Column("legal_hold", Boolean, nullable=False, default=False),
    Column("legal_hold_reference_hash", String(64), nullable=True),
    Column("converged_at", DateTime(timezone=True), nullable=True),
    Column("last_verified_at", DateTime(timezone=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "length(identity_hash) = 64",
        name="ck_deletion_ledger_identity_hash",
    ),
    CheckConstraint(
        "tombstone_sequence >= 1",
        name="ck_deletion_ledger_sequence",
    ),
    CheckConstraint(
        "document_version >= 1",
        name="ck_deletion_ledger_document_version",
    ),
    CheckConstraint(
        "routing_revision >= 1",
        name="ck_deletion_ledger_routing_revision",
    ),
    CheckConstraint(
        "length(event_digest) = 64",
        name="ck_deletion_ledger_event_digest",
    ),
)
Index(
    "ix_deletion_ledger_retention",
    deletion_ledger.c.retention_until,
)
Index(
    "ix_deletion_ledger_tenant_collection",
    deletion_ledger.c.tenant_id,
    deletion_ledger.c.collection,
)

deletion_ledger_targets = Table(
    "deletion_ledger_targets",
    metadata,
    Column(
        "identity_hash",
        String(64),
        ForeignKey("deletion_ledger.identity_hash", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("target_id", String(128), primary_key=True),
    Column("tombstone_sequence", BigInteger, nullable=False),
    Column("status", String(16), nullable=False),
    Column("checkpoint_sequence", BigInteger, nullable=True),
    Column("receipt_hash", String(64), nullable=True),
    Column("verified_at", DateTime(timezone=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "tombstone_sequence >= 1",
        name="ck_deletion_target_sequence",
    ),
    CheckConstraint(
        "status IN ('pending', 'applied', 'absent')",
        name="ck_deletion_target_status",
    ),
    CheckConstraint(
        "checkpoint_sequence IS NULL OR checkpoint_sequence >= tombstone_sequence",
        name="ck_deletion_target_checkpoint",
    ),
)
Index(
    "ix_deletion_ledger_targets_status",
    deletion_ledger_targets.c.status,
)
