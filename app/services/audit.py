"""Append-only audit log (contract section 9). Callers own the commit.

``detail`` is stored as JSON and must describe *what changed*, never raw message text or
credentials. As a safety net, values under secret-looking keys are dropped and long strings
are truncated.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select

from app.core.timezone import now_utc
from app.db.models_assistant import Audit

_FORBIDDEN_KEYS = {"raw_text", "raw", "text", "key", "api_key", "token", "password", "secret", "authorization"}
_MAX_STRING = 500


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items() if str(k).lower() not in _FORBIDDEN_KEYS}
    if isinstance(value, list | tuple | set):
        return [_clean(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value[:_MAX_STRING]
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)[:_MAX_STRING]


async def record(session, *, action: str, target_type: str, target_id, detail: dict | None = None, principal=None) -> None:
    """Stage one audit row; the caller commits. ``principal`` is the authenticated caller, if any."""
    session.add(Audit(
        at=now_utc(),
        device_id=getattr(principal, "device_id", None),
        actor=getattr(principal, "name", None) or "system",
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        detail=_clean(detail or {}),
    ))


async def entries(session, *, since: datetime | None = None, action: str | None = None, limit: int = 50, offset: int = 0) -> tuple[list[Audit], int]:
    """Newest first. ``since`` is inclusive; ``action`` matches exactly."""
    query = select(Audit)
    if since is not None:
        query = query.where(Audit.at >= since)
    if action:
        query = query.where(Audit.action == action)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(query.order_by(Audit.id.desc()).limit(limit).offset(offset))
    return list(rows.all()), total
