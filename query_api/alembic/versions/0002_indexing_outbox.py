"""Add durable ordered indexing event outbox.

Revision ID: 0002_indexing_outbox
Revises: 0001_control_plane
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0002_indexing_outbox"
down_revision: Union[str, None] = "0001_control_plane"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "indexing_event_heads",
        sa.Column("identity_hash", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=256), nullable=False),
        sa.Column("collection", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.String(length=256), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(identity_hash) = 64",
            name="ck_indexing_head_identity_hash",
        ),
        sa.CheckConstraint(
            "last_sequence >= 0",
            name="ck_indexing_head_sequence",
        ),
        sa.PrimaryKeyConstraint("identity_hash"),
    )
    op.create_table(
        "indexing_event_outbox",
        sa.Column("event_id", sa.String(length=68), nullable=False),
        sa.Column(
            "global_position",
            sa.BigInteger(),
            sa.Identity(always=True, start=1),
            nullable=False,
        ),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("identity_hash", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("payload_json", _json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("publish_attempts", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "length(idempotency_key_hash) = 64",
            name="ck_indexing_outbox_key_hash",
        ),
        sa.CheckConstraint(
            "length(request_digest) = 64",
            name="ck_indexing_outbox_request_digest",
        ),
        sa.CheckConstraint(
            "length(identity_hash) = 64",
            name="ck_indexing_outbox_identity_hash",
        ),
        sa.CheckConstraint(
            "global_position >= 1",
            name="ck_indexing_outbox_global_position",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_indexing_outbox_sequence"),
        sa.CheckConstraint(
            "publish_attempts >= 0",
            name="ck_indexing_outbox_attempts",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("idempotency_key_hash"),
    )
    op.create_index(
        "uq_indexing_event_identity_sequence",
        "indexing_event_outbox",
        ["identity_hash", "sequence"],
        unique=True,
    )
    op.create_index(
        "uq_indexing_event_global_position",
        "indexing_event_outbox",
        ["global_position"],
        unique=True,
    )
    op.create_index(
        "ix_indexing_event_outbox_unpublished",
        "indexing_event_outbox",
        ["published_at"],
    )
    op.create_index(
        "ix_indexing_event_outbox_created",
        "indexing_event_outbox",
        ["created_at"],
    )
    op.create_table(
        "indexing_checkpoints",
        sa.Column("checkpoint_key", sa.String(length=64), nullable=False),
        sa.Column("identity_key", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.String(length=256), nullable=False),
        sa.Column("collection", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.String(length=256), nullable=False),
        sa.Column("target_cluster", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("document_version", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_digest", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("tombstone", sa.Boolean(), nullable=False),
        sa.Column("routing_revision", sa.BigInteger(), nullable=False),
        sa.Column("rollout_id", sa.String(length=128), nullable=False),
        sa.CheckConstraint(
            "length(checkpoint_key) = 64",
            name="ck_indexing_checkpoint_key_hash",
        ),
        sa.CheckConstraint(
            "sequence >= 1",
            name="ck_indexing_checkpoint_sequence",
        ),
        sa.CheckConstraint(
            "document_version >= 1",
            name="ck_indexing_checkpoint_document_version",
        ),
        sa.CheckConstraint(
            "routing_revision >= 1",
            name="ck_indexing_checkpoint_routing_revision",
        ),
        sa.CheckConstraint(
            "applied_at_ms >= 0",
            name="ck_indexing_checkpoint_applied_at",
        ),
        sa.CheckConstraint(
            "operation IN ('upsert', 'delete', 'tombstone')",
            name="ck_indexing_checkpoint_operation",
        ),
        sa.CheckConstraint(
            "length(event_digest) = 64",
            name="ck_indexing_checkpoint_event_digest",
        ),
        sa.CheckConstraint(
            "((operation = 'upsert' AND tombstone = false) OR "
            "(operation IN ('delete', 'tombstone') AND tombstone = true))",
            name="ck_indexing_checkpoint_tombstone",
        ),
        sa.PrimaryKeyConstraint("checkpoint_key"),
        sa.UniqueConstraint(
            "identity_key",
            "target_cluster",
            name="uq_indexing_checkpoint_identity_target",
        ),
    )
    op.create_index(
        "ix_indexing_checkpoints_tenant_collection",
        "indexing_checkpoints",
        ["tenant_id", "collection"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_indexing_checkpoints_tenant_collection",
        table_name="indexing_checkpoints",
    )
    op.drop_table("indexing_checkpoints")
    op.drop_index(
        "ix_indexing_event_outbox_created",
        table_name="indexing_event_outbox",
    )
    op.drop_index(
        "ix_indexing_event_outbox_unpublished",
        table_name="indexing_event_outbox",
    )
    op.drop_index(
        "uq_indexing_event_identity_sequence",
        table_name="indexing_event_outbox",
    )
    op.drop_index(
        "uq_indexing_event_global_position",
        table_name="indexing_event_outbox",
    )
    op.drop_table("indexing_event_outbox")
    op.drop_table("indexing_event_heads")
