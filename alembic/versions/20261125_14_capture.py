"""Capture: photographed receipts, utility bills and the queue of bill-looking e-mails (v4).

Revision ID: 20261125_14
Revises: 20261124_13
"""
import sqlalchemy as sa
from alembic import op

revision = "20261125_14"
down_revision = "20261124_13"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(60)),
        sa.Column("image_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("item_id", sa.Integer()),
        sa.Column("match_kind", sa.String(10)),
        sa.Column("total", sa.Numeric(18, 2)),
        sa.Column("receipt_date", sa.Date()),
        sa.Column("model", sa.String(100)),
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("data", sa.Text()),
        sa.Column("applied", sa.JSON()),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
    )
    op.create_index("ix_receipts_status", "receipts", ["status"])
    op.create_index("ix_receipts_item_id", "receipts", ["item_id"])
    op.create_index("ix_receipts_created_at", "receipts", ["created_at"])
    op.create_table(
        "bills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity", sa.String(255), nullable=False),
        sa.Column("entity_key", sa.String(255), nullable=False),
        sa.Column("service", sa.String(20), nullable=False),
        sa.Column("reference", sa.String(40)),
        sa.Column("period", sa.String(40)),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("issued_on", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("paid_item_id", sa.Integer()),
        sa.Column("paid_at", timestamp),
        sa.Column("source", sa.String(10), nullable=False, server_default="manual"),
        sa.Column("source_item_id", sa.Integer()),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.UniqueConstraint("entity_key", "due_date", "amount", name="uq_bills_entity_due_amount"),
    )
    op.create_index("ix_bills_entity_key", "bills", ["entity_key"])
    op.create_index("ix_bills_due_date", "bills", ["due_date"])
    op.create_index("ix_bills_status", "bills", ["status"])
    op.create_index("ix_bills_paid_item_id", "bills", ["paid_item_id"])
    op.create_table(
        "bill_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("sender", sa.String(512), nullable=False, server_default=""),
        sa.Column("received_on", sa.Date(), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
    )
    op.create_index("ix_bill_candidates_status", "bill_candidates", ["status"])


def downgrade():
    raise RuntimeError("v4 migrations are forward-only; restore a pre-upgrade backup")
