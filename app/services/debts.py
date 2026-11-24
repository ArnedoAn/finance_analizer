"""Debts: CRUD over Firefly liabilities, schedules, payoff plans, payment recognition and the month-close summary.

Firefly owns the balance (a liability account, or a ``ccAsset`` card the owner linked); Cofre keeps the rest (term,
installment, payment day, limit, EA rate, patterns). Installments are recorded as TRANSFERS from the paying account to the
liability, so the balance drops by itself (``Processor._extract`` does it through ``payment_debt``, the only place).
The maths is ``app.services.debt_math``; nothing here does arithmetic of its own beyond reading balances and mapping rows.

Functions never commit unless they say so; the creation, update and removal commit (they also touch ``own_accounts``).
"""
import re
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.exceptions import PermanentError
from app.db.models import InboxItem
from app.db.models_debts import Debt
from app.db.repositories import OwnAccountRepository
from app.parsers.common import (
    canonical_mask,
    fold,
    is_masked_counterparty,
    match_masked_counterparty,
)
from app.services import audit, own_accounts
from app.services import debt_math as math

LIABILITY_TYPES = frozenset({"liability", "liabilities", "loan", "debt", "mortgage"})
ASSET_OR_LIABILITY = frozenset({"asset", *LIABILITY_TYPES})
KINDS = ("loan", "credit_card")
OWN_KIND = {"loan": "loan", "credit_card": "credit"}
LIABILITY_KIND = {"loan": "loan", "credit_card": "debt"}
ZERO = Decimal(0)


class DebtError(Exception):
    """A domain failure with the HTTP status and code the router answers."""

    def __init__(self, status: int, detail: str, code: str) -> None:
        super().__init__(detail)
        self.status, self.detail, self.code = status, detail, code


def today(settings):
    """The current Bogota date; tests replace this function to pin the calendar."""
    return datetime.now(ZoneInfo(settings.timezone)).date()


def money(value) -> str:
    return f"{Decimal(value):.2f}"


def peso_str(value) -> str:
    """COP for projections (schedule, plan, month plan, close report): whole pesos, half up, serialized ``"1234.00"``.

    The only place those responses round, and only when serializing: the maths in between keeps what it computed.
    """
    return f"{math.peso(value):.2f}"


def _optional(value) -> str | None:
    return None if value is None else money(value)


def pattern_matches(patterns, *texts) -> bool:
    """True when any pattern appears in any text, ignoring case and accents; ``*`` in a pattern matches any run of characters.

    A masked counterparty the bank wrote with asterisks («PAU*** BET***») matches a pattern that agrees with it token by token
    by prefix, like ``Processor._unmask`` does with the known names.
    """
    if any(match_masked_counterparty(canonical_mask(text), patterns or ()) for text in texts if text and is_masked_counterparty(text)):
        return True
    folded = [fold(text) for text in texts if text]
    for pattern in patterns or ():
        wanted = fold(pattern or "").strip()
        if not wanted:
            continue
        expression = re.compile(".*".join(re.escape(part) for part in wanted.split("*")))
        if any(expression.search(text) for text in folded):
            return True
    return False


# ---- reading --------------------------------------------------------------------------------------------------------

async def active(session) -> list[Debt]:
    return list((await session.scalars(select(Debt).where(Debt.active.is_(True)).order_by(Debt.id))).all())


async def get(session, debt_id: int) -> Debt:
    row = await session.get(Debt, debt_id, populate_existing=True)
    if row is None or not row.active:
        raise DebtError(404, "Deuda no encontrada.", "debt_not_found")
    return row


def owed(info: dict | None) -> Decimal:
    """What is owed, positive, from Firefly's signed ``current_balance``.

    A liability the owner owes is negative in Firefly (the absolute value is the debt, whichever sign it stores); a card
    linked as an asset is owed when its balance is negative (a positive balance is credit in favour, owed is zero).
    """
    if info is None:
        return ZERO
    balance = info["balance"]
    if str(info["type"]).casefold() in LIABILITY_TYPES:
        return abs(balance)
    return max(ZERO, -balance)


async def balance_of(firefly, row: Debt) -> Decimal:
    return owed(await firefly.account_balance(row.firefly_account_id))


