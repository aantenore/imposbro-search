"""Create transactional control-plane state, audit chain, and outbox.

Revision ID: 0001_control_plane
Revises: None
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001_control_plane"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "control_plane_state",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("state_json", _json_type(), nullable=False),
        sa.Column("state_digest", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 'current'", name="ck_control_plane_state_singleton"),
        sa.CheckConstraint("revision >= 1", name="ck_control_plane_state_revision"),
        sa.CheckConstraint(
            "schema_version >= 1",
            name="ck_control_plane_state_schema_version",
        ),
        sa.CheckConstraint(
            "length(state_digest) = 64",
            name="ck_control_plane_state_digest",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "control_plane_audit_head",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "id = 'head'",
            name="ck_control_plane_audit_head_singleton",
        ),
        sa.CheckConstraint(
            "sequence >= 0",
            name="ck_control_plane_audit_head_sequence",
        ),
        sa.CheckConstraint(
            "length(event_hash) = 64",
            name="ck_control_plane_audit_head_hash",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        sa.table(
            "control_plane_audit_head",
            sa.column("id", sa.String()),
            sa.column("sequence", sa.BigInteger()),
            sa.column("event_hash", sa.String()),
        ),
        [{"id": "head", "sequence": 0, "event_hash": "0" * 64}],
    )

    op.create_table(
        "control_plane_audit",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=256), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("resource_type", sa.String(length=128), nullable=False),
        sa.Column("resource_id", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("details_json", _json_type(), nullable=False),
        sa.Column("previous_hash", sa.String(length=64), nullable=False),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "sequence >= 1",
            name="ck_control_plane_audit_sequence",
        ),
        sa.CheckConstraint(
            "length(previous_hash) = 64",
            name="ck_control_plane_audit_previous_hash",
        ),
        sa.CheckConstraint(
            "length(event_hash) = 64",
            name="ck_control_plane_audit_event_hash",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sequence"),
    )
    op.create_index(
        "ix_control_plane_audit_timestamp",
        "control_plane_audit",
        ["timestamp"],
    )
    op.create_index(
        "ix_control_plane_audit_action",
        "control_plane_audit",
        ["action"],
    )
    op.create_index(
        "ix_control_plane_audit_resource_type",
        "control_plane_audit",
        ["resource_type"],
    )

    op.create_table(
        "control_plane_outbox",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload_json", _json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("publish_attempts", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_control_plane_outbox_revision",
        ),
        sa.CheckConstraint(
            "publish_attempts >= 0",
            name="ck_control_plane_outbox_attempts",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision"),
    )
    op.create_index(
        "ix_control_plane_outbox_unpublished",
        "control_plane_outbox",
        ["published_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_control_plane_outbox_unpublished",
        table_name="control_plane_outbox",
    )
    op.drop_table("control_plane_outbox")

    op.drop_index(
        "ix_control_plane_audit_resource_type",
        table_name="control_plane_audit",
    )
    op.drop_index(
        "ix_control_plane_audit_action",
        table_name="control_plane_audit",
    )
    op.drop_index(
        "ix_control_plane_audit_timestamp",
        table_name="control_plane_audit",
    )
    op.drop_table("control_plane_audit")
    op.drop_table("control_plane_audit_head")
    op.drop_table("control_plane_state")
