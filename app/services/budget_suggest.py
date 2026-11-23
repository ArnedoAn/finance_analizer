"""Budget proposals per category from the last three closed months (``POST /budgets/suggest``).

Deterministic, no LLM: for each category the monthly totals of the three months before the current one are computed after
excluding one-off expenses, and the proposal is the median (adjusted upwards by the last month when the trend is rising),
rounded up to 1.000 COP. A one-off expense is one that is at least ``ONE_OFF_FACTOR`` times the median expense of its
category (with at least ``ONE_OFF_MIN_EXPENSES`` expenses in the three months) AND whose merchant appears only once in the
three months. Categories with spending in fewer than two of the three months are skipped. Nothing is created in Firefly:
the app applies the proposals. Existing Firefly budgets with the same name as the category are reported as ``current_limit``.
"""
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.services import reports, snapshots
from app.services.alerts import money

logger = get_logger(__name__)

MONTHS = 3
ONE_OFF_FACTOR = 3
ONE_OFF_MIN_EXPENSES = 6
TREND_BAND = Decimal("0.10")
ROUND_TO = Decimal(1000)
MAX_ITEMS = 25
CENT = Decimal("0.01")
ZERO = Decimal(0)
NO_CATEGORY = "Sin categoría"


def _money(value: Decimal) -> str:
    return f"{value.quantize(CENT, ROUND_HALF_UP):.2f}"


def _round_up(value: Decimal) -> Decimal:
    return (value / ROUND_TO).to_integral_value(ROUND_CEILING) * ROUND_TO


def _split_one_offs(expenses: list[tuple[int, str | None, Decimal]]) -> tuple[list[tuple[int, str | None, Decimal]], list[Decimal]]:
    """``(kept, one_offs)`` for the expenses of one category: ``(month index, merchant key, amount)``."""
    if len(expenses) < ONE_OFF_MIN_EXPENSES:
        return expenses, []
    median = statistics.median(amount for _, _, amount in expenses)
    seen = Counter(merchant for _, merchant, _ in expenses if merchant)
    kept, one_offs = [], []
    for expense in expenses:
        _, merchant, amount = expense
        if amount >= median * ONE_OFF_FACTOR and (merchant is None or seen[merchant] == 1):
            one_offs.append(amount)
        else:
            kept.append(expense)
    return kept, one_offs


def _reason(name: str, median: Decimal, last: Decimal, trend: str, suggested: Decimal, excluded: list[Decimal]) -> str:
    text = f"La mediana de tus últimos {MONTHS} meses en {name} es {money(median)}"
    text += {
        "up": f" y el último mes subiste a {money(last)}: se propone un punto intermedio, {money(suggested)}.",
        "down": f" y el último mes bajaste a {money(last)}: se propone la mediana, {money(suggested)}.",
        "flat": f" y el último mes ({money(last)}) se mantuvo estable: se propone {money(suggested)}.",
    }[trend]
    if excluded:
        text += f" No cuenta {len(excluded)} gasto{'s' if len(excluded) > 1 else ''} único{'s' if len(excluded) > 1 else ''} por {money(sum(excluded, ZERO))}."
    return text


async def suggest(session, firefly, settings, now: datetime | None = None) -> dict:
    """``{"currency", "months", "items", "total_suggested"}``; ``items`` sorted by the proposed limit, largest first."""
    today = (now or now_utc()).astimezone(ZoneInfo(settings.timezone)).date()
    current = today.replace(day=1)
    firsts = [reports._add_months(current, -n) for n in range(MONTHS, 0, -1)]
    per_category: dict[str, list[tuple[int, str | None, Decimal]]] = defaultdict(list)
    for index, first in enumerate(firsts):
        last = reports._add_months(first, 1) - timedelta(days=1)
        for row in await reports._ledger(session, settings, first, last, None, reports.SPENDING, cop_only=True):
            if row.category and row.category != NO_CATEGORY:
                key = " ".join(row.merchant.casefold().split()) if row.merchant else None
                per_category[row.category].append((index, key, row.amount))
    limits: dict[str, Decimal] = {}
    try:
        for budget in await firefly.budget_progress(current, reports._add_months(current, 1) - timedelta(days=1)):
            limits[budget["name"].casefold()] = budget["limit"]
    except Exception as exc:  # existing limits are optional context
        logger.info("budget_suggest_limits_unavailable", error_type=type(exc).__name__)
    items = []
    for name, expenses in per_category.items():
        kept, excluded = _split_one_offs(expenses)
        totals = [sum((amount for index, _, amount in kept if index == month), ZERO) for month in range(MONTHS)]
        if sum(1 for total in totals if total > 0) < 2:
            continue
        median = statistics.median(totals)
        last = totals[-1]
        ratio = last / median if median else ZERO
        trend = "up" if ratio >= 1 + TREND_BAND else "down" if ratio <= 1 - TREND_BAND else "flat"
        suggested = _round_up((median + last) / 2 if trend == "up" else median)
        current_limit = limits.get(name.casefold())
        items.append({
            "category": name, "suggested_limit": _money(suggested), "median": _money(median), "last_month": _money(last), "trend": trend,
            "trend_pct": _money((last - totals[0]) / totals[0] * 100) if totals[0] else None,
            "monthly": [{"month": snapshots.month_of(first), "total": _money(total)} for first, total in zip(firsts, totals, strict=True)],
            "excluded": {"count": len(excluded), "total": _money(sum(excluded, ZERO))},
            "current_limit": _money(current_limit) if current_limit else None,
            "reason": _reason(name, median, last, trend, suggested, excluded),
        })
    items.sort(key=lambda item: (-Decimal(item["suggested_limit"]), item["category"]))
    items = items[:MAX_ITEMS]
    return {
        "currency": "COP", "months": [snapshots.month_of(first) for first in firsts], "items": items,
        "total_suggested": _money(sum((Decimal(item["suggested_limit"]) for item in items), ZERO)),
    }
