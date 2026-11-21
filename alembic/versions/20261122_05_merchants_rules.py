"""Add merchants, per-item merchant counting marks and categorization rules (v4, A6).

Revision ID: 20261122_05
Revises: 20261122_04
"""
import sqlalchemy as sa
from alembic import op

revision = "20261122_05"
down_revision = "20261122_04"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "merchants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("default_category", sa.String(255)),
        sa.Column("count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_seen_at", timestamp),
    )
    op.create_table(
        "merchant_items",
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("inbox_items.id"), primary_key=True),
        sa.Column("merchant_id", sa.Integer(), sa.ForeignKey("merchants.id"), nullable=False),
    )
    op.create_index("ix_merchant_items_merchant_id", "merchant_items", ["merchant_id"])
    op.create_table(
        "rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("match", sa.String(20), nullable=False),
        sa.Column("pattern", sa.String(255), nullable=False),
        sa.Column("category", sa.String(255), nullable=False),
        sa.Column("merchant", sa.String(255)),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", timestamp, nullable=False),
        sa.UniqueConstraint("match", "pattern", name="uq_rules_match_pattern"),
    )


def downgrade():
    raise RuntimeError("v4 migrations are forward-only; restore a pre-upgrade backup")