def installment_for(row: Debt, balance: Decimal, on) -> Decimal:
    """The monthly capital+interest payment of a debt.

    A card pays the installment the owner states or else its minimum payment. A loan uses its installment; without one it is
    derived from the balance and the months the calendar says are left (``term_months`` and ``start_date``), and last from the
    original amount and the term. 422 ``debt_without_payment`` when none is possible.
    """
    if row.kind == "credit_card":
        fixed = math.card_payment(row.installment, row.minimum_payment)
        if fixed is None:
            raise DebtError(422, f"Indica la cuota o el pago mínimo de «{row.name}».", "debt_without_payment")
        return fixed
    if row.installment is not None and row.installment > 0:
        return Decimal(row.installment)
    remaining = math.calendar_remaining(row.term_months, row.start_date, on)
    if remaining:
        return math.french_installment(balance, row.rate_ea, remaining)
    if row.term_months and row.original_amount:
        return math.french_installment(row.original_amount, row.rate_ea, row.term_months)
    raise DebtError(422, f"Indica la cuota de «{row.name}» o su plazo y fecha de inicio.", "debt_without_payment")


def installment_or_zero(row: Debt, balance: Decimal, on) -> Decimal:
    try:
        return installment_for(row, balance, on)
    except DebtError:
        return ZERO


def monthly_outflow(row: Debt, balance: Decimal, on) -> Decimal:
    """Installment plus insurance, zero when the debt has no usable payment (it only feeds totals)."""
    paying = installment_or_zero(row, balance, on)
    return paying + Decimal(row.insurance_monthly or 0) if paying else ZERO


def view(row: Debt, balance: Decimal) -> dict:
    card = row.kind == "credit_card"
    return {
        "id": row.id, "name": row.name, "kind": row.kind, "lender": row.lender, "firefly_account_id": row.firefly_account_id,
        "currency": row.currency, "balance": money(balance), "original_amount": _optional(row.original_amount),
        "rate_ea": f"{row.rate_ea:.2f}", "term_months": row.term_months,
        "start_date": row.start_date.isoformat() if row.start_date else None, "installment": _optional(row.installment),
        "payment_day": row.payment_day, "insurance_monthly": money(row.insurance_monthly or 0),
        "credit_limit": _optional(row.credit_limit) if card else None,
        "minimum_payment": _optional(row.minimum_payment) if card else None,
        "payment_match": list(row.payment_match or []), "active": row.active,
    }


async def listing(session, firefly, settings) -> dict:
    on = today(settings)
    items, total, monthly = [], ZERO, ZERO
    for row in await active(session):
        balance = await balance_of(firefly, row)
        items.append(view(row, balance))
        total += balance
        monthly += monthly_outflow(row, balance, on)
    return {"items": items, "total": len(items), "totals": {"balance": money(total), "installments_monthly": money(monthly)}}


# ---- writing --------------------------------------------------------------------------------------------------------

async def _account(firefly, account_id: str) -> dict:
    found = next((a for a in await firefly.accounts() if str(a["id"]) == str(account_id)), None)
    if found is None or found["type"] not in ASSET_OR_LIABILITY:
        raise DebtError(422, "Firefly no tiene una cuenta de activo o pasivo con ese id.", "firefly_account_missing")
    return found


async def _register_own_account(session, row: Debt) -> bool:
    repository = OwnAccountRepository(session)
    if await repository.taken(row.firefly_account):
        return False
    await repository.add(
        firefly_account=row.firefly_account, bank=(row.lender or row.name)[:60], kind=OWN_KIND[row.kind],
        last4=[], aliases=[], sources=[], payment_day=row.payment_day if row.kind == "credit_card" else None,
    )
    return True


async def _taken(session, name: str, exclude_id: int | None = None) -> bool:
    wanted = name.casefold()
    return any(row.id != exclude_id and row.name.casefold() == wanted for row in await active(session))


