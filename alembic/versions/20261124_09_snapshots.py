"""Dashboard snapshots and the persistent server error buffer.

Revision ID: 20261124_09
Revises: 20261123_08
"""
import sqlalchemy as sa
from alembic import op

revision = "20261124_09"
down_revision = "20261123_08"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "dashboard_snapshots",
        sa.Column("month", sa.String(7), primary_key=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("computed_at", timestamp, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_table(
        "server_errors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", timestamp, nullable=False),
        sa.Column("component", sa.String(20), nullable=False),
        sa.Column("operation", sa.String(100), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("exc_type", sa.String(100)),
        sa.Column("status_code", sa.Integer()),
        sa.Column("response_body", sa.Text()),
    )
    op.create_index("ix_server_errors_at", "server_errors", ["at"])
    op.create_index("ix_server_errors_component", "server_errors", ["component"])


def downgrade():
    raise RuntimeError("v4 migrations are forward-only; restore a pre-upgrade backup")
