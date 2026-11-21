"""Runtime flags (auto-approval pause after a suspicious report).

Revision ID: 20261122_06
Revises: 20261122_05
"""
import sqlalchemy as sa
from alembic import op

revision = "20261122_06"
down_revision = "20261122_05"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runtime_flags",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    raise RuntimeError("Migrations are forward-only; restore a pre-upgrade backup")
