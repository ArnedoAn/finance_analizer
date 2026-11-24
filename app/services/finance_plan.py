"""Plan of the month: expected and received income, budget, installments and what is left to pay down debt.

``available_for_extra = income_expected.total - budget_total - installments_total`` and may be negative. Only FIXED
incomes are expected (the conservative plan); extras count as received when they arrive. The suggestion splits the
available money plus the extras received and not yet paid into debts (``debts.suggest_allocations``).
"""
import calendar
import re
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.models import InboxItem
from app.services import debts, fx, incomes

ZERO = Decimal(0)
_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def parse_month(value: str | None, on: date) -> tuple[date, date]:
    """First and last day of ``YYYY-MM`` (the current month when None); ValueError when malformed."""
    if value is None:
        return on.replace(day=1), on.replace(day=calendar.monthrange(on.year, on.month)[1])
    found = _MONTH.fullmatch(value)
    if not found:
        raise ValueError("month must be YYYY-MM")
    year, month = int(found[1]), int(found[2])
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


async def month_items(session, zone: ZoneInfo, first: date, last: date) -> list[InboxItem]:
    """Created movements of the Bogota month, oldest first."""
    begin, finish = datetime.combine(first, time.min, zone), datetime.combine(last, time.max, zone)
    return list((await session.scalars(select(InboxItem).where(
        InboxItem.status == "created", InboxItem.occurred_at >= begin, InboxItem.occurred_at <= finish,
    ).order_by(InboxItem.occurred_at))).all())


async def month_plan(session, firefly, settings, month: str | None) -> dict:
    on = debts.today(settings)
    first, last = parse_month(month, on)
    zone = ZoneInfo(settings.timezone)
    sources = await incomes.active(session)

    fixed_cop = sum((i.amount for i in sources if i.kind == "fixed" and i.currency == "COP"), ZERO)
    fixed_usd = sum((i.amount for i in sources if i.kind == "fixed" and i.currency == "USD"), ZERO)
    rate = ZERO
    if fixed_usd > 0:
        quote = await fx.quote(session, settings, min(max(on, first), last))
        rate = quote.rate if quote else ZERO
    usd_in_cop = (fixed_usd * rate).quantize(Decimal("0.01"))

    movements = await month_items(session, zone, first, last)
    limit = await incomes.threshold(session)
    received = {"fixed": ZERO, "variable": ZERO}
    for item in (m for m in movements if incomes.is_candidate(m)):
        kind = incomes.classify(item, sources, limit)
        if kind:
            received[kind] += item.amount

    rows = await debts.active(session)
    balances = {row.id: await debts.balance_of(firefly, row) for row in rows}
    installments = sum((debts.monthly_outflow(row, balances[row.id], on) for row in rows), ZERO)
    budget_total = sum((entry["limit"] for entry in await firefly.budget_progress(first, last)), ZERO)

    expected_total = fixed_cop + usd_in_cop
    available = expected_total - budget_total - installments

    names = {row.firefly_account.casefold() for row in rows}
    paid = sum((m.amount for m in movements if m.transaction_type == "transfer" and (m.destination_account or "").casefold() in names and m.amount), ZERO)
    scheduled = sum((debts.installment_or_zero(row, balances[row.id], on) for row in rows), ZERO)
    unallocated = max(ZERO, received["variable"] - max(ZERO, paid - scheduled))
    suggestion = debts.suggest_allocations(await debts.plan_inputs(session, firefly, settings), max(ZERO, available) + unallocated)
    return {
        "month": f"{first.year:04d}-{first.month:02d}",
        "income_expected": {
            "fixed_cop": debts.peso_str(fixed_cop),
            "fixed_usd": {"usd": debts.money(fixed_usd), "rate": debts.money(rate), "cop": debts.peso_str(usd_in_cop)},
            "total": debts.peso_str(expected_total),
        },
        "income_received": {
            "fixed": debts.peso_str(received["fixed"]), "variable": debts.peso_str(received["variable"]),
            "total": debts.peso_str(received["fixed"] + received["variable"]),
        },
        "budget_total": debts.peso_str(budget_total),
        "installments_total": debts.peso_str(installments),
        "available_for_extra": debts.peso_str(available),
        "extra_received_unallocated": debts.peso_str(unallocated),
        "suggestion": suggestion,
    }
