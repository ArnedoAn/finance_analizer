"""Audit log, assistant batches and proposals, inbox description and tags (v4 phase 4).

Revision ID: 20261122_04
Revises: 20261122_03
"""
import sqlalchemy as sa
from alembic import op

revision = "20261122_04"
down_revision = "20261122_03"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime(timezone=True)
    op.add_column("inbox_items", sa.Column("description", sa.Text()))
    op.add_column("inbox_items", sa.Column("tags", sa.JSON()))
    op.create_table(
        "audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", timestamp, nullable=False),
        sa.Column("device_id", sa.Integer()),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=False),
        sa.Column("target_id", sa.String(100), nullable=False),
        sa.Column("detail", sa.JSON()),
    )
    op.create_index("ix_audit_at", "audit", ["at"])
    op.create_index("ix_audit_action", "audit", ["action"])
    op.create_table(
        "proposal_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("window_from", timestamp, nullable=False),
        sa.Column("window_to", timestamp, nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("applied_at", timestamp),
    )
    op.create_table(
        "proposals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("proposal_batches.id"), nullable=False),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("inbox_items.id"), nullable=False),
        sa.Column("field", sa.String(30), nullable=False),
        sa.Column("old_value", sa.JSON()),
        sa.Column("new_value", sa.JSON()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("risky", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("applied_at", timestamp),
        sa.Column("reverted_at", timestamp),
    )
    op.create_index("ix_proposals_batch_id", "proposals", ["batch_id"])
    op.create_index("ix_proposals_target_id", "proposals", ["target_id"])


def downgrade():
    raise RuntimeError("v4 migrations are forward-only; restore a pre-upgrade backup")
