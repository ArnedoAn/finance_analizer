"""Debt maths: pure functions, no I/O, no clock, no database (the callers pass dates and numbers).

Conventions
-----------
* Money is ``Decimal`` in whole pesos: every interest, installment and payment is rounded half up to the peso
  (``peso``), the way a Colombian bank statement shows it. Rates are percentages: ``28.5`` means 28.5 % EA.
* Colombian rates are *Efectiva Anual* (EA). The monthly rate is ``(1 + EA)^(1/12) - 1`` (``monthly_rate``).
* The installment (cuota) is the French one: constant principal+interest payment. The monthly insurance is a fixed
  charge on top of it; it never earns interest and never reduces the balance.
* ``amortize`` builds one debt's schedule. An extra payment (monthly and/or once) goes to principal after that month's
  installment, in one of two modes: ``term`` keeps the installment and shortens the debt, ``installment`` keeps the
  original end date and lowers the installment (recomputed with the French formula for the months left).
* ``simulate_plan`` pays several debts at once. Every debt keeps paying its own installment; the extra money (and
  the installment+insurance of every debt already paid off: the snowball effect) goes to the target debt, chosen by
  ``avalanche`` (highest rate first) or ``snowball`` (smallest balance first).
* A card pays a fixed amount (the minimum or the installment the owner states) with interest on the balance each month:
  it is the same schedule as a loan (``card_payment`` picks the amount).
"""
import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP, Context, Decimal, localcontext
from typing import NamedTuple

PESO = Decimal(1)
ZERO = Decimal(0)
MAX_MONTHS = 480  # 40 years: a debt that does not end by then is reported as never ending
_PRECISION = 40
_ROUNDING_SLACK = Decimal("0.05")  # months: the peso rounding of an installment never moves the term by more than this

MODES = ("term", "installment")
STRATEGIES = ("avalanche", "snowball")


class NotAmortizingError(ValueError):
    """The payment does not even cover the interest: the balance never goes down."""


def peso(value) -> Decimal:
    """Round half up to a whole peso."""
    return Decimal(value).quantize(PESO, rounding=ROUND_HALF_UP)


def monthly_rate(rate_ea) -> Decimal:
    """Monthly rate from an EA percentage: ``(1 + EA/100)^(1/12) - 1``."""
    with localcontext(Context(prec=_PRECISION)):
        return (Decimal(1) + Decimal(rate_ea) / 100) ** (Decimal(1) / 12) - 1


def french_installment(principal, rate_ea, months: int) -> Decimal:
    """French installment ``P r / (1 - (1 + r)^-n)`` rounded to the peso (``P / n`` when the rate is zero)."""
    principal = Decimal(principal)
    if months <= 0:
        raise ValueError("months must be positive")
    rate = monthly_rate(rate_ea)
    with localcontext(Context(prec=_PRECISION)):
        if rate == 0:
            return peso(principal / months)
        return peso(principal * rate / (1 - (1 + rate) ** -months))


