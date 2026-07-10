"""Add audit delivery checkpoints and durable deletion suppression.

Revision ID: 0003_audit_delivery_deletion
Revises: 0002_indexing_outbox
"""

from __future__ import annotations

import hashlib
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_audit_delivery_deletion"
down_revision: Union[str, None] = "0002_indexing_outbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_delivery_checkpoints",
        sa.Column("destination_id", sa.String(length=64), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("last_event_hash", sa.String(length=64), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_attempts", sa.Integer(), nullable=False),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("last_sequence >= 0", name="ck_audit_delivery_sequence"),
        sa.CheckConstraint(
            "length(last_event_hash) = 64",
            name="ck_audit_delivery_event_hash",
        ),
        sa.CheckConstraint(
            "failure_attempts >= 0",
            name="ck_audit_delivery_failure_attempts",
        ),
        sa.PrimaryKeyConstraint("destination_id"),
    )
    op.create_index(
        "ix_audit_delivery_next_retry",
        "audit_delivery_checkpoints",
        ["next_retry_at"],
    )

    op.create_table(
        "deletion_ledger",
        sa.Column("identity_hash", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=256), nullable=False),
        sa.Column("collection", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.String(length=256), nullable=False),
        sa.Column("tombstone_sequence", sa.BigInteger(), nullable=False),
        sa.Column("document_version", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_digest", sa.String(length=64), nullable=False),
        sa.Column("routing_revision", sa.BigInteger(), nullable=False),
        sa.Column("rollout_id", sa.String(length=128), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("legal_hold", sa.Boolean(), nullable=False),
        sa.Column("legal_hold_reference_hash", sa.String(length=64), nullable=True),
        sa.Column("converged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(identity_hash) = 64",
            name="ck_deletion_ledger_identity_hash",
        ),
        sa.CheckConstraint(
            "tombstone_sequence >= 1",
            name="ck_deletion_ledger_sequence",
        ),
        sa.CheckConstraint(
            "document_version >= 1",
            name="ck_deletion_ledger_document_version",
        ),
        sa.CheckConstraint(
            "routing_revision >= 1",
            name="ck_deletion_ledger_routing_revision",
        ),
        sa.CheckConstraint(
            "length(event_digest) = 64",
            name="ck_deletion_ledger_event_digest",
        ),
        sa.PrimaryKeyConstraint("identity_hash"),
    )
    op.create_index(
        "ix_deletion_ledger_retention",
        "deletion_ledger",
        ["retention_until"],
    )
    op.create_index(
        "ix_deletion_ledger_tenant_collection",
        "deletion_ledger",
        ["tenant_id", "collection"],
    )
    op.create_table(
        "deletion_ledger_targets",
        sa.Column("identity_hash", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("tombstone_sequence", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("checkpoint_sequence", sa.BigInteger(), nullable=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "tombstone_sequence >= 1",
            name="ck_deletion_target_sequence",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'absent')",
            name="ck_deletion_target_status",
        ),
        sa.CheckConstraint(
            "checkpoint_sequence IS NULL OR checkpoint_sequence >= tombstone_sequence",
            name="ck_deletion_target_checkpoint",
        ),
        sa.ForeignKeyConstraint(
            ["identity_hash"],
            ["deletion_ledger.identity_hash"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("identity_hash", "target_id"),
    )
    op.create_index(
        "ix_deletion_ledger_targets_status",
        "deletion_ledger_targets",
        ["status"],
    )
    _backfill_existing_tombstones()


def _backfill_existing_tombstones() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT event_id, identity_hash, sequence, payload_json, created_at "
            "FROM indexing_event_outbox ORDER BY identity_hash, sequence"
        )
    ).mappings()
    latest = {}
    for row in rows:
        payload = row["payload_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if payload.get("operation") not in {"delete", "tombstone"}:
            continue
        latest[str(row["identity_hash"])] = (row, payload)

    ledger = sa.table(
        "deletion_ledger",
        *[sa.column(name) for name in (
            "identity_hash", "tenant_id", "collection", "document_id",
            "tombstone_sequence", "document_version", "event_id", "event_digest",
            "routing_revision", "rollout_id", "requested_at", "retention_until",
            "legal_hold", "legal_hold_reference_hash", "converged_at",
            "last_verified_at", "updated_at",
        )],
    )
    targets = sa.table(
        "deletion_ledger_targets",
        *[sa.column(name) for name in (
            "identity_hash", "target_id", "tombstone_sequence", "status",
            "checkpoint_sequence", "receipt_hash", "verified_at", "updated_at",
        )],
    )
    for identity_hash, (row, payload) in latest.items():
        identity = payload["identity"]
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        sequence = int(row["sequence"])
        bind.execute(
            ledger.insert().values(
                identity_hash=identity_hash,
                tenant_id=str(identity["tenant_id"]),
                collection=str(identity["collection"]),
                document_id=str(identity["document_id"]),
                tombstone_sequence=sequence,
                document_version=int(payload.get("document_version", sequence)),
                event_id=str(row["event_id"]),
                event_digest=hashlib.sha256(encoded).hexdigest(),
                routing_revision=int(payload["routing_revision"]),
                rollout_id=str(payload.get("rollout_id") or ""),
                requested_at=row["created_at"],
                retention_until=None,
                legal_hold=False,
                legal_hold_reference_hash=None,
                converged_at=None,
                last_verified_at=None,
                updated_at=row["created_at"],
            )
        )
        for target_id in dict.fromkeys(payload.get("target_clusters") or ()):
            bind.execute(
                targets.insert().values(
                    identity_hash=identity_hash,
                    target_id=str(target_id),
                    tombstone_sequence=sequence,
                    status="pending",
                    checkpoint_sequence=None,
                    receipt_hash=None,
                    verified_at=None,
                    updated_at=row["created_at"],
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in ("deletion_ledger", "audit_delivery_checkpoints"):
        count = bind.scalar(sa.text(f"SELECT count(*) FROM {table_name}"))
        if int(count or 0) != 0:
            raise RuntimeError(
                f"Refusing downgrade while {table_name} contains durable evidence"
            )
    op.drop_index(
        "ix_deletion_ledger_targets_status",
        table_name="deletion_ledger_targets",
    )
    op.drop_table("deletion_ledger_targets")
    op.drop_index(
        "ix_deletion_ledger_tenant_collection",
        table_name="deletion_ledger",
    )
    op.drop_index(
        "ix_deletion_ledger_retention",
        table_name="deletion_ledger",
    )
    op.drop_table("deletion_ledger")
    op.drop_index(
        "ix_audit_delivery_next_retry",
        table_name="audit_delivery_checkpoints",
    )
    op.drop_table("audit_delivery_checkpoints")
