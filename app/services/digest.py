"""Owner's digest preference (``runtime_flags.digest_policy``) and the figures of the digest alerts.

Policy: ``{"daily": bool, "weekly": bool, "hour": 0-23, "silence_days": 1-30}``. Defaults: daily and weekly on, 20:00
America/Bogota, three silent days before ``capture_gap``. The scheduler ticks every hour and ``due`` decides what
to send; the digest is delivered the same evening, so the daily one covers today and the weekly one (Sundays) the
current Monday-to-Sunday week.

While the daily digest is on, ``new_charge`` alerts of automatically registered items below the auto-approval cap are
not emitted one by one: ``defer_charge`` keeps them (id and amount, at most ``MAX_GROUPED``) and the next daily digest
mentions and clears them. Functions never commit; callers own the transaction.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.core.timezone import now_utc
from app.db.models import InboxItem, MailAccountState, Notification
from app.db.models_flags import RuntimeFlag
from app.services import capture, reports

POLICY_KEY = "digest_policy"
GROUPED_KEY = "digest_grouped"
DEFAULT_POLICY = {"daily": True, "weekly": True, "hour": 20, "silence_days": 3}
MAX_GROUPED = 500
GROUPED_MAX_AGE = timedelta(days=3)
WEEKLY_WEEKDAY = 6  # Sunday
LEDGER_TYPES = ("withdrawal", "deposit", "transfer", "refund")


async def _read(session, key):
    return await session.scalar(select(RuntimeFlag.value).where(RuntimeFlag.key == key))


async def _write(session, key, value) -> None:
    row = await session.get(RuntimeFlag, key, populate_existing=True)
    if row is None:
        session.add(RuntimeFlag(key=key, value=value))
    else:
        row.value = value
    await session.flush()


async def policy(session) -> dict:
    """The stored policy over the defaults (a damaged row falls back to them field by field)."""
    stored = await _read(session, POLICY_KEY)
    result = dict(DEFAULT_POLICY)
    if isinstance(stored, dict):
        for name in ("daily", "weekly"):
            if isinstance(stored.get(name), bool):
                result[name] = stored[name]
        hour, days = stored.get("hour"), stored.get("silence_days")
        if isinstance(hour, int) and not isinstance(hour, bool) and 0 <= hour <= 23:
            result["hour"] = hour
        if isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= 30:
            result["silence_days"] = days
    return result


async def set_policy(session, *, daily: bool, weekly: bool, hour: int, silence_days: int | None = None) -> dict:
    """Store the policy (the caller validates ranges, audits and commits); ``silence_days`` None keeps the stored one."""
    current = await policy(session)
    value = {"daily": bool(daily), "weekly": bool(weekly), "hour": int(hour),
             "silence_days": current["silence_days"] if silence_days is None else int(silence_days)}
    await _write(session, POLICY_KEY, value)
    return value


def period(day: date, weekly: bool) -> tuple[date, date]:
    """First and last day (inclusive) of the day, or of the Monday-to-Sunday week, containing ``day``."""
    if weekly:
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=6)
    return day, day


async def sent(session, weekly: bool, start: date) -> bool:
    """Whether the digest of that period was already emitted (its anti-noise key exists)."""
    key = f"alert:digest:{'week' if weekly else 'day'}:{start}"
    return await session.scalar(select(Notification.key).where(Notification.key == key)) is not None


async def due(session, settings, now: datetime | None = None) -> list[bool]:
    """Which digests (``weekly`` flags) are due at ``now``: the hour has come and the period was not sent yet."""
    local = (now or now_utc()).astimezone(ZoneInfo(settings.timezone))
    current = await policy(session)
    wanted = []
    if current["daily"]:
        wanted.append(False)
    if current["weekly"] and local.weekday() == WEEKLY_WEEKDAY:
        wanted.append(True)
    if local.hour < current["hour"]:
        return []
    return [weekly for weekly in wanted if not await sent(session, weekly, period(local.date(), weekly)[0])]


async def defer_charge(session, item) -> None:
    """Keep an automatic charge for the daily digest instead of alerting about it."""
    now = now_utc()
    kept = [
        entry for entry in (await _read(session, GROUPED_KEY) or {}).get("items", [])
        if datetime.fromisoformat(entry["at"]) > now - GROUPED_MAX_AGE and entry["id"] != item.id
    ]
    kept.append({"id": item.id, "amount": str(item.amount or Decimal(0)), "at": now.isoformat()})
    await _write(session, GROUPED_KEY, {"items": kept[-MAX_GROUPED:]})


async def grouped(session) -> tuple[int, Decimal]:
    """Charges waiting for the daily digest: how many and their sum."""
    cutoff = now_utc() - GROUPED_MAX_AGE
    entries = [entry for entry in (await _read(session, GROUPED_KEY) or {}).get("items", []) if datetime.fromisoformat(entry["at"]) > cutoff]
    return len(entries), sum((Decimal(entry["amount"]) for entry in entries), Decimal(0))


async def clear_grouped(session) -> None:
    await _write(session, GROUPED_KEY, {"items": []})


async def should_group(session, settings, item) -> bool:
    """True when this automatic charge belongs to the daily digest: it is on and the amount is under the auto-approval cap."""
    from app.services import flags

    if not (await policy(session))["daily"] or item.amount is None:
        return False
    return item.amount < (await flags.policy(session, settings))["max_amount_cop"]


async def capture_failures(session, settings, begin: datetime, finish: datetime) -> list[str]:
    """Capture failures worth a line in the digest: failed items of the period, failing mailboxes and the LLM."""
    found = []
    failed = await session.scalar(select(func.count()).select_from(InboxItem).where(
        InboxItem.status == "failed", InboxItem.created_at >= begin, InboxItem.created_at < finish))
    if failed:
        found.append(f"{failed} movimiento{'s' if failed != 1 else ''} no se pudo registrar")
    names = {account.name for account in settings.mail_accounts}
    for state in (await session.scalars(select(MailAccountState).order_by(MailAccountState.name))).all():
        if state.name in names and state.consecutive_failures >= capture.FAILURE_STREAK:
            found.append(f"el buzón «{state.name}» falla al consultarse")
    if await capture.llm_streak(session) >= capture.FAILURE_STREAK:
        found.append("las últimas extracciones con IA fallaron")
    return found


async def stats(session, settings, day: date, weekly: bool) -> dict:
    """Figures of the digest of the period containing ``day`` (see ``alerts.digest`` for the text)."""
    zone = ZoneInfo(settings.timezone)
    start, end = period(day, weekly)
    length = (end - start).days + 1
    before_start, before_end = start - timedelta(days=length), start - timedelta(days=1)
    rows = await reports._ledger(session, settings, start, end, None, LEDGER_TYPES, cop_only=False)
    spending = [row for row in rows if row.transaction_type == "withdrawal" and (row.currency or "COP") == "COP"]
    categories: dict[str, Decimal] = {}
    for row in spending:
        categories[row.category or "Sin categoría"] = categories.get(row.category or "Sin categoría", Decimal(0)) + row.amount
    previous = await reports._ledger(session, settings, before_start, before_end, None, ("withdrawal",), cop_only=True)
    begin = datetime.combine(start, datetime.min.time(), zone)
    finish = datetime.combine(end + timedelta(days=1), datetime.min.time(), zone)
    review = await session.scalar(select(func.count()).select_from(InboxItem).where(InboxItem.status == "needs_review"))
    return {
        "start": start, "weekly": weekly, "registered": len(rows),
        "spent": sum((row.amount for row in spending), Decimal(0)),
        "previous_spent": sum((row.amount for row in previous), Decimal(0)),
        "categories": sorted(categories.items(), key=lambda pair: (-pair[1], pair[0]))[:3],
        "review": review or 0,
        "failures": await capture_failures(session, settings, begin, finish),
        "auto": await grouped(session) if not weekly else None,
    }
