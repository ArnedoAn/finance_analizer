"""Rules can add tags to the movements they match.

Revision ID: 20261126_19
Revises: 20261126_18
"""
import sqlalchemy as sa
from alembic import op

revision = "20261126_19"
down_revision = "20261126_18"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("rules") as batch:
        batch.add_column(sa.Column("tags", sa.JSON()))


def downgrade():
    raise RuntimeError("v4 migrations are forward-only; restore a pre-upgrade backup")
