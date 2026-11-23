"""AI insights: category/tag proposals per movement, narrated monthly reports and small model-written notes (v4).

Revision ID: 20261124_12
Revises: 20261124_11
"""
import sqlalchemy as sa
from alembic import op

revision = "20261124_12"
down_revision = "20261124_11"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "ai_item_suggestions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("inbox_items.id"), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("category", sa.String(255)),
        sa.Column("category_confidence", sa.Float(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("tags_confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("note", sa.String(60)),
        sa.Column("model", sa.String(100)),
        sa.Column("group_id", sa.String(36)),
        sa.Column("applied_fields", sa.JSON(), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.Column("applied_at", timestamp),
        sa.UniqueConstraint("item_id", name="uq_ai_item_suggestions_item"),
    )
    op.create_index("ix_ai_item_suggestions_status", "ai_item_suggestions", ["status"])
    op.create_index("ix_ai_item_suggestions_group_id", "ai_item_suggestions", ["group_id"])
    op.create_table(
        "ai_narratives",
        sa.Column("month", sa.String(7), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("figures_json", sa.Text(), nullable=False),
        sa.Column("model", sa.String(100)),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
    )
    op.create_table(
        "ai_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.UniqueConstraint("kind", "key", name="uq_ai_notes_kind_key"),
    )


def downgrade():
    raise RuntimeError("v4 migrations are forward-only; restore a pre-upgrade backup")
