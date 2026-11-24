"""Browser view of the enrollment QR: only the hash of the view token is stored.

Revision ID: 20261126_22
Revises: 20261126_21
"""
import sqlalchemy as sa
from alembic import op

revision = "20261126_22"
down_revision = "20261126_21"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "enroll_qr_views",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("expires_at", timestamp, nullable=False),
    )
    op.create_index("ix_enroll_qr_views_token_hash", "enroll_qr_views", ["token_hash"], unique=True)


def downgrade():
    raise RuntimeError("Forward-only migration; restore a pre-upgrade backup")