async def create(session, firefly, settings, data: dict, principal=None) -> dict:
    """Create a debt. Without ``firefly_account_id`` a liability is created in Firefly with the current balance as its
    opening balance (``data["balance"]``, required then); with one, the existing account (liability or card asset) is linked.
    The Firefly account is also registered as an own account so the payments are recognized. Commits."""
    if await _taken(session, data["name"]):
        raise DebtError(409, "Ya existe una deuda con ese nombre.", "debt_exists")
    on = today(settings)
    if data.get("firefly_account_id"):
        account = await _account(firefly, data["firefly_account_id"])
    else:
        balance = data.get("balance")
        if balance is None or balance <= 0:
            raise DebtError(422, "Indica el saldo actual (balance) para crear el pasivo en Firefly.", "balance_required")
        if any(a["name"].casefold() == data["name"].casefold() and a["type"] in ASSET_OR_LIABILITY for a in await firefly.accounts()):
            raise DebtError(409, "Ya existe una cuenta con ese nombre en Firefly; vincúlala con firefly_account_id.", "firefly_account_exists")
        try:
            account = await firefly.create_liability(data["name"], LIABILITY_KIND[data["kind"]], data["rate_ea"], balance, on)
        except PermanentError as exc:
            raise DebtError(422, "Firefly rechazó crear el pasivo: " + str(exc)[:200], "firefly_rejected") from exc
    row = Debt(
        kind=data["kind"], name=data["name"], lender=data.get("lender") or "", firefly_account_id=str(account["id"]),
        firefly_account=account["name"], currency="COP", original_amount=data.get("original_amount"), rate_ea=data["rate_ea"],
        term_months=data.get("term_months"), start_date=data.get("start_date"), installment=data.get("installment"),
        payment_day=data.get("payment_day"), insurance_monthly=data.get("insurance_monthly") or ZERO,
        credit_limit=data.get("credit_limit"), minimum_payment=data.get("minimum_payment"),
        payment_match=list(data.get("payment_match") or []), active=True,
    )
    session.add(row)
    await session.flush()
    own = await _register_own_account(session, row)
    await audit.record(session, action="debt.create", target_type="debt", target_id=row.id, principal=principal,
                       detail={"name": row.name, "kind": row.kind, "linked": bool(data.get("firefly_account_id")), "own_account": own})
    await session.commit()
    own_accounts.invalidate()
    return view(row, await balance_of(firefly, row))


async def update(session, firefly, settings, debt_id: int, data: dict, principal=None) -> dict:
    """Update the fields sent (``balance`` is ignored: Firefly owns it). A new EA rate is pushed to the liability. Commits."""
    row = await get(session, debt_id)
    data = {key: value for key, value in data.items() if key != "balance"}
    if "name" in data and await _taken(session, data["name"], row.id):
        raise DebtError(409, "Ya existe una deuda con ese nombre.", "debt_exists")
    if data.get("firefly_account_id") and str(data["firefly_account_id"]) != row.firefly_account_id:
        account = await _account(firefly, data.pop("firefly_account_id"))
        row.firefly_account_id, row.firefly_account = str(account["id"]), account["name"]
    data.pop("firefly_account_id", None)
    if "kind" in data and data["kind"] != row.kind:
        raise DebtError(422, "El tipo de una deuda no se puede cambiar.", "debt_kind_locked")
    rate_changed = "rate_ea" in data and Decimal(data["rate_ea"]) != Decimal(row.rate_ea)
    for key, value in data.items():
        if key == "payment_match":
            value = list(value or [])
        setattr(row, key, value)
    if rate_changed:
        info = await firefly.account_balance(row.firefly_account_id)
        if info is not None and str(info["type"]).casefold() in LIABILITY_TYPES:
            await firefly.update_account(row.firefly_account_id, {"interest": f"{Decimal(row.rate_ea):.2f}", "interest_period": "yearly"})
    await session.flush()
    await audit.record(session, action="debt.update", target_type="debt", target_id=row.id, principal=principal,
                       detail={"changed": sorted(data), "rate_pushed": rate_changed})
    await session.commit()
    return view(row, await balance_of(firefly, row))


async def deactivate(session, debt_id: int, principal=None) -> None:
    """Soft delete: the debt stops counting; the liability stays in Firefly. Commits."""
    row = await get(session, debt_id)
    row.active = False
    await audit.record(session, action="debt.delete", target_type="debt", target_id=row.id, principal=principal, detail={"name": row.name})
    await session.commit()


# ---- payment recognition (the only place) -----------------------------------------------------------------------------

