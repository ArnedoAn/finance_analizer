"""Forecast, subscriptions, period comparison, capture status and subscription alerts (contract sections 4, 6, 14).

Everything is computed from the analyzer ledger: ``inbox_items`` with ``status == "created"``, bucketed in
``settings.timezone``. Only ``withdrawal`` is spending; ``deposit`` and ``refund`` are income; transfers are ignored.
Money is ``Decimal`` internally and serialized as two-decimal strings. Forecast and comparison are COP-only so
amounts in different currencies are never summed.
"""
import calendar
import statistics
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.core.timezone import now_utc
from app.db.models import InboxItem

CENT = Decimal("0.01")
ZERO = Decimal(0)
SPENDING = ("withdrawal",)
INCOME = ("deposit", "refund")
LOOKBACK_DAYS = 400
NEW_WINDOW_DAYS = 35
PRICE_INCREASE_RATIO = Decimal("1.05")
MAX_VARIATION = Decimal("0.20")
PHONE_SOURCES = ("sms", "notification")
CAPTURE_SOURCES = ("sms", "notification", "email")


def _money(value: Decimal) -> str:
    return f"{value.quantize(CENT, ROUND_HALF_UP):.2f}"


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    return numerator / denominator * 100


def _spanish_amount(value: Decimal, currency: str) -> str:
    return f"{value.quantize(CENT, ROUND_HALF_UP):,.2f}".translate(str.maketrans(",.", ".,")) + f" {currency}"


def _zone(settings) -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def _local_day(settings, day: date | None) -> date:
    return day or now_utc().astimezone(_zone(settings)).date()


