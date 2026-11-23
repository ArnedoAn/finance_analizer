"""AI budget, AI suggestions for the review queue, rules that learn and counterparty merges (v4).

Revision ID: 20261124_10
Revises: 20261124_09
"""
import sqlalchemy as sa
from alembic import op

revision = "20261124_10"
down_revision = "20261124_09"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "ai_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("auto_calls", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("allowance", sa.JSON()),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.UniqueConstraint("day", "kind", name="uq_ai_usage_day_kind"),
    )
    op.create_index("ix_ai_usage_day", "ai_usage", ["day"])
    op.create_table(
        "ai_suggestions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("inbox_items.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("action", sa.String(12), nullable=False),
        sa.Column("firefly_account", sa.String(255)),
        sa.Column("category", sa.String(255)),
        sa.Column("merchant", sa.String(255)),
        sa.Column("transaction_type", sa.String(20)),
        sa.Column("amount", sa.Numeric(18, 2)),
        sa.Column("duplicate_of_id", sa.Integer()),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.String(100)),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.Column("applied_at", timestamp),
    )
    op.create_index("ix_ai_suggestions_status", "ai_suggestions", ["status"])
    op.create_table(
        "merchant_corrections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("merchant", sa.String(255), nullable=False, unique=True),
        sa.Column("category", sa.String(255), nullable=False),
        sa.Column("streak", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", timestamp, nullable=False),
    )
    op.create_table(
        "counterparty_merges",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_type", sa.String(10), nullable=False),
        sa.Column("target_id", sa.String(100), nullable=False),
        sa.Column("target_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False, server_default="system"),
        sa.Column("device_id", sa.Integer()),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("expires_at", timestamp, nullable=False),
        sa.Column("undone_at", timestamp),
    )
    op.create_index("ix_counterparty_merges_status", "counterparty_merges", ["status"])
    # A learned rule the owner contradicts is switched off, not deleted; existing rules stay active.
    op.add_column("rules", sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    raise RuntimeError("v4 migrations are forward-only; restore a pre-upgrade backup")
