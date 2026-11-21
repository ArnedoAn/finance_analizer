"""Per-device API keys and one-time enrollment codes (v4 phase 1).

Revision ID: 20261122_01
Revises: 20261116_01
"""
import sqlalchemy as sa
from alembic import op

revision = "20261122_01"
down_revision = "20261116_01"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "devices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("last_used_at", timestamp),
        sa.Column("revoked_at", timestamp),
        sa.Column("expires_at", timestamp),
    )
    op.create_index("ix_devices_key_hash", "devices", ["key_hash"], unique=True)
    op.create_table(
        "enroll_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("expires_at", timestamp, nullable=False),
        sa.Column("used_at", timestamp),
    )
    op.create_index("ix_enroll_codes_code_hash", "enroll_codes", ["code_hash"], unique=True)


def downgrade():
    raise RuntimeError("Device keys are forward-only; restore a pre-upgrade backup")
