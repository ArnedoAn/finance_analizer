"""Helpers shared by every domain router: DB/service dependencies and the inbox field allowlist."""
import re
from typing import Annotated

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_processor
from app.db.repositories import InboxRepository
from app.security.redaction import redact
from app.services import descriptions
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
# Free text derived from raw messages (subject, user text, model reasons, upstream errors, sender): redacted on output.
REDACTED_ITEM_FIELDS = ("subject_or_title", "description", "review_reason", "last_error", "sender")
MAX_DIAGNOSTIC_CHARS = 300
DIAGNOSTIC_FIELDS = ("review_reason", "last_error")


async def get_item(db, item_id):
    item = await InboxRepository(db).get(item_id)
    if item is None:
        raise HTTPException(404, "Inbox item not found")
    return item


def _cap(text, limit):
    """Cut to ``limit`` characters without leaving half a placeholder such as ``[CUENTA-12``."""
    if text is None or len(text) <= limit:
        return text
    return re.sub(r"\[[A-Z0-9-]*$", "", text[:limit])


def item_data(item, *, redacted_raw=False, include_raw=False):
    """Public inbox view and the ONLY exit point of inbox item data towards API responses.

    ``subject_or_title``, ``description``, ``review_reason``, ``last_error`` and ``sender`` can quote the
    original message (an e-mail subject, a model's reasoning, an upstream error echo), so they go through
    ``redact`` here unless ``include_raw`` is set, which only review-scoped callers (``review`` scope, see
    ``routers/inbox.py``) may do. ``review_reason`` and ``last_error`` are also cut to 300 characters in every
    case. ``redacted_raw`` adds ``raw_text_redacted``; ``include_raw`` adds the unredacted ``raw_text``.
    New routers must build item payloads with this function, never with ``getattr`` on the model.
    """
    data = {name: getattr(item, name) for name in PUBLIC_ITEM_FIELDS}
    data["tags"] = list(data["tags"] or [])
    if not include_raw:
        for name in REDACTED_ITEM_FIELDS:
            data[name] = redact(data[name])
    for name in DIAGNOSTIC_FIELDS:
        data[name] = _cap(data[name], MAX_DIAGNOSTIC_CHARS)
    if item.description and descriptions.is_generic_subject(item.sender, item.subject_or_title):
        # The same subject in every e-mail of a bank tells movements apart no better than nothing: show the derived description.
        data["subject_or_title"] = data["description"]
    # Contract section 14: money is a decimal string ("12345.00"), never a JSON number.
    if data["amount"] is not None:
        data["amount"] = f"{data['amount']:.2f}"
    if redacted_raw:
        data["raw_text_redacted"] = redact(item.raw_text)
    if include_raw:
        data["raw_text"] = item.raw_text
    return data
