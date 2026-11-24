"""Debts, incomes and the official exchange rate (TRM); own accounts may be of kind ``loan``.

``own_accounts.kind`` is a free ``VARCHAR(20)`` validated by the API, so ``loan`` needs no schema change.

Revision ID: 20261126_21
Revises: 20261126_20
"""
import sqlalchemy as sa
from alembic import op

revision = "20261126_21"
down_revision = "20261126_20"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "debts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("lender", sa.String(100), nullable=False),
        sa.Column("firefly_account_id", sa.String(40), nullable=False),
        sa.Column("firefly_account", sa.String(255), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("original_amount", sa.Numeric(18, 2)),
        sa.Column("rate_ea", sa.Numeric(8, 4), nullable=False),
        sa.Column("term_months", sa.Integer()),
        sa.Column("start_date", sa.Date()),
        sa.Column("installment", sa.Numeric(18, 2)),
        sa.Column("payment_day", sa.Integer()),
        sa.Column("insurance_monthly", sa.Numeric(18, 2), nullable=False),
        sa.Column("credit_limit", sa.Numeric(18, 2)),
        sa.Column("minimum_payment", sa.Numeric(18, 2)),
        sa.Column("payment_match", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
    )
    op.create_index("ix_debts_active", "debts", ["active"])
    op.create_table(
        "incomes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("day_of_month", sa.Integer()),
        sa.Column("match", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
    )
    op.create_index("ix_incomes_active", "incomes", ["active"])
    op.create_table(
        "fx_rates",
        sa.Column("valid_from", sa.Date(), primary_key=True),
        sa.Column("valid_until", sa.Date(), nullable=False),
        sa.Column("rate", sa.Numeric(18, 4), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("fetched_at", timestamp, nullable=False),
    )
    op.add_column("inbox_items", sa.Column("foreign_amount", sa.Numeric(18, 2)))
    op.add_column("inbox_items", sa.Column("foreign_currency", sa.String(10)))


def downgrade():
    raise RuntimeError("v4 migrations are forward-only; restore a pre-upgrade backup")