async def payment_debt(session, *texts, exclude_account: str | None = None) -> Debt | None:
    """The active debt whose ``payment_match`` patterns match the merchant or counterparty of an outgoing movement.

    ``exclude_account`` is the movement's own source account: a purchase made with the card itself is never a payment of it.
    """
    for row in await active(session):
        if row.payment_match and row.firefly_account.casefold() != (exclude_account or "").casefold() and pattern_matches(row.payment_match, *texts):
            return row
    return None


async def loan_by_account(session, account_name: str) -> Debt | None:
    """The active LOAN (not a card) whose Firefly liability is ``account_name``: its payments are split into capital and interest."""
    wanted = (account_name or "").casefold()
    return next((row for row in await active(session) if row.kind == "loan" and row.firefly_account.casefold() == wanted), None)


# ---- schedules and plans ----------------------------------------------------------------------------------------------

async def _balance_and_installment(firefly, row: Debt, on) -> tuple[Decimal, Decimal]:
    balance = await balance_of(firefly, row)
    return balance, installment_for(row, balance, on)


def _not_amortizing(row: Debt) -> DebtError:
    return DebtError(422, f"La cuota de «{row.name}» no cubre los intereses: el saldo nunca baja.", "debt_not_amortizing")


def _summary(baseline: math.Schedule, adjusted: math.Schedule) -> dict:
    return {
        "remaining_installments": adjusted.months,
        "payoff_date": adjusted.payoff_date.isoformat() if adjusted.payoff_date else None,
        "total_interest": peso_str(adjusted.total_interest),
        "baseline": {"payoff_date": baseline.payoff_date.isoformat() if baseline.payoff_date else None, "total_interest": peso_str(baseline.total_interest)},
        "interest_saved": peso_str(baseline.total_interest - adjusted.total_interest),
        "months_saved": baseline.months - adjusted.months,
        "new_installment": peso_str(adjusted.new_installment),
    }


async def schedule(session, firefly, settings, debt_id: int, *, extra_monthly=ZERO, extra_once=ZERO, extra_once_month=1, mode="term") -> dict:
    """Amortization of one debt from its current Firefly balance, with the extra payments, against the same debt without them."""
    row = await get(session, debt_id)
    on = today(settings)
    balance, installment = await _balance_and_installment(firefly, row, on)
    first = math.first_payment_date(on, row.payment_day or (row.start_date.day if row.start_date else None))
    common = {"insurance": row.insurance_monthly or ZERO}
    try:
        baseline = math.amortize(balance, row.rate_ea, installment, first, **common)
        adjusted = math.amortize(
            balance, row.rate_ea, installment, first, extra_monthly=extra_monthly, extra_once=extra_once,
            extra_once_month=extra_once_month, mode=mode, **common,
        )
    except math.NotAmortizingError as exc:
        raise _not_amortizing(row) from exc
    rows = [
        {"n": r.n, "date": r.date.isoformat(), "installment": peso_str(r.installment), "interest": peso_str(r.interest), "principal": peso_str(r.principal),
         "insurance": peso_str(r.insurance), "extra": peso_str(r.extra), "balance": peso_str(r.balance)}
        for r in adjusted.rows
    ]
    return {"rows": rows, "summary": _summary(baseline, adjusted)}


async def plan_inputs(session, firefly, settings) -> list[tuple[Debt, math.DebtInput]]:
    """Active debts that still owe something, with the numbers the plan needs."""
    on, result = today(settings), []
    for row in await active(session):
        balance, installment = await _balance_and_installment(firefly, row, on)
        if balance > 0:
            result.append((row, math.DebtInput(row.id, balance, Decimal(row.rate_ea), installment, Decimal(row.insurance_monthly or 0))))
    return result


def _date_of(row: Debt, months: int | None, on) -> str | None:
    if months is None:
        return None
    first = math.first_payment_date(on, row.payment_day or (row.start_date.day if row.start_date else None))
    return math.add_months(first, max(months, 1) - 1).isoformat()