def _add_months(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _merchant_key(row) -> tuple[str, str] | None:
    if not row.merchant or not row.merchant.strip():
        return None
    return " ".join(row.merchant.casefold().split()), row.currency or "COP"


async def _ledger(session, settings, first: date, last: date, tag: str | None, types, *, cop_only: bool) -> list[InboxItem]:
    """Created ledger rows whose local date is within ``first..last`` inclusive, oldest first."""
    zone = _zone(settings)
    begin = datetime.combine(first, time.min, zone).astimezone(UTC)
    finish = datetime.combine(last + timedelta(days=1), time.min, zone).astimezone(UTC)
    rows = (await session.scalars(
        select(InboxItem)
        .where(
            InboxItem.status == "created", InboxItem.amount.is_not(None), InboxItem.transaction_type.in_(types),
            InboxItem.occurred_at >= begin, InboxItem.occurred_at < finish,
        )
        .order_by(InboxItem.occurred_at, InboxItem.id)
    )).all()
    wanted = tag.strip().casefold() if tag and tag.strip() else None
    return [
        row for row in rows
        if (not cop_only or (row.currency or "COP") == "COP")
        and (wanted is None or wanted in {str(t).casefold() for t in row.tags or []})
    ]


def _cadence(gap: int) -> str | None:
    if 6 <= gap <= 8:
        return "weekly"
    if 28 <= gap <= 35:
        return "monthly"
    if 360 <= gap <= 370:
        return "annual"
    return None


def _recurrence(charges: list[InboxItem], zone: ZoneInfo, day: date) -> dict | None:
    """Detect a regular series at the tail of one merchant's charges (oldest first)."""
    dates = [charge.occurred_at.astimezone(zone).date() for charge in charges]
    gaps = [(later - earlier).days for earlier, later in zip(dates, dates[1:], strict=False)]
    if not gaps or (kind := _cadence(gaps[-1])) is None:
        return None
    start = len(gaps) - 1
    while start > 0 and _cadence(gaps[start - 1]) == kind:
        start -= 1
    run, run_dates, run_gaps = charges[start:], dates[start:], gaps[start:]
    # MAX_VARIATION bounds the amounts *before* the last one; the last is judged against
    # PRICE_INCREASE_RATIO so a jump above 20 % is reported as ``price_increase`` instead of hiding the series.
    prior = [charge.amount for charge in run[:-1]]
    low = min(prior)
    if low <= 0 or (max(prior) - low) / low > MAX_VARIATION:
        return None
    last, previous = run[-1], run[-2]
    last_date = run_dates[-1]
    if kind == "weekly":
        next_date = last_date + timedelta(days=7)
    else:
        next_date = _add_months(last_date, 1 if kind == "monthly" else 12)
    period_days = round(statistics.median(run_gaps))
    if day > next_date + timedelta(days=period_days):
        return None  # more than one full cycle overdue: treated as cancelled
    if last.amount > previous.amount * PRICE_INCREASE_RATIO:
        status = "price_increase"
    elif (day - run_dates[0]).days <= NEW_WINDOW_DAYS:
        status = "new"
    else:
        status = "active"
    return {
        "merchant": last.merchant, "category": last.category, "currency": last.currency or "COP",
        "amount": last.amount, "previous_amount": previous.amount,
        "change_pct": _pct(last.amount - previous.amount, previous.amount),
        "period_days": period_days, "occurrences": len(run), "last_date": last_date, "next_date": next_date,
        "status": status, "cadence": kind, "key": _merchant_key(last), "item_id": last.id,
    }


async def _detect(session, settings, day: date, tag: str | None) -> list[dict]:
    rows = await _ledger(session, settings, day - timedelta(days=LOOKBACK_DAYS), day, tag, SPENDING, cop_only=False)
    groups = defaultdict(list)
    for row in rows:
        if (key := _merchant_key(row)) is not None and row.amount > 0:
            groups[key].append(row)
    zone = _zone(settings)
    found = [sub for charges in groups.values() if (sub := _recurrence(charges, zone, day))]
    return sorted(found, key=lambda sub: (sub["next_date"], sub["merchant"]))


def _public_subscription(sub: dict) -> dict:
    return {
        "merchant": sub["merchant"], "category": sub["category"], "currency": sub["currency"],
        "amount": _money(sub["amount"]), "previous_amount": _money(sub["previous_amount"]),
        "change_pct": _money(sub["change_pct"]), "period_days": sub["period_days"],
        "occurrences": sub["occurrences"], "last_date": sub["last_date"].isoformat(),
        "next_date": sub["next_date"].isoformat(), "status": sub["status"],
    }


async def subscriptions(session, settings, day: date | None = None, tag: str | None = None) -> dict:
    day = _local_day(settings, day)
    return {"items": [_public_subscription(sub) for sub in await _detect(session, settings, day, tag)]}


def _remaining_charges(sub: dict, day: date, first: date, month_end: date) -> int:
    """Charges of a subscription still expected between tomorrow and the end of the month."""
    if sub["currency"] != "COP":
        return 0
    if sub["cadence"] == "weekly":
        due, count = sub["next_date"], 0
        while due <= month_end:
            count += due > day
            due += timedelta(days=7)
        return count
    return int(sub["last_date"] < first and day < sub["next_date"] <= month_end)


async def forecast(session, settings, day: date | None = None, tag: str | None = None) -> dict:
    day = _local_day(settings, day)
    first = day.replace(day=1)
    days_in_month = calendar.monthrange(day.year, day.month)[1]
    month_end = day.replace(day=days_in_month)
    days_elapsed, days_left = day.day, days_in_month - day.day
    rows = await _ledger(session, settings, first, day, tag, SPENDING + INCOME, cop_only=True)
    subs = await _detect(session, settings, day, tag)
    recurring = {sub["key"] for sub in subs}
    zone = _zone(settings)
    income = spent = spent_today = variable = ZERO
    for row in rows:
        if row.transaction_type in INCOME:
            income += row.amount
            continue
        spent += row.amount
        if row.occurred_at.astimezone(zone).date() == day:
            spent_today += row.amount
        if _merchant_key(row) not in recurring:
            variable += row.amount
    projected_fixed = sum((sub["amount"] * _remaining_charges(sub, day, first, month_end) for sub in subs), ZERO)
    projected_spent = spent + projected_fixed + variable / days_elapsed * days_left
    return {
        "month": f"{day.year:04d}-{day.month:02d}", "as_of": day.isoformat(), "currency": "COP",
        "income": _money(income), "spent": _money(spent), "balance": _money(income - spent),
        "projected_spent": _money(projected_spent), "projected_balance": _money(income - projected_spent),
        "projected_fixed": _money(projected_fixed),
        "days_elapsed": days_elapsed, "days_left": days_left,
        # On the last day nothing is left to divide over, so the remainder is today's allowance.
        "daily_available": _money(max(ZERO, income - projected_fixed - spent) / max(days_left, 1)),
        "spent_today": _money(spent_today),
    }


async def _category_totals(session, settings, first: date, last: date, tag: str | None) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for row in await _ledger(session, settings, first, last, tag, SPENDING, cop_only=True):
        totals[row.category or "Sin categoría"] += row.amount
    return totals


async def compare(session, settings, a_from: date, a_to: date, b_from: date, b_to: date, tag: str | None = None) -> dict:
    """Spending per category between two inclusive local-date ranges; ``diff = b - a``."""
    if a_from > a_to or b_from > b_to:
        raise ValueError("Range start must not be after its end")
    a = await _category_totals(session, settings, a_from, a_to, tag)
    b = await _category_totals(session, settings, b_from, b_to, tag)
    categories = []
    for name in sorted(a.keys() | b.keys()):
        before, after = a.get(name, ZERO), b.get(name, ZERO)
        diff = after - before
        categories.append({
            "category": name, "a": _money(before), "b": _money(after), "diff": _money(diff),
            "diff_pct": _money(_pct(diff, before)) if before else None,
        })
    categories.sort(key=lambda entry: (-abs(Decimal(entry["diff"])), entry["category"]))
    return {
        "a": {"from": a_from.isoformat(), "to": a_to.isoformat(), "total": _money(sum(a.values(), ZERO))},
        "b": {"from": b_from.isoformat(), "to": b_to.isoformat(), "total": _money(sum(b.values(), ZERO))},
        "categories": categories,
    }


async def alert_check(session, settings, firefly=None) -> int:
    """Emit ``new_subscription`` / ``price_increase`` alerts; ``dedupe_key`` is stable per merchant and amount."""
    from app.services import ai_subscriptions  # imported here: it imports this module
    from app.services.alerts import ONCE_MINUTES, emit

    emitted = 0
    rejected = await ai_subscriptions.rejected_names(session)
    own = await ai_subscriptions.owns_price_alerts(session)
    for sub in await _detect(session, settings, _local_day(settings, None), None):
        if sub["status"] not in ("new", "price_increase"):
            continue
        if sub["key"][0] in rejected or (own and sub["status"] == "price_increase"):
            continue
        name, currency, amount = sub["key"][0], sub["currency"], sub["amount"]
        if sub["status"] == "new":
            kind, dedupe = "new_subscription", f"new_subscription:{name}:{_money(amount)}"
            title = f"Nueva suscripción: {sub['merchant']}"
            body = f"{sub['merchant']} te cobró {_spanish_amount(amount, currency)} y se repite cada {sub['period_days']} días."
        else:
            previous = sub["previous_amount"]
            kind, dedupe = "price_increase", f"price_increase:{name}:{_money(previous)}:{_money(amount)}"
            title = f"Subió el precio de {sub['merchant']}"
            body = (
                f"{sub['merchant']} pasó de {_spanish_amount(previous, currency)} a "
                f"{_spanish_amount(amount, currency)} ({_money(sub['change_pct'])} %)."
            )
        alert = await emit(
            session, kind, title=title, body=body, item_id=sub["item_id"], amount=amount, currency=currency,
            dedupe_key=dedupe, min_interval_minutes=ONCE_MINUTES,
        )
        emitted += alert is not None
    return emitted


async def capture_status(session, settings, now: datetime | None = None) -> dict:
    """Capture health: last event per source, devices, phone silence and queue sizes."""
    from app.db.models_devices import Device

    now = now or now_utc()
    last_by_source = dict((await session.execute(
        select(InboxItem.source, func.max(InboxItem.created_at)).group_by(InboxItem.source)
    )).all())
    phone = [last_by_source[s] for s in PHONE_SOURCES if last_by_source.get(s)]
    last_phone = max(phone, default=None)
    # No phone event yet means nothing to compare against: not reported as silent.
    silent = last_phone is not None and now - last_phone > timedelta(hours=settings.phone_silence_hours)
    counts = dict((await session.execute(select(InboxItem.status, func.count()).group_by(InboxItem.status))).all())
    devices = (await session.scalars(select(Device).where(Device.revoked_at.is_(None)).order_by(Device.id))).all()
    events = [value for value in last_by_source.values() if value]

    def iso(value):
        return value.isoformat() if value else None

    return {
        "last_event_at": iso(max(events, default=None)),
        "phone_silence_hours": settings.phone_silence_hours,
        "silent": silent,
        "devices": [
            {"id": d.id, "name": d.name, "scopes": list(d.scopes or []), "last_used_at": iso(d.last_used_at)} for d in devices
        ],
        "queue": {key: counts.get(key, 0) for key in ("pending", "needs_review", "failed")},
        "by_source": {source: iso(last_by_source.get(source)) for source in CAPTURE_SOURCES},
    }
