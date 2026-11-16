"""Freeze v3 schema; adopt an existing single-owner v2 database without data loss.

Revision ID: 20261116_01
Revises: none
"""
import sqlalchemy as sa
from alembic import op

revision = "20261116_01"
down_revision = None
branch_labels = None
depends_on = None


def schema():
    metadata = sa.MetaData()
    timestamp = sa.DateTime(timezone=True)
    sa.Table(
        "inbox_items", metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(20), nullable=False, index=True),
        sa.Column("source_ref", sa.String(512), nullable=False, unique=True),
        sa.Column("account_name", sa.String(255)),
        sa.Column("message_id", sa.String(512), index=True),
        sa.Column("app", sa.String(255)),
        sa.Column("sender", sa.String(512), nullable=False),
        sa.Column("subject_or_title", sa.Text(), nullable=False),
        sa.Column("raw_text", sa.Text()),
        sa.Column("occurred_at", timestamp, nullable=False, index=True),
        sa.Column("status", sa.String(20), nullable=False, index=True),
        sa.Column("method", sa.String(20)),
        sa.Column("amount", sa.Numeric(18, 2)),
        sa.Column("currency", sa.String(10)),
        sa.Column("merchant", sa.String(512)),
        sa.Column("category", sa.String(255)),
        sa.Column("asset_account", sa.String(255)),
        sa.Column("transaction_type", sa.String(20)),
        sa.Column("destination_account", sa.String(255)),
        sa.Column("original_kind", sa.String(20)),
        sa.Column("template_id", sa.String(255)),
        sa.Column("review_reason", sa.Text()),
        sa.Column("claim_token", sa.String(36)),
        sa.Column("claim_until", timestamp),
        sa.Column("firefly_transaction_id", sa.String(100)),
        sa.Column("duplicate_of_id", sa.Integer(), sa.ForeignKey("inbox_items.id")),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", timestamp, index=True),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
    )
    sa.Table(
        "mail_account_state", metadata,
        sa.Column("name", sa.String(255), primary_key=True),
        sa.Column("uidvalidity", sa.String(100)),
        sa.Column("last_uid", sa.Integer(), nullable=False),
        sa.Column("last_poll_at", timestamp),
        sa.Column("last_error", sa.Text()),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
    )
    sa.Table(
        "known_senders", metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("keyword", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("require_auth", sa.Boolean(), nullable=False),
    )
    sa.Table(
        "inbox_events", metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("inbox_id", sa.Integer(), sa.ForeignKey("inbox_items.id"), nullable=False, index=True),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column("created_at", timestamp, nullable=False),
    )
    sa.Table(
        "cycle_runs", metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", timestamp, nullable=False),
        sa.Column("finished_at", timestamp),
        sa.Column("result", sa.Text()),
    )
    sa.Table(
        "notifications", metadata,
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("last_sent_at", timestamp, nullable=False),
    )
    return metadata


def upgrade():
    connection = op.get_bind()
    metadata = schema()
    existing = set(sa.inspect(connection).get_table_names()) - {"alembic_version"}
    legacy = existing - set(metadata.tables)
    if legacy:
        raise RuntimeError("Not a single-owner v2 database; migrate to a separate database, not in place")
    additions = {
        "inbox_items": {"message_id", "destination_account", "original_kind", "template_id", "review_reason", "claim_token", "claim_until"},
        "mail_account_state": {"consecutive_failures"},
        "known_senders": {"status", "require_auth"},
    }
    # Validate before any DDL (especially important for SQLite's nontransactional DDL).
    for name in existing:
        present = {column["name"] for column in sa.inspect(connection).get_columns(name)}
        required = set(metadata.tables[name].columns.keys()) - additions.get(name, set())
        if not required.issubset(present) or "session_id" in present:
            raise RuntimeError(f"Unsupported pre-v2 schema in {name}; restore backup and use a separate database")
    for table in metadata.sorted_tables:
        if table.name not in existing:
            table.create(connection)
            continue
        present = {column["name"] for column in sa.inspect(connection).get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            default = {"consecutive_failures": sa.text("0"), "status": sa.text("'pending_approval'"), "require_auth": sa.true()}.get(column.name)
            op.add_column(table.name, sa.Column(column.name, column.type, nullable=column.nullable, server_default=default))
        indexes = {index["name"] for index in sa.inspect(connection).get_indexes(table.name)}
        for index in table.indexes:
            if index.name not in indexes:
                index.create(connection)
    if "known_senders" in existing:
        # Old substring rules and automatically learned domains require explicit approval.
        op.execute(sa.text("UPDATE known_senders SET status = 'pending_approval', require_auth = true"))


def downgrade():
    raise RuntimeError("The v3 baseline is data-preserving and irreversible; restore a pre-upgrade backup")
