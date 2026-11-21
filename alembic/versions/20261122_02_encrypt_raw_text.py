"""Encrypt existing inbox_items.raw_text with AES-256-GCM (v4 phase 2).

Rows already carrying the ``enc:v1:`` prefix are left alone (idempotent rerun); every other non-null row is
plaintext and is sealed as ``enc:v1:<kid>:<base64>`` with the active key. The prefix is compared with
``substr`` so the check is case-sensitive on every dialect.

Revision ID: 20261122_02
Revises: 20261122_01
"""
import sqlalchemy as sa
from alembic import op

from app.core.secrets import read_secret
from app.security.crypto import VERSION_PREFIX, encrypt, parse_key

revision = "20261122_02"
down_revision = "20261122_01"
branch_labels = None
depends_on = None

BATCH_SIZE = 500


def _load_key() -> bytes:
    try:
        value = read_secret("FIELD_ENCRYPTION_KEY")
    except ValueError as exc:
        raise RuntimeError(str(exc)) from None
    if not value or not value.strip():
        raise RuntimeError(
            "inbox_items has raw_text rows to encrypt but no key is configured: set "
            "FIELD_ENCRYPTION_KEY_FILE (or FIELD_ENCRYPTION_KEY) to a base64 32-byte key and rerun; "
            "no data was changed"
        )
    try:
        return parse_key(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid field encryption key: {exc}; no data was changed") from None


def upgrade():
    connection = op.get_bind()
    inbox = sa.table("inbox_items", sa.column("id", sa.Integer), sa.column("raw_text", sa.Text))
    plaintext = sa.and_(
        inbox.c.raw_text.is_not(None),
        sa.func.substr(inbox.c.raw_text, 1, len(VERSION_PREFIX)) != VERSION_PREFIX,
    )
    if not connection.scalar(sa.select(sa.func.count()).select_from(inbox).where(plaintext)):
        return
    key = _load_key()
    update = (
        sa.update(inbox)
        .where(inbox.c.id == sa.bindparam("row_id"))
        .values(raw_text=sa.bindparam("ciphertext"))
    )
    last_id = 0
    while True:
        rows = connection.execute(
            sa.select(inbox.c.id, inbox.c.raw_text)
            .where(plaintext, inbox.c.id > last_id)
            .order_by(inbox.c.id)
            .limit(BATCH_SIZE)
        ).all()
        if not rows:
            break
        connection.execute(update, [{"row_id": row.id, "ciphertext": encrypt(row.raw_text, key)} for row in rows])
        last_id = rows[-1].id


def downgrade():
    raise RuntimeError("Field encryption is forward-only; restore a pre-upgrade backup")
