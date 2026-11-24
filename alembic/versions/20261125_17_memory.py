"""Assistant memory, alert mutes and answerable alert questions (v4).

Revision ID: 20261125_17
Revises: 20261125_16
"""
import sqlalchemy as sa
from alembic import op

revision = "20261125_17"
down_revision = "20261125_16"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "memories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("text", sa.String(300), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("origin", sa.String(10), nullable=False, server_default="owner"),
        sa.Column("status", sa.String(10), nullable=False, server_default="active"),
        sa.Column("rule_proposal", sa.JSON()),
        sa.Column("rule_ref", sa.String(40)),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.Column("last_used_at", timestamp),
    )
    op.create_index("ix_memories_status", "memories", ["status"])
    op.create_table(
        "alert_mutes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("alert_type", sa.String(40)),
        sa.Column("merchant", sa.String(255)),
        sa.Column("below_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("memory_id", sa.Integer(), sa.ForeignKey("memories.id", ondelete="SET NULL")),
        sa.Column("created_at", timestamp, nullable=False),
    )
    op.create_index("ix_alert_mutes_active", "alert_mutes", ["active"])
    op.create_index("ix_alert_mutes_memory_id", "alert_mutes", ["memory_id"])
    op.create_table(
        "alert_questions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("alert_id", sa.Integer(), sa.ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("item_id", sa.Integer()),
        sa.Column("amount", sa.Numeric(18, 2)),
        sa.Column("choices", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="open"),
        sa.Column("answer", sa.String(40)),
        sa.Column("claimed_at", timestamp),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("answered_at", timestamp),
        sa.UniqueConstraint("kind", "key", name="uq_alert_questions_kind_key"),
    )
    op.create_index("ix_alert_questions_item_id", "alert_questions", ["item_id"])
    op.create_index("ix_alert_questions_status", "alert_questions", ["status"])
    op.create_index("ix_alert_questions_created_at", "alert_questions", ["created_at"])


def downgrade():
    raise RuntimeError("v4 migrations are forward-only; restore a pre-upgrade backup")
