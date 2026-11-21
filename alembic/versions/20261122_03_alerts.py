"""Alerts polled by the app instead of bot pushes (v4 phase 3).

Revision ID: 20261122_03
Revises: 20261122_02
"""
import sqlalchemy as sa
from alembic import op

revision = "20261122_03"
down_revision = "20261122_02"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("item_id", sa.Integer()),
        sa.Column("amount", sa.Numeric(18, 2)),
        sa.Column("currency", sa.String(10)),
        sa.Column("suggested_categories", sa.JSON()),
        sa.Column("question", sa.Text()),
        sa.Column("options", sa.JSON()),
        sa.Column("dedupe_key", sa.String(255)),
        sa.Column("acked_at", timestamp),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_alerts_type", "alerts", ["type"])
    op.create_index("ix_alerts_item_id", "alerts", ["item_id"])
    op.create_index("ix_alerts_acked_at", "alerts", ["acked_at"])


def downgrade():
    raise RuntimeError("Alerts are forward-only; restore a pre-upgrade backup")
