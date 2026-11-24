"""«No avisar de X menor a Y»: filters that ``alerts.emit`` consults before inserting an alert.

A mute drops an alert when its type is mutable, it carries an amount strictly below ``below_amount`` and, if the
mute names a merchant, the alert's inbox item has that normalized merchant. Alerts without amount are never muted.
Operational alerts (failures, backups, keys, capture gaps, suspicious charges, summaries) can never be muted.
"""
from decimal import Decimal

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models import AlertMute, InboxItem
from app.services import rules

logger = get_logger(__name__)

MUTABLE_TYPES = (
    "pending_review", "new_charge", "duplicate_suspect", "anomaly", "new_subscription", "price_increase", "question", "big_amount",
)


def mute_data(row: AlertMute) -> dict:
    return {
        "id": row.id, "alert_type": row.alert_type, "merchant": row.merchant, "below_amount": f"{row.below_amount:.2f}",
        "active": row.active, "memory_id": row.memory_id, "created_at": row.created_at,
    }


async def is_muted(session, alert_type: str, *, item_id=None, amount=None) -> bool:
    """Whether an active mute covers this alert. Fails open: a broken lookup must never swallow alerts."""
    if amount is None or alert_type not in MUTABLE_TYPES:
        return False
    try:
        value = Decimal(str(amount))
        mutes = (await session.scalars(select(AlertMute).where(AlertMute.active.is_(True), AlertMute.below_amount > value))).all()
        mutes = [mute for mute in mutes if mute.alert_type in (None, alert_type)]
        if not mutes:
            return False
        if any(mute.merchant is None for mute in mutes):
            return True
        merchant = await session.scalar(select(InboxItem.merchant).where(InboxItem.id == item_id)) if item_id is not None else None
        name = rules.merchant_name(merchant)
        return bool(name) and any(mute.merchant == name for mute in mutes)
    except Exception as exc:
        logger.warning("alert_mute_check_failed", error_type=type(exc).__name__)
        return False
