"""Shared conservative grammar; bank modules own the completed-action vocabulary.

Ambiguous money is never guessed. Dates require an explicit transaction label and
merchant tails are discarded before account creation can see them.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .types import Extraction, Kind

BOGOTA = ZoneInfo("America/Bogota")
MONEY = r"(?P<currency>US\$|USD|COP|\$)\s*(?P<amount>\d[\d.,]*)"
FAILURE = re.compile(
    r"\b(?:intento|rechazad[ao]|declinad[ao]|denegad[ao]|cancelad[ao]|"
    r"no\s+(?:fue\s+)?(?:exitos[ao]|autorizad[ao]|aprobad[ao])|"
    r"no\s+(?:se\s+)?(?:realizo|pudo\s+realizar)|fondos\s+insuficientes)\b"
)
REFUND = re.compile(r"\b(?:revers\w*|anul\w*|devoluci\w*|reembols\w*)\b")
NON_TRANSACTION = re.compile(
    r"\b(?:otp|extracto|recordatorio|estado\s+de\s+cuenta|clave\s+(?:dinamica|temporal)|"
    r"codigo\s+(?:de\s+)?(?:seguridad|verificacion|acceso|autorizacion)|"
    r"one.time\s+password|promocion\w*|descuentos?|ofertas?|preaprobad\w*|"
    r"(?:tu\s+)?fecha\s+(?:(?:limite|de)\s+)?pago|fecha\s+de\s+pago|"
    r"proximo\s+pago|pago\s+minimo)\b"
)
MERCHANT_END = re.compile(
    r"[.,;\n]|\s+-\s+|\s+(?:saldo\b|si\s+no\b|(?:pagad[ao]\s+a\s+)?\d+\s+cuotas\b|"
    r"cuotas\b|pagad[ao]\s+a\b|con\s+(?:tu|su|tarjeta|tc)\b|desde\s+tu\b|"
    r"en\s+tu\b|fecha\s*:|hora\s*:|el\s+\d|a\s+las\s+\d|fue\b|"
    r"\d{1,2}:\d{2}(?!\d)|\d{1,2}/\d{1,2}/\d{2,4}(?!\d))", re.I
)


def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.casefold())
                   if unicodedata.category(c) != "Mn")


def parse_amount(raw: str, currency: str = "COP") -> Decimal:
    """Accept canonical grouping and currency-specific decimals, never space grouping."""
    value = raw.strip()
    if currency not in {"COP", "USD"}:
        raise ValueError("Unsupported currency")
    integer = r"(?:0|[1-9]\d*)"
    grouped_cop = r"[1-9]\d{0,2}(?:\.\d{3})+"
    grouped_usd = r"[1-9]\d{0,2}(?:,\d{3})+"
    if currency == "COP":
        if re.fullmatch(rf"(?:{integer}|{grouped_cop})(?:,00)?", value):
            value = value.replace(".", "").replace(",", ".")
        elif re.fullmatch(rf"{integer}\.00", value):
            pass
        elif re.fullmatch(rf"{grouped_usd}\.00", value):
            value = value.replace(",", "")
        else:
            raise ValueError("Ambiguous COP amount")
    elif re.fullmatch(rf"(?:{integer}|{grouped_usd})(?:\.\d{{1,2}})?", value):
        value = value.replace(",", "")
    else:
        raise ValueError("Ambiguous USD amount")
    try:
        amount = Decimal(value)
        if amount <= 0:
            raise ValueError("Amount must be positive")
        return amount.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("Invalid amount") from exc


def normalize_merchant(value: str) -> str:
    value = MERCHANT_END.split(value, maxsplit=1)[0]
    value = " ".join(value.upper().split()).strip(" :*- ")
    value = re.sub(r"^(?:(?:DLO|UBR|PAYU|MERCADOPAGO)\s*\*\s*)+", "", value)
    if not value or len(value) > 60 or "$" in value or re.search(r"\d{5,}", value):
        return ""
    return value


DATE = r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-](?:\d{4}|\d{2}))"
DATE_ANCHOR = re.compile(rf"\b(?:el|del|fecha\s*:)\s*(?P<date>{DATE})(?!\d)", re.I)
TIME = re.compile(
    r"(?:\ba\s+las\s+|\bhora\s*:\s*|[T ])(?P<time>\d{1,2}:\d{2}(?::\d{2})?)"
    r"\s*(?P<ampm>AM|PM)?(?P<offset>Z|[+-]\d{2}:\d{2})?", re.I
)


def transaction_date(text: str) -> tuple[datetime | None, date | None]:
    for match in DATE_ANCHOR.finditer(text):
        # A label embedded in a due-date phrase is not a transaction anchor.
        prefix = fold(text[max(0, match.start() - 35):match.start()])
        if re.search(r"(?:vence|vencimiento|limite|pago\s+hasta)[^.;\n]*$", prefix):
            continue
        try:
            value = match["date"]
            if re.match(r"\d{4}-", value):
                day = datetime.strptime(value, "%Y-%m-%d")
            else:
                value = re.sub(r"[.-]", "/", value)
                day = datetime.strptime(value, "%d/%m/%Y" if len(value.split("/")[-1]) == 4 else "%d/%m/%y")
            # Explicit labels after a date or 'a las ... del DATE' before it.
            after = text[match.end():match.end() + 50]
            before = text[max(0, match.start() - 35):match.start()]
            clock = TIME.match(after) or TIME.match(after.lstrip()) or TIME.search(before)
            if not clock:
                return None, day.date()
            hour, minute, *seconds = map(int, clock["time"].split(":"))
            if clock["ampm"]:
                if not 1 <= hour <= 12:
                    return None, None
                hour = hour % 12 + (12 if clock["ampm"].upper() == "PM" else 0)
            offset = clock["offset"]
            tz = datetime.fromisoformat("2000-01-01T00:00:00" + offset.replace("Z", "+00:00")).tzinfo if offset else BOGOTA
            return day.replace(hour=hour, minute=minute, second=seconds[0] if seconds else 0, tzinfo=tz), None
        except ValueError:
            return None, None
    return None, None


@dataclass(frozen=True)
class Template:
    name: str
    kind: Kind
    pattern: re.Pattern[str]
    counterparty: bool = False


def template(name: str, kind: Kind, action: str, counterparty: bool = False) -> Template:
    return Template(name, kind, re.compile(
        rf"\b(?:{action})\s*(?:(?:por\s+valor\s+de|por|de|valor|monto)\s*:?\s*|:\s*)?{MONEY}", re.I
    ), counterparty)


@dataclass(frozen=True)
class Candidate:
    start: int
    end: int
    extraction: Extraction


_KEY = re.compile(r"(?:la\s+)?llave\s+(\S+)", re.I)
KEY_MERCHANT = "LLAVE ···"  # followed by the last four characters of the key
QR_MERCHANT = "PAGO QR"


def candidates(text: str, bank: str, templates: tuple[Template, ...]) -> list[Candidate]:
    results = []
    normalized = fold(text)
    for rule in templates:
        for match in rule.pattern.finditer(normalized):
            currency = "USD" if match["currency"] in {"us$", "usd"} else "COP"
            # Patterns run on accent-folded text, whose offsets preserve Spanish text.
            tail = text[match.end():]
            tail = re.split(r"[\n;]|(?<=[.!?])\s+", tail, maxsplit=1)[0]
            merchant_match = re.match(r"\s*(?:en|a|de)\s+(.+)", tail, re.I)
            counterparty = None
            account = None
            source = re.search(r"desde\s+tu\s+cuenta\s+(\*?\d{4})", tail, re.I)
            if source:
                account = source[1].lstrip("*")
                # «a las 17:50» is the time, not the destination.
                destination = re.search(r"\s+a\s+(?!las?\s+\d)(?:la\s+cuenta\s+)?(.+)", tail[source.end():], re.I)
                merchant_match = destination
            merchant_raw = merchant_match[1] if merchant_match else ""
            if rule.counterparty or re.match(r"(?:tarjeta|tu\s+bolsillo|bolsillo)\b", fold(merchant_raw)):
                counterparty = MERCHANT_END.split(merchant_raw, maxsplit=1)[0].strip()[:120] or None
            merchant = normalize_merchant(merchant_raw) or None
            key = _KEY.match(merchant_raw)
            if key and (tail_digits := re.sub(r"[^0-9A-Za-z]", "", key[1])[-4:]):
                # A Bre-B / Bancolombia «llave» has no merchant name: its last four characters name the counterparty.
                merchant = f"{KEY_MERCHANT}{tail_digits}"
                counterparty = f"la llave ···{tail_digits}"  # never the whole key: a phone number would contain some own account's last four
            elif not merchant and re.search(r"codigo\s+qr", fold(tail)):
                merchant = QR_MERCHANT
            product = re.search(r"(?:con|desde|en)\s+(?:tu|su)\s+(?:tarjeta\s+)?(?:t\.?\s*cred\w*|cuenta|ahorros|tarjeta)(?:\s+(?:de\s+credito|terminada\s+en))?\s*\*?(\d{4})\b", fold(tail))
            if product and not account:
                account = product[1]
            if not account:
                if re.search(r"\bt\.?cred\b|tarjeta\s+de\s+credito", fold(tail)) and not counterparty:
                    account = "T.Cred"
                elif re.search(r"\bahorros\b", fold(tail)):
                    account = "Ahorros"
            occurred_at, date_only = transaction_date(text[match.start():match.end() + 300])
            try:
                amount = parse_amount(match["amount"].rstrip(".,"), currency)
                reason = None
                kind = rule.kind
            except ValueError as exc:
                amount, kind, reason = None, "review", str(exc)
            results.append(Candidate(match.start(), match.end(), Extraction(
                kind=kind, amount=amount, currency=currency,
                merchant=merchant,
                occurred_at=occurred_at, date_only=date_only, account_hint=account,
                counterparty_hint=counterparty, template_id=f"{bank}.{rule.name}", reason=reason,
            )))
    return results
