"""Incomes: the owner's income sources, how a deposit is classified, the extra-income threshold and its alert.

``fixed`` incomes count in the month's plan (expected, and received when a deposit matches their ``match`` patterns).
Everything else that arrives is conservative «extra»: a deposit that matches a ``variable`` income, or one that matches
nothing and is strictly greater than the threshold (``runtime_flags`` key ``extra_income_threshold``, 200.000 COP by
default), is a variable extra and raises the ``extra_income`` alert with the plan's suggested split. Smaller unmatched
deposits (refunds, small transfers) count in neither. Functions never commit unless they say so.
"""
from decimal import Decimal

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models_debts import Income
from app.db.models_flags import RuntimeFlag
from app.services import alerts as alerting
from app.services import audit, debts

logger = get_logger(__name__)

THRESHOLD_KEY = "extra_income_threshold"
DEFAULT_THRESHOLD = Decimal("200000.00")
MAX_THRESHOLD = Decimal("1000000000")
ALERT_TYPE = "extra_income"


class IncomeError(Exception):
    def __init__(self, status: int, detail: str, code: str) -> None:
        super().__init__(detail)
        self.status, self.detail, self.code = status, detail, code


async def threshold(session) -> Decimal:
    stored = await session.scalar(select(RuntimeFlag.value).where(RuntimeFlag.key == THRESHOLD_KEY))
    return Decimal(stored["amount_cop"]).quantize(Decimal("0.01")) if stored else DEFAULT_THRESHOLD


async def set_threshold(session, amount: Decimal) -> Decimal:
    """Store the threshold (the caller validates the range and commits)."""
    amount = Decimal(amount).quantize(Decimal("0.01"))
    row = await session.get(RuntimeFlag, THRESHOLD_KEY, populate_existing=True)
    if row is None:
        session.add(RuntimeFlag(key=THRESHOLD_KEY, value={"amount_cop": str(amount)}))
    else:
        row.value = {"amount_cop": str(amount)}
    await session.flush()
    return amount


async def active(session) -> list[Income]:
    return list((await session.scalars(select(Income).where(Income.active.is_(True)).order_by(Income.id))).all())


async def get(session, income_id: int) -> Income:
    row = await session.get(Income, income_id, populate_existing=True)
    if row is None or not row.active:
        raise IncomeError(404, "Ingreso no encontrado.", "income_not_found")
    return row


def view(row: Income) -> dict:
    return {
        "id": row.id, "name": row.name, "kind": row.kind, "currency": row.currency, "amount": debts.money(row.amount),
        "day_of_month": row.day_of_month, "match": list(row.match or []), "active": row.active,
    }


async def create(session, data: dict, principal=None) -> dict:
    row = Income(
        name=data["name"], kind=data["kind"], currency=data["currency"], amount=data["amount"],
        day_of_month=data.get("day_of_month"), match=list(data.get("match") or []), active=True,
    )
    session.add(row)
    await session.flush()
    await audit.record(session, action="income.create", target_type="income", target_id=row.id, principal=principal,
                       detail={"name": row.name, "kind": row.kind, "currency": row.currency})
    await session.commit()
    return view(row)


async def update(session, income_id: int, data: dict, principal=None) -> dict:
    row = await get(session, income_id)
    for key, value in data.items():
        setattr(row, key, list(value or []) if key == "match" else value)
    await session.flush()
    await audit.record(session, action="income.update", target_type="income", target_id=row.id, principal=principal, detail={"changed": sorted(data)})
    await session.commit()
    return view(row)


async def deactivate(session, income_id: int, principal=None) -> None:
    row = await get(session, income_id)
    row.active = False
    await audit.record(session, action="income.delete", target_type="income", target_id=row.id, principal=principal, detail={"name": row.name})
    await session.commit()


def classify(item, incomes, limit: Decimal) -> str | None:
    """``fixed`` or ``variable`` for a registered deposit, None when it counts as neither.

    A fixed income's patterns win over a variable one's; no match is variable only above ``limit`` (strictly).
    """
    texts = (item.merchant, item.description)
    matched = [income for income in incomes if debts.pattern_matches(income.match, *texts)]
    if any(income.kind == "fixed" for income in matched):
        return "fixed"
    if matched:
        return "variable"
    return "variable" if item.amount is not None and item.amount > limit else None


def is_candidate(item) -> bool:
    """A created COP deposit with an amount: the only movement that is income."""
    return item.status == "created" and item.transaction_type == "deposit" and item.currency == "COP" and item.amount is not None


async def on_item_created(session, firefly, settings, item) -> None:
    """Raise ``extra_income`` for a deposit that classifies as a variable extra. Never raises: the alert is a courtesy."""
    if not is_candidate(item):
        return
    try:
        if classify(item, await active(session), await threshold(session)) != "variable":
            return
        body = f"Te llegó un ingreso extra de {alerting.money(item.amount, item.currency)}."
        try:
            suggestion = debts.suggest_allocations(await debts.plan_inputs(session, firefly, settings), item.amount)
        except Exception as exc:  # the suggestion needs Firefly balances; without them the alert still goes out
            logger.warning("extra_income_suggestion_failed", error_type=type(exc).__name__)
            suggestion = {"allocations": []}
        names = {row.id: row.name for row in await debts.active(session)}
        parts = [f"{alerting.money(a['amount'])} a «{names.get(a['debt_id'], 'deuda')}»" for a in suggestion["allocations"]]
        if parts:
            body += " Sugerencia del plan para abonar a capital: " + "; ".join(parts) + "."
        async with session.begin_nested():
            await alerting.emit(
                session, ALERT_TYPE, title="Ingreso extra recibido", body=body, item_id=item.id, amount=item.amount, currency=item.currency,
                dedupe_key=f"extra_income:{item.id}", min_interval_minutes=alerting.ONCE_MINUTES,
            )
        await session.commit()
    except Exception as exc:
        logger.error("extra_income_alert_failed", inbox_id=item.id, error_type=type(exc).__name__)
