"""Budget assignment: learned merchant/category -> budget mappings and the per-expense assignment queue (v4).

Revision ID: 20261124_13
Revises: 20261124_12
"""
import sqlalchemy as sa
from alembic import op

revision = "20261124_13"
down_revision = "20261124_12"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "budget_map",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("budget", sa.String(255), nullable=False),
        sa.Column("streak", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("learned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.UniqueConstraint("kind", "key", name="uq_budget_map_kind_key"),
    )
    op.create_table(
        "budget_assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("inbox_items.id"), nullable=False, unique=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("budget", sa.String(255)),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("source", sa.String(14)),
        sa.Column("reason", sa.String(300), nullable=False, server_default=""),
        sa.Column("note", sa.String(60)),
        sa.Column("model", sa.String(100)),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("group_id", sa.String(36)),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.Column("applied_at", timestamp),
    )
    op.create_index("ix_budget_assignments_status", "budget_assignments", ["status"])
    op.create_index("ix_budget_assignments_group_id", "budget_assignments", ["group_id"])


def downgrade():
    raise RuntimeError("v4 migrations are forward-only; restore a pre-upgrade backup")
