"""Manual ledger edit history for undo (v4, contract section 15).

Revision ID: 20261122_07
Revises: 20261122_06
"""
import sqlalchemy as sa
from alembic import op

revision = "20261122_07"
down_revision = "20261122_06"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ledger_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("group_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("firefly_id", sa.String(100), nullable=False),
        sa.Column("client_ref", sa.String(80), unique=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("device_id", sa.Integer()),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("before_version", sa.JSON()),
        sa.Column("after_version", sa.JSON()),
        sa.Column("inbox_id", sa.Integer()),
        sa.Column("inbox_before", sa.JSON()),
        sa.Column("undone_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_ledger_changes_group_id", "ledger_changes", ["group_id"])
    op.create_index("ix_ledger_changes_firefly_id", "ledger_changes", ["firefly_id"])
    op.create_index("ix_ledger_changes_at", "ledger_changes", ["at"])


def downgrade():
    raise RuntimeError("v4 migrations are forward-only; restore a pre-upgrade backup")
