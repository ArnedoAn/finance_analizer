"""Where each monthly budget stands (``GET /budgets/status``): limit, spent, remaining, percent and the month-end projection.

Figures come from Firefly (``budget_progress``: active budgets with a limit in the month and what was spent on them), so
what the owner assigns by hand and what the server assigns count the same. The projection is linear: what was spent so far
divided by the days elapsed, times the days of the month (a past month projects what was spent; a future one projects zero).
``state``: ``over`` (already above the limit), ``at_risk`` (the projection passes the limit) or ``ok``.
"""
from decimal import ROUND_HALF_UP, Decimal

from app.core.timezone import now_utc
from app.services.budget_assign import month_bounds

CENT = Decimal("0.01")
ZERO = Decimal(0)


def _money(value: Decimal) -> str:
    return f"{value.quantize(CENT, ROUND_HALF_UP):.2f}"


def _percent(part: Decimal, whole: Decimal) -> float:
    return float((part * 100 / whole).quantize(Decimal("0.1"), ROUND_HALF_UP)) if whole > 0 else 0.0


def _state(limit: Decimal, spent: Decimal, projected: Decimal) -> str:
    return "over" if spent > limit else "at_risk" if projected > limit else "ok"


async def status(firefly, month: str, zone) -> dict:
    start, end = month_bounds(month)
    today = now_utc().astimezone(zone).date()
    days = end.day
    elapsed = 0 if today < start else days if today > end else today.day
    budgets, total_limit, total_spent, total_projected = [], ZERO, ZERO, ZERO
    for row in await firefly.budget_progress(start, end):
        limit, spent = Decimal(row["limit"]), Decimal(row["spent"])
        projected = spent * days / elapsed if elapsed else ZERO
        budgets.append({
            "name": row["name"], "limit": _money(limit), "spent": _money(spent), "remaining": _money(limit - spent),
            "percent": _percent(spent, limit), "projected": _money(projected), "projected_percent": _percent(projected, limit),
            "state": _state(limit, spent, projected),
        })
        total_limit, total_spent, total_projected = total_limit + limit, total_spent + spent, total_projected + projected
    budgets.sort(key=lambda row: (-row["percent"], row["name"].casefold()))
    return {
        "month": month, "days_in_month": days, "days_elapsed": elapsed, "budgets": budgets,
        "totals": {
            "limit": _money(total_limit), "spent": _money(total_spent), "remaining": _money(total_limit - total_spent),
            "percent": _percent(total_spent, total_limit), "projected": _money(total_projected),
            "projected_percent": _percent(total_projected, total_limit), "state": _state(total_limit, total_spent, total_projected),
        },
    }