def strategy_result(items: list[tuple[Debt, math.DebtInput]], extra_monthly, extra_once, strategy: str, on) -> dict:
    inputs = [debt_input for _, debt_input in items]
    rows = {row.id: row for row, _ in items}
    result = math.simulate_plan(inputs, extra_monthly, extra_once, strategy)
    baseline = math.simulate_plan(inputs, 0, 0, strategy, redistribute=False)
    dates = {o.debt_id: _date_of(rows[o.debt_id], o.months, on) for o in result.outcomes}
    free = None if result.debt_free_month is None else max((d for d in dates.values() if d), default=None)
    return {
        "order": math.payoff_order(result),
        "debts": [{"debt_id": o.debt_id, "payoff_date": dates[o.debt_id], "total_interest": peso_str(o.total_interest)} for o in result.outcomes],
        "debt_free_date": free,
        "total_interest": peso_str(result.total_interest),
        "interest_saved_vs_minimum": peso_str(baseline.total_interest - result.total_interest),
    }


async def plan(session, firefly, settings, *, extra_monthly=ZERO, extra_once=ZERO, strategy="both") -> dict:
    """Avalanche and/or snowball over every active debt: ``{strategy: result}`` (see ``strategy_result``)."""
    items, on = await plan_inputs(session, firefly, settings), today(settings)
    names = math.STRATEGIES if strategy == "both" else (strategy,)
    return {name: strategy_result(items, extra_monthly, extra_once, name, on) for name in names}


def suggest_allocations(items: list[tuple[Debt, math.DebtInput]], amount: Decimal) -> dict:
    """Where to put ``amount`` extra: the strategy that ends up paying less interest, aimed at its first debts.

    ``{strategy, allocations: [{debt_id, amount, reason}]}``; the amount goes to the first debt in the strategy's priority
    until it is paid off, then to the next. No debts or nothing to allocate give an empty list (strategy avalanche).
    """
    amount = max(Decimal(amount), ZERO)
    if not items or amount <= 0:
        return {"strategy": "avalanche", "allocations": []}
    inputs = [debt_input for _, debt_input in items]
    names = {row.id: row.name for row, _ in items}
    best = min(math.STRATEGIES, key=lambda s: (math.simulate_plan(inputs, amount, 0, s).total_interest, s != "avalanche"))
    allocations, left = [], amount
    for debt in math.priority(inputs, best):
        if left <= 0:
            break
        part = min(left, debt.balance)
        reason = (f"Tasa más alta ({debt.rate_ea:.2f} % EA): es la que más intereses cobra." if best == "avalanche"
                  else f"Saldo más bajo: «{names[debt.id]}» se liquida primero y libera su cuota.")
        allocations.append({"debt_id": debt.id, "amount": peso_str(part), "reason": reason})
        left -= part
    return {"strategy": best, "allocations": allocations}


# ---- month close ------------------------------------------------------------------------------------------------------

async def month_summary(session, firefly, settings, first, last) -> dict:
    """Debt section of the month-close report: total balance, installments paid in the month and estimated interest.

    ``installments_paid`` sums the created transfers whose destination is one of the active debts' accounts between the two
    Bogota dates (inclusive). ``estimated_interest`` is the EA interest on each balance for one month (what the schedule
    charges), an estimate: Firefly does not post interest.
    """
    zone = ZoneInfo(settings.timezone)
    begin = datetime.combine(first, time.min, zone)
    finish = datetime.combine(last, time.max, zone)
    rows = await active(session)
    names = {row.firefly_account.casefold(): row for row in rows}
    paid = (await session.scalars(select(InboxItem).where(
        InboxItem.status == "created", InboxItem.transaction_type == "transfer", InboxItem.occurred_at >= begin, InboxItem.occurred_at <= finish,
    ))).all()
    paid = [item for item in paid if (item.destination_account or "").casefold() in names]
    total, interest, items = ZERO, ZERO, []
    for row in rows:
        balance = await balance_of(firefly, row)
        estimate = math.peso(balance * math.monthly_rate(row.rate_ea))
        total += balance
        interest += estimate
        items.append({"debt_id": row.id, "name": row.name, "balance": peso_str(balance), "installments_paid": sum(1 for p in paid if p.destination_account.casefold() == row.firefly_account.casefold())})
    return {
        "total_balance": peso_str(total), "installments_paid": len(paid),
        "installments_paid_amount": peso_str(sum((p.amount or ZERO for p in paid), ZERO)),
        "estimated_interest": peso_str(interest), "debts": items,
    }
