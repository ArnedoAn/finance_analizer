"""Assistant proposals that are not about an inbox item (categories, ledger movements, budgets, rules).

Revision ID: 20261126_18
Revises: 20261125_17
"""
import sqlalchemy as sa
from alembic import op

revision = "20261126_18"
down_revision = "20261125_17"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("proposals") as batch:
        batch.alter_column("target_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("target_ref", sa.String(255)))
        batch.add_column(sa.Column("subject", sa.String(255)))
        batch.add_column(sa.Column("result", sa.JSON()))


def downgrade():
    raise RuntimeError("v4 migrations are forward-only; restore a pre-upgrade backup")
