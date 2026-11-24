"""Credit cards (own accounts of kind ``credit``): next statement cut, next payment, interest-free days and which card to use today.

``statement_day`` and ``payment_day`` (1-31, clamped to the month length) live on the ``own_accounts`` row. A purchase made on
or before a cut belongs to the statement that closes on that cut and is paid on the first ``payment_day`` AFTER the cut, so the
interest-free days of a purchase today are ``payment date - today``. The recommended card is the one with the most of them (the
first by name wins a tie). The period balance is the card's charges since the previous cut when Firefly answers (best effort,
``None`` otherwise). ``alert_check`` emits ``card_payment_due`` 3 and 1 days before the payment. Functions never commit.
"""
import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.core.logging import get_logger
from app.services.alerts import ONCE_MINUTES, emit, money

logger = get_logger(__name__)

ALERT = "card_payment_due"
ALERT_DAYS = (3, 1)


def at_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _shift(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + month - 1 + delta
    return index // 12, index % 12 + 1


def next_on_or_after(today: date, day: int) -> date:
    """First date ``>= today`` that is day ``day`` of its month (clamped)."""
    here = at_day(today.year, today.month, day)
    if here >= today:
        return here
    year, month = _shift(today.year, today.month, 1)
    return at_day(year, month, day)


def previous_before(anchor: date, day: int) -> date:
    """Last date ``< anchor`` that is day ``day`` of its month (clamped)."""
    here = at_day(anchor.year, anchor.month, day)
    if here < anchor:
        return here
    year, month = _shift(anchor.year, anchor.month, -1)
    return at_day(year, month, day)


def payment_for(cut: date, payment_day: int) -> date:
    """The payment date of the statement that closes on ``cut``: the first ``payment_day`` strictly after the cut."""
    return next_on_or_after(cut + timedelta(days=1), payment_day)


def _cards(accounts) -> list:
    return [account for account in accounts if account.kind == "credit"]


async def _period_spent(firefly, name: str, start: date, end: date) -> str | None:
    """Charges of the card between two dates from Firefly, or None when Firefly cannot tell."""
    try:
        match = next((row for row in await firefly.accounts() if row["name"].casefold() == name.casefold()), None)
        if match is None:
            return None
        total = Decimal("0")
        for row in await firefly.account_transactions(match["id"], start, end):
            if str(row.get("source_id")) == str(match["id"]) and row.get("type") in {"withdrawal", "transfer"}:
                total += Decimal(str(row.get("amount") or "0"))
        return f"{total:.2f}"
    except Exception as exc:  # optional enrichment: Firefly down never breaks the advice or the alerts
        logger.warning("card_period_unavailable", error_type=type(exc).__name__)
        return None


async def advice(session, firefly, today: date, accounts=None) -> dict:
    """Body of ``GET /cards/advice``: one entry per credit card and the best card to use ``today``."""
    from app.services import own_accounts

    cards = _cards(accounts if accounts is not None else await own_accounts.load(session))
    entries = []
    for card in cards:
        entry = {
            "account": card.firefly_account, "bank": card.bank, "statement_day": card.statement_day, "payment_day": card.payment_day,
            "next_statement": None, "next_payment": None, "interest_free_days": None, "period_spent": None, "recommended": False,
        }
        if card.payment_day is not None:
            entry["next_payment"] = next_on_or_after(today, card.payment_day).isoformat()
        if card.statement_day is not None:
            cut = next_on_or_after(today, card.statement_day)
            entry["next_statement"] = cut.isoformat()
            if card.payment_day is not None:
                entry["interest_free_days"] = (payment_for(cut, card.payment_day) - today).days
            start = previous_before(cut, card.statement_day) + timedelta(days=1)
            entry["period_spent"] = await _period_spent(firefly, card.firefly_account, start, today)
        entries.append(entry)
    ranked = [entry for entry in entries if entry["interest_free_days"] is not None]
    best = max(ranked, key=lambda entry: entry["interest_free_days"], default=None)
    if best is not None:
        best["recommended"] = True
    return {
        "date": today.isoformat(), "items": entries,
        "recommended": {"account": best["account"], "interest_free_days": best["interest_free_days"]} if best else None,
    }


async def alert_check(session, firefly, today: date) -> int:
    """``card_payment_due`` alerts 3 and 1 days before each card's payment, with the period balance when Firefly gives it."""
    from app.services import own_accounts

    emitted = 0
    for card in _cards(await own_accounts.load(session)):
        if card.payment_day is None:
            continue
        due = next_on_or_after(today, card.payment_day)
        days = (due - today).days
        if days not in ALERT_DAYS:
            continue
        spent = None
        if card.statement_day is not None:
            cut = previous_before(due, card.statement_day)
            start = previous_before(cut, card.statement_day) + timedelta(days=1)
            spent = await _period_spent(firefly, card.firefly_account, start, cut)
        balance = f" Saldo del periodo: {money(Decimal(spent))}." if spent is not None else ""
        when = "mañana" if days == 1 else f"en {days} días"
        alert = await emit(
            session, ALERT, title=f"Pago de {card.firefly_account} {when}",
            body=f"El pago de la tarjeta «{card.firefly_account}» vence {when} ({due:%d/%m/%Y}).{balance}",
            amount=Decimal(spent) if spent is not None else None, currency="COP" if spent is not None else None,
            dedupe_key=f"{ALERT}:{card.firefly_account}:{due.isoformat()}:{days}", min_interval_minutes=ONCE_MINUTES,
        )
        emitted += alert is not None
    return emitted


def parse_day(value: str | None, today: date) -> date:
    """The ``date`` query value (``YYYY-MM-DD``) or ``today``; ``ValueError`` when malformed."""
    return today if not value else datetime.strptime(value, "%Y-%m-%d").date()
