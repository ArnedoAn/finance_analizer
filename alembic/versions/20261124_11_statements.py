"""Bank statement reconciliation: extracted rows and their result (v4, phase 9).

Revision ID: 20261124_11
Revises: 20261124_10
"""
import sqlalchemy as sa
from alembic import op

revision = "20261124_11"
down_revision = "20261124_10"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "statements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("own_account_id", sa.Integer(), nullable=False),
        sa.Column("account_name", sa.String(255), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(60)),
        sa.Column("period_from", sa.Date()),
        sa.Column("period_to", sa.Date()),
        sa.Column("rows_count", sa.Integer(), nullable=False),
        sa.Column("discarded_rows", sa.Integer(), nullable=False),
        sa.Column("llm_calls", sa.Integer(), nullable=False),
        sa.Column("totals", sa.JSON()),
        sa.Column("summary", sa.JSON()),
        sa.Column("extra_json", sa.Text()),
        sa.Column("reconciled_at", timestamp),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
    )
    op.create_index("ix_statements_own_account_id", "statements", ["own_account_id"])
    op.create_index("ix_statements_status", "statements", ["status"])
    op.create_index("ix_statements_created_at", "statements", ["created_at"])
    op.create_index("uq_statements_account_file", "statements", ["own_account_id", "file_sha256"], unique=True)
    op.create_table(
        "statement_rows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("statement_id", sa.Integer(), sa.ForeignKey("statements.id", ondelete="CASCADE"), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("direction", sa.String(6), nullable=False),
        sa.Column("balance", sa.Numeric(18, 2)),
        sa.Column("match_status", sa.String(12)),
        sa.Column("firefly_id", sa.String(100)),
        sa.Column("firefly_day", sa.Date()),
        sa.Column("firefly_amount", sa.Numeric(18, 2)),
        sa.Column("firefly_description", sa.Text()),
        sa.Column("inbox_id", sa.Integer()),
    )
    op.create_index("ix_statement_rows_statement_id", "statement_rows", ["statement_id"])


def downgrade():
    raise RuntimeError("v4 migrations are forward-only; restore a pre-upgrade backup")
