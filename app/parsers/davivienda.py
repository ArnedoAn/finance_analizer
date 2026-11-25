"""Completed transaction vocabulary for davivienda, plus its structured account-movement notice.

The notice («Le informamos que se ha registrado el siguiente movimiento de su Cta de Ahorros terminada (o) en ****0000»)
has no action phrase: it is a list of labelled fields, so ``notice`` reads it directly instead of going through ``TEMPLATES``.
"""

import re
from datetime import date, datetime
from decimal import Decimal

from .common import BOGOTA, REFUND, fold, normalize_merchant, parse_amount, template
from .types import Extraction, Kind

TEMPLATES = (
    template('compra', 'withdrawal', 'compra(?:\\s+(?:aprobada|exitosa))?|compraste|realizaste\\s+(?:una\\s+)?compra', False),
    template('pago', 'withdrawal', 'pagaste|pago\\s+exitoso', True),
    template('transferencia', 'withdrawal', 'transferiste|enviaste', True),
    template('abono', 'deposit', 'recibiste|abono(?:\\s+recibido)?', True),
)

CARD_PAYMENT_MERCHANT = "PAGO TARJETA DE CREDITO"
CASH_MERCHANT = "RETIRO"

_FIELD = re.compile(r"^\s*([^:\n]{1,40}?)\s*:\s*(.*?)\s*$")
_ACCOUNT = re.compile(r"\b(?:cta\.?|cuenta)\s+(?:de\s+)?(ahorros|corriente)\b")
_LAST4 = re.compile(r"\bterminad[ao]\s*(?:\(\s*[ao]\s*\)\s*)?en\s*[*x\s]*(\d{4})\b")
_VALUE = re.compile(r"(?P<currency>US\$|USD|COP|\$)?\s*(?P<amount>\d[\d.,]*)", re.I)
_THOUSANDS_COMMA = re.compile(r"[1-9]\d{0,2}(?:,\d{3})+")
_YEAR_FIRST = re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})")
_DAY_FIRST = re.compile(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})")
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")
_DEPOSIT_CLASSES = ("abono", "consignacion", "transferencia recibida", "deposito")


def _fields(text: str) -> dict[str, str]:
    """«Label: value» lines keyed by the folded label; the first occurrence of a label wins."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        if match := _FIELD.match(line):
            found.setdefault(fold(match[1]), match[2])
    return found


def _amount(raw: str) -> tuple[Decimal | None, str, str | None]:
    """In this notice a comma groups thousands («$1,651,063» is 1.651.063 pesos); ``parse_amount`` rejects that as ambiguous."""
    match = _VALUE.fullmatch(raw.strip().rstrip(".,"))
    if not match:
        return None, "COP", "Invalid amount"
    currency = "USD" if fold(match["currency"] or "") in {"us$", "usd"} else "COP"
    digits = match["amount"]
    if currency == "COP" and _THOUSANDS_COMMA.fullmatch(digits):
        digits = digits.replace(",", "")
    try:
        return parse_amount(digits, currency), currency, None
    except ValueError as exc:
        return None, currency, str(exc)


def _when(day: str, clock: str) -> tuple[datetime | None, date | None]:
    """``Fecha`` + ``Hora`` in Bogota time; a date without a readable time is a date only."""
    if match := _YEAR_FIRST.fullmatch(day):
        parts = match[1], match[2], match[3]
    elif match := _DAY_FIRST.fullmatch(day):
        parts = match[3], match[2], match[1]
    else:
        return None, None
    try:
        moment = date(*map(int, parts))
    except ValueError:
        return None, None
    if not (time := _CLOCK.fullmatch(clock)):
        return None, moment
    try:
        return datetime(moment.year, moment.month, moment.day, int(time[1]), int(time[2]), int(time[3] or 0), tzinfo=BOGOTA), None
    except ValueError:
        return None, moment


def _account(folded: str) -> str | None:
    if last4 := _LAST4.search(folded):
        return last4[1]
    if kind := _ACCOUNT.search(folded):
        return kind[1].capitalize()
    return None


def _card(fields: dict[str, str]) -> str | None:
    """The card the payment goes to, when the notice names one: only its last four digits are kept."""
    for label, value in fields.items():
        if "tarjeta" in label and (digits := re.search(r"(\d{4})\D*$", value)):
            return f"Tarjeta ****{digits[1]}"
    return None


def _classify(movement: str, place: str, fields: dict[str, str]) -> tuple[str, Kind, str | None, str | None] | None:
    """(template, kind, merchant, counterparty) for a movement class, or None when it is not one the bank vocabulary covers.

    Folded class, in this order: a card payment, anything that brings money in (so «Abono pago de nomina» is never a
    payment), a purchase, a cash withdrawal, other payments and discounts, a transfer sent.
    """
    folded = fold(movement)
    name = normalize_merchant(re.sub(r"\bdescuento\b", " ", movement, flags=re.I)) or None
    if "pago tarjeta de credito" in folded or "pago tc" in folded:
        return "aviso_pago_tarjeta", "withdrawal", CARD_PAYMENT_MERCHANT, _card(fields)
    if any(word in folded for word in _DEPOSIT_CLASSES):
        return "aviso_abono", "deposit", name, None
    if "compra" in folded:
        return "aviso_compra", "withdrawal", normalize_merchant(place) or None, None
    if "retiro" in folded:
        return "aviso_retiro", "withdrawal", normalize_merchant(f"{CASH_MERCHANT} {place}") or CASH_MERCHANT, None
    if "pago" in folded or "descuento" in folded:
        return "aviso_pago", "withdrawal", name, None
    if "transferencia" in folded:
        return "aviso_transferencia", "withdrawal", name, None
    return None


def notice(text: str) -> Extraction | None:
    """The structured movement notice of the bank, or None when ``text`` is not one (or its class is unknown: the LLM decides)."""
    fields = _fields(text)
    raw_amount, movement = fields.get("valor transaccion"), fields.get("clase de movimiento", "").strip(" ,.;")
    if not raw_amount or not movement or REFUND.search(fold(movement)):
        return None
    if not (classified := _classify(movement, fields.get("lugar de transaccion", ""), fields)):
        return None
    name, kind, merchant, counterparty = classified
    amount, currency, reason = _amount(raw_amount)
    occurred_at, date_only = _when(fields.get("fecha", ""), fields.get("hora", ""))
    return Extraction(
        kind="review" if amount is None else kind, amount=amount, currency=currency, merchant=merchant,
        occurred_at=occurred_at, date_only=date_only, account_hint=_account(fold(text)), counterparty_hint=counterparty,
        template_id=f"davivienda.{name}", reason=reason,
    )
