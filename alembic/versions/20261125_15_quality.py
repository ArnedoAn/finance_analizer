"""Quality: second opinion verdict, AI job queue, trip/event proposals and credit card cut/payment days (v4).

Revision ID: 20261125_15
Revises: 20261125_14
"""
import sqlalchemy as sa
from alembic import op

revision = "20261125_15"
down_revision = "20261125_14"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime(timezone=True)
    op.add_column("inbox_items", sa.Column("second_opinion", sa.JSON()))
    op.add_column("own_accounts", sa.Column("statement_day", sa.Integer()))
    op.add_column("own_accounts", sa.Column("payment_day", sa.Integer()))
    op.create_table(
        "ai_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("runner_key", sa.String(60), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("dedupe_key", sa.String(120)),
        sa.Column("estimated_calls", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("estimated_allowance_pct", sa.Float()),
        sa.Column("model", sa.String(100)),
        sa.Column("run_after", timestamp),
        sa.Column("decided_by", sa.String(10)),
        sa.Column("result", sa.JSON()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.Column("started_at", timestamp),
        sa.Column("finished_at", timestamp),
    )
    op.create_index("ix_ai_jobs_kind", "ai_jobs", ["kind"])
    op.create_index("ix_ai_jobs_status", "ai_jobs", ["status"])
    op.create_index("ix_ai_jobs_dedupe_key", "ai_jobs", ["dedupe_key"])
    op.create_table(
        "ai_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(80), nullable=False, unique=True),
        sa.Column("label", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("city", sa.String(60)),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("item_ids", sa.JSON(), nullable=False),
        sa.Column("total_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("name_source", sa.String(10), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False, server_default=""),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("group_id", sa.String(36)),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.Column("applied_at", timestamp),
    )
    op.create_index("ix_ai_events_status", "ai_events", ["status"])
    op.create_index("ix_ai_events_group_id", "ai_events", ["group_id"])


def downgrade():
    raise RuntimeError("v4 migrations are forward-only; restore a pre-upgrade backup")
