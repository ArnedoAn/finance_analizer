"""Helpers shared by every domain router: DB/service dependencies and the inbox field allowlist."""
from typing import Annotated

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_processor
from app.db.repositories import InboxRepository
from app.security.redaction import redact
from app.services.processor import Processor

DB = Annotated[AsyncSession, Depends(get_db)]
Service = Annotated[Processor, Depends(get_processor)]

PUBLIC_ITEM_FIELDS = (
    "id", "source", "source_ref", "account_name", "message_id", "app", "sender",
    "subject_or_title", "occurred_at", "status", "method", "amount", "currency",
    "merchant", "category", "asset_account", "transaction_type", "destination_account",
    "original_kind", "template_id", "review_reason", "firefly_transaction_id",
    "duplicate_of_id", "attempts", "next_attempt_at", "last_error", "created_at", "updated_at",
    "description", "tags",
)


async def get_item(db, item_id):
    item = await InboxRepository(db).get(item_id)
    if item is None:
        raise HTTPException(404, "Inbox item not found")
    return item


def item_data(item, *, redacted_raw=False, include_raw=False):
    """Public inbox view. ``redacted_raw`` adds ``raw_text_redacted`` (single redaction point);
    ``include_raw`` adds the unredacted ``raw_text`` and must only be set for review-scoped callers."""
    data = {name: getattr(item, name) for name in PUBLIC_ITEM_FIELDS}
    data["tags"] = list(data["tags"] or [])
    # Contract section 14: money is a decimal string ("12345.00"), never a JSON number.
    if data["amount"] is not None:
        data["amount"] = f"{data['amount']:.2f}"
    if redacted_raw:
        data["raw_text_redacted"] = redact(item.raw_text)
    if include_raw:
        data["raw_text"] = item.raw_text
    return data