def add_months(day: date, months: int, anchor_day: int | None = None) -> date:
    """``day`` moved ``months`` months, on ``anchor_day`` (default its own day) clamped to the month length."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(anchor_day or day.day, calendar.monthrange(year, month)[1]))


def first_payment_date(today: date, payment_day: int | None) -> date:
    """Next date, today included, that falls on ``payment_day`` (today's day when there is none)."""
    anchor = payment_day or today.day
    candidate = add_months(today.replace(day=1), 0, anchor)
    return candidate if candidate >= today else add_months(today.replace(day=1), 1, anchor)


def calendar_remaining(term_months: int | None, start_date: date | None, today: date) -> int | None:
    """Installments left by the calendar: the term minus the months elapsed since the start (None without both)."""
    if not term_months or start_date is None:
        return None
    elapsed = (today.year - start_date.year) * 12 + today.month - start_date.month
    if today.day < start_date.day:
        elapsed -= 1
    return max(0, term_months - max(0, elapsed))


def card_payment(installment, minimum_payment) -> Decimal | None:
    """Fixed monthly payment of a card: the installment the owner states, else the minimum payment."""
    for value in (installment, minimum_payment):
        if value is not None and Decimal(value) > 0:
            return Decimal(value)
    return None


class PaymentSplit(NamedTuple):
    """How a loan payment divides: ``capital`` reduces the balance; ``expense`` (interest + insurance, or the whole payment
    when it does not cover them) is what the lender keeps."""

    payment: Decimal
    capital: Decimal
    interest: Decimal
    insurance: Decimal
    expense: Decimal


def payment_split(balance, rate_ea, insurance_monthly, payment) -> PaymentSplit:
    """Split a loan ``payment`` made against ``balance``: interest = balance x monthly EA rate and insurance = the fixed one,
    both to the peso; the capital is what the payment leaves (zero when it does not cover them; an extra payment is all capital)."""
    interest = peso(Decimal(balance) * monthly_rate(rate_ea))
    insurance = peso(insurance_monthly or 0)
    payment = Decimal(payment)
    capital = max(ZERO, payment - interest - insurance)
    return PaymentSplit(payment, capital, interest, insurance, payment - capital)


def term_for(balance, rate_ea, installment) -> int | None:
    """Months a balance takes to be paid by a constant installment (None when it never is).

    Closed form ``-ln(1 - B r / P) / ln(1 + r)``; a result within ``_ROUNDING_SLACK`` of a whole number is that number
    (the installment is rounded to the peso), otherwise it is rounded up.
    """
    balance, installment = Decimal(balance), Decimal(installment)
    if balance <= 0:
        return 0
    rate = monthly_rate(rate_ea)
    with localcontext(Context(prec=_PRECISION)):
        if rate == 0:
            exact = balance / installment if installment > 0 else None
        else:
            ratio = balance * rate / installment if installment > 0 else Decimal(2)
            exact = None if ratio >= 1 else -(1 - ratio).ln() / (1 + rate).ln()
        if exact is None or exact > MAX_MONTHS:
            return None
        nearest = exact.to_integral_value(rounding=ROUND_HALF_UP)
        if abs(exact - nearest) <= _ROUNDING_SLACK:
            return max(1, int(nearest))
        return int(exact.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class Row:
    n: int
    date: date
    installment: Decimal  # interest + scheduled principal of the month (without insurance)
    interest: Decimal
    principal: Decimal
    insurance: Decimal
    extra: Decimal
    balance: Decimal  # after this month's payments


@dataclass(frozen=True)
class Schedule:
    rows: tuple[Row, ...]
    total_interest: Decimal
    new_installment: Decimal  # installment in force after the first extra payment (the original one without extras)

    @property
    def months(self) -> int:
        return len(self.rows)

    @property
    def payoff_date(self) -> date | None:
        return self.rows[-1].date if self.rows else None


def amortize(
    balance, rate_ea, installment, first_date: date, *, insurance=0, extra_monthly=0, extra_once=0,
    extra_once_month: int = 1, mode: str = "term",
) -> Schedule:
    """Month-by-month schedule of one debt from its current balance.

    Month ``n`` is paid on ``first_date`` plus ``n - 1`` months. Each month: interest on the balance, the installment
    (capped by what is owed), then the extra payment to principal. Raises ``NotAmortizingError`` when the balance does
    not go down or the debt does not end within ``MAX_MONTHS``.
    """
    if mode not in MODES:
        raise ValueError("mode must be term or installment")
    balance, installment = peso(balance), peso(installment)
    insurance, extra_monthly, extra_once = peso(insurance), peso(extra_monthly), peso(extra_once)
    rate = monthly_rate(rate_ea)
    # Months the debt would take without extras: the installment mode keeps this end date, the last row pays whatever is left.
    horizon = term_for(balance, rate_ea, installment)
    current, rows, interest_total, new_installment = balance, [], ZERO, installment
    recalculated = False
    for n in range(1, MAX_MONTHS + 1):
        if current <= 0:
            break
        interest = peso(current * rate)
        extra = extra_monthly + (extra_once if n == extra_once_month else ZERO)
        scheduled = installment - interest
        if scheduled + extra <= 0:
            raise NotAmortizingError("the payment does not cover the interest")
        if horizon is not None and n >= horizon:
            scheduled, extra = current, ZERO  # the last month of the original term pays the rounding dust too
        scheduled = min(current, scheduled)
        extra = min(current - scheduled, extra)
        current -= scheduled + extra
        rows.append(Row(n, add_months(first_date, n - 1), interest + scheduled, interest, scheduled, insurance, extra, current))
        interest_total += interest
        if mode == "installment" and extra > 0 and current > 0 and horizon is not None and horizon - n > 0:
            installment = french_installment(current, rate_ea, horizon - n)
            if not recalculated:
                new_installment, recalculated = installment, True
    if current > 0:
        raise NotAmortizingError("the debt does not end within the maximum term")
    return Schedule(tuple(rows), interest_total, new_installment)


@dataclass(frozen=True)
class DebtInput:
    """What the plan needs from one debt."""
    id: int
    balance: Decimal
    rate_ea: Decimal
    installment: Decimal
    insurance: Decimal = ZERO


@dataclass
class _State:
    debt: DebtInput
    balance: Decimal
    horizon: int | None
    interest: Decimal = ZERO
    paid_month: int | None = None
    rate: Decimal = field(default=ZERO)


@dataclass(frozen=True)
class DebtOutcome:
    debt_id: int
    months: int | None  # None: not paid off within MAX_MONTHS
    total_interest: Decimal


@dataclass(frozen=True)
class PlanResult:
    order: tuple[int, ...]  # priority order of the strategy (who receives the extra first)
    outcomes: tuple[DebtOutcome, ...]
    debt_free_month: int | None
    total_interest: Decimal


def priority(debts, strategy: str) -> list[DebtInput]:
    """Order in which the extra money is aimed: highest rate (avalanche) or smallest balance (snowball), ties by id."""
    if strategy == "avalanche":
        return sorted(debts, key=lambda d: (-d.rate_ea, d.balance, d.id))
    if strategy == "snowball":
        return sorted(debts, key=lambda d: (d.balance, -d.rate_ea, d.id))
    raise ValueError("strategy must be avalanche or snowball")


def simulate_plan(debts, extra_monthly=0, extra_once=0, strategy: str = "avalanche", redistribute: bool = True) -> PlanResult:
    """Pay every debt together, month by month, until all are paid off (or ``MAX_MONTHS``).

    Each month every open debt accrues interest and pays its own installment. What is left of the budget (the monthly
    extra, the once-only extra in month 1, the unused part of an installment that paid a debt off and the full
    installment+insurance of every debt already closed) goes to the open debts in ``priority`` order.
    ``redistribute=False`` is the baseline: every debt only ever pays its own installment, nothing moves between them.
    """
    ordered = priority(debts, strategy)
    states = {d.id: _State(d, peso(d.balance), term_for(d.balance, d.rate_ea, d.installment), rate=monthly_rate(d.rate_ea)) for d in ordered}
    for state in states.values():
        if state.balance <= 0:
            state.paid_month = 0
    freed = ZERO
    extra_monthly = peso(extra_monthly)
    for month in range(1, MAX_MONTHS + 1):
        open_states = [states[d.id] for d in ordered if states[d.id].paid_month is None]
        if not open_states:
            break
        pool = extra_monthly + (peso(extra_once) if month == 1 else ZERO) + freed
        for state in open_states:
            interest = peso(state.balance * state.rate)
            state.interest += interest
            state.balance += interest
            payment = min(state.balance, state.debt.installment)
            if state.horizon is not None and month >= state.horizon:
                payment = state.balance  # the last month of its own term settles the rounding dust
            pool += state.debt.installment - payment
            state.balance -= payment
        for state in open_states if redistribute else ():
            if state.balance > 0 and pool > 0:
                applied = min(state.balance, pool)
                state.balance -= applied
                pool -= applied
        for state in open_states:
            if state.balance <= 0:
                state.paid_month = month
                freed += state.debt.installment + state.debt.insurance
    outcomes = tuple(DebtOutcome(s.debt.id, s.paid_month, s.interest) for s in (states[d.id] for d in ordered))
    done = [o.months for o in outcomes]
    return PlanResult(
        tuple(d.id for d in ordered), outcomes, None if None in done else max(done, default=0),
        sum((o.total_interest for o in outcomes), ZERO),
    )


def payoff_order(result: PlanResult) -> list[int]:
    """Debt ids by the month they end (unpaid last), ties by priority: the order they are actually cleared."""
    position = {debt_id: index for index, debt_id in enumerate(result.order)}
    return [o.debt_id for o in sorted(result.outcomes, key=lambda o: (o.months is None, o.months or 0, position[o.debt_id]))]
