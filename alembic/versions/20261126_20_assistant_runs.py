"""Background runs of ``POST /assistant/batches/{id}/apply``.

Revision ID: 20261126_20
Revises: 20261126_19
"""
import sqlalchemy as sa
from alembic import op

revision = "20261126_20"
down_revision = "20261126_19"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "assistant_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("proposal_batches.id"), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="running"),
        sa.Column("idempotency_key", sa.String(200)),
        sa.Column("approved_ids", sa.JSON(), nullable=False),
        sa.Column("applied", sa.JSON(), nullable=False),
        sa.Column("failed", sa.JSON(), nullable=False),
        sa.Column("discarded", sa.JSON(), nullable=False),
        sa.Column("started_at", timestamp, nullable=False),
        sa.Column("finished_at", timestamp),
        sa.UniqueConstraint("batch_id", "idempotency_key", name="uq_assistant_runs_batch_key"),
    )
    op.create_index("ix_assistant_runs_batch_id", "assistant_runs", ["batch_id"])


def downgrade():
    raise RuntimeError("v4 migrations are forward-only; restore a pre-upgrade backup")
