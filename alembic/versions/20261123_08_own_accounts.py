"""Own accounts managed from the app (v4, contract section 7).

Revision ID: 20261123_08
Revises: 20261122_07
"""
import sqlalchemy as sa
from alembic import op

revision = "20261123_08"
down_revision = "20261122_07"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "own_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("firefly_account", sa.String(255), nullable=False),
        sa.Column("bank", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("last4", sa.JSON(), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
    )
    # One account per Firefly name, ignoring case; a plain expression index works on SQLite and PostgreSQL.
    op.create_index("uq_own_accounts_firefly_account_lower", "own_accounts", [sa.text("lower(firefly_account)")], unique=True)


def downgrade():
    raise RuntimeError("v4 migrations are forward-only; restore a pre-upgrade backup")
