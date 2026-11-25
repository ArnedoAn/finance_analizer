"""Budgets and their monthly limits, written to Firefly one at a time (assistant proposals).

The spreadsheet import (``budget_import``) shares ``write_limit``. Every function audits and commits; an owner-fixable
failure is ``ValueError`` and a Firefly failure propagates.
"""
from decimal import Decimal

from sqlalchemy import update

from app.db.models import BudgetAssignment, BudgetMap
from app.services import audit
from app.services.budget_assign import month_bounds, valid_month
from app.services.budget_import import CENT, exact_limit, write_limit


async def find(firefly, name: str) -> dict | None:
    """The Firefly budget ``{id, name}`` called ``name`` (any case), or None."""
    wanted = name.strip().casefold()
    return next((row for row in await firefly.budgets() if row["name"].casefold() == wanted), None)


async def _existing(firefly, name: str) -> dict:
    budget = await find(firefly, name)
    if budget is None:
        raise ValueError(f"Unknown budget: {name}")
    return budget


async def current_limit(firefly, budget: dict, month: str) -> dict | None:
    """The limit of ``budget`` for exactly ``month`` as ``{id, amount, ...}``, or None."""
    start, end = month_bounds(month)
    return exact_limit(await firefly.budget_limits(budget["id"], start, end), start, end)


async def _write(firefly, budget: dict, month: str, amount: Decimal) -> None:
    if not valid_month(month):
        raise ValueError("The month must look like YYYY-MM")
    start, end = month_bounds(month)
    await write_limit(firefly, budget["id"], start, end, amount.quantize(CENT), await current_limit(firefly, budget, month))


async def create(session, firefly, name: str, amount: Decimal | None, month: str | None, principal) -> dict:
    """Create the budget (an existing one is kept) and, with ``amount``, its limit for ``month``. Commits."""
    budget = await find(firefly, name)
    created = budget is None
    if created:
        budget = await firefly.create_budget(name)
    if amount is not None:
        await _write(firefly, budget, month, amount)
    await audit.record(
        session, action="budget.create", target_type="budget", target_id=budget["name"], principal=principal,
        detail={"name": budget["name"], "created": created, "month": month, "amount": f"{amount:.2f}" if amount is not None else None},
    )
    await session.commit()
    return budget


async def set_limit(session, firefly, name: str, amount: Decimal, month: str, principal) -> dict:
    """Set the limit of an existing budget for ``month`` (created or updated). Commits."""
    budget = await _existing(firefly, name)
    await _write(firefly, budget, month, amount)
    await audit.record(
        session, action="budget.limit", target_type="budget", target_id=budget["name"], principal=principal,
        detail={"name": budget["name"], "month": month, "amount": f"{amount:.2f}"},
    )
    await session.commit()
    return budget


async def rename(session, firefly, name: str, new_name: str, principal) -> str:
    """Rename an existing budget in Firefly and in the analyzer's rows that name it (learned mappings and suggestions);
    returns the new spelling. A different budget with that name is a conflict. Commits."""
    budget = await _existing(firefly, name)
    clash = await find(firefly, new_name)
    if clash is not None and clash["id"] != budget["id"]:
        raise ValueError(f"A budget named «{clash['name']}» already exists")
    stored = (await firefly.rename_budget(budget["id"], new_name))["name"]
    await session.execute(update(BudgetMap).where(BudgetMap.budget == budget["name"]).values(budget=stored))
    await session.execute(update(BudgetAssignment).where(BudgetAssignment.budget == budget["name"]).values(budget=stored))
    await audit.record(session, action="budget.rename", target_type="budget", target_id=stored, detail={"from": budget["name"], "to": stored}, principal=principal)
    await session.commit()
    return stored


async def delete(session, firefly, name: str, principal) -> dict:
    """Delete an existing budget with its limits. Commits."""
    budget = await _existing(firefly, name)
    await firefly.delete_budget(budget["id"])
    await audit.record(session, action="budget.delete", target_type="budget", target_id=budget["name"], detail={"name": budget["name"]}, principal=principal)
    await session.commit()
    return budget
