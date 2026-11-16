"""Conservative Colombian bank templates. Unknown formats fall through to the LLM."""

import re
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation

from app.models.schemas import TransactionAnalysis


def _fold(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFD", text.casefold()) if unicodedata.category(char) != "Mn")


_IGNORE = re.compile(
    r"\b(?:rechazad[ao]s?|declinad[ao]s?|denegad[ao]s?|no\s+(?:fue\s+)?aprobad[ao]|"
    r"no\s+(?:se\s+)?(?:realizo|pudo\s+realizar)|fondos\s+insuficientes|"
    r"fecha\s+(?:limite\s+)?de\s+pago|proximo\s+pago|pago\s+minimo|"
    r"recordatorio|extracto|estado\s+de\s+cuenta|codigo\s+(?:de\s+)?(?:seguridad|verificacion|acceso|autorizacion)|"
    r"clave\s+(?:dinamica|temporal)|otp|one.time\s+password)\b"
)
_PROMOTIONAL_SUBJECT = re.compile(r"\b(?:promocion(?:es)?|descuentos?|ofertas?|preaprobados?)\b")


def should_ignore(text: str, subject: str = "") -> bool:
    introduction = " ".join(text.split())[:300]
    return bool(_IGNORE.search(_fold(subject + "\n" + introduction)) or _PROMOTIONAL_SUBJECT.search(_fold(subject)))


def normalize_merchant(value: str) -> str:
    value = " ".join(value.upper().split())
    return re.sub(r"^(?:(?:DLO|UBR|PAYU|MERCADOPAGO)\s*\*\s*)+", "", value).strip()


_AMOUNT = r"(?P<currency>US\$|USD|COP|\$)\s*(?P<amount>\d[\d.,]*(?:\s\d{3})*)"
# Templates intentionally require an explicit completed action and currency marker.
_TEMPLATES = [
    ("withdrawal", re.compile(
        r"\b(?:compraste|pagaste|compra(?:\s+(?:aprobada|exitosa))?|pago\s+(?:aprobado|exitoso)|realizaste\s+(?:una\s+)?compra)"
        r"\s*(?:por|de|por\s+valor\s+de|:)?\s*" + _AMOUNT +
        r"\s*(?:en|a)\s+(?P<merchant>.+?)(?=\s+(?:con\s+(?:tu|su)|(?:el\s+)?\d{1,4}[-/]\d{1,2}[-/]\d{1,4}|a\s+las\s+\d|con\s+(?:tarjeta|tc)|desde\s+tu)|[\n;]|$)", re.I)),
    ("withdrawal", re.compile(
        r"\b(?:transferiste|enviaste)\s*(?:por|:)?\s*" + _AMOUNT +
        r"\s+a\s+(?P<merchant>.+?)(?=\s+(?:desde|el\s+\d|a\s+las\s+\d)|[\n;]|$)", re.I)),
    ("deposit", re.compile(
        r"\b(?:recibiste|recibio|recibimos|te\s+llegaron)\s*(?:un\s+pago\s+(?:de|por)\s*|una\s+transferencia\s+(?:de|por)\s*|:)?" + _AMOUNT +
        r"\s+de\s+(?P<merchant>.+?)(?=\s+(?:en\s+tu|el\s+\d|a\s+las\s+\d)|[\n;]|$)", re.I)),
    ("withdrawal", re.compile(
        r"\b(?:retiraste|retiro\s+exitoso)\s*(?:por|de|:)?\s*" + _AMOUNT +
        r"\s+(?:en|de)\s+(?P<merchant>.+?)(?=\s+(?:el\s+\d|a\s+las\s+\d|con\s+tu)|[\n;]|$)", re.I)),
]


def _amount(value: str) -> Decimal:
    value = value.replace(" ", "")
    if "," in value and "." in value:
        decimal = "," if value.rfind(",") > value.rfind(".") else "."
        value = value.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif "," in value or "." in value:
        separator = "," if "," in value else "."
        pieces = value.split(separator)
        if len(pieces[-1]) == 3 and all(len(piece) == 3 for piece in pieces[1:]):
            value = "".join(pieces)
        elif len(pieces) == 2 and len(pieces[-1]) in {1, 2}:
            value = value.replace(separator, ".")
        else:
            raise ValueError("Ambiguous amount")
    result = Decimal(value)
    if result <= 0:
        raise ValueError("Amount must be positive")
    return result


def _date(text: str) -> str | None:
    match = re.search(r"\b(?P<date>\d{4}[-/]\d{2}[-/]\d{2}|\d{1,2}[/.-]\d{1,2}[/.-](?:\d{4}|\d{2}))(?:[T\s]+(?:a\s+las\s+)?(?P<time>\d{1,2}:\d{2}(?::\d{2})?)(?:\s*(?P<ampm>AM|PM))?(?P<offset>Z|[+-]\d{2}:\d{2})?)?", text, re.I)
    if not match:
        return None
    date = match["date"]
    try:
        if re.match(r"\d{4}[-/]", date):
            day = datetime.strptime(date.replace("/", "-"), "%Y-%m-%d")
        else:
            normalized = re.sub(r"[.-]", "/", date)
            day = datetime.strptime(normalized, "%d/%m/%Y" if len(normalized.split("/")[-1]) == 4 else "%d/%m/%y")
        if not match["time"]:
            return day.date().isoformat()
        hour, minute, *second = [int(value) for value in match["time"].split(":")]
        if match["ampm"]:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if match["ampm"].upper() == "PM" else 0)
        day = day.replace(hour=hour, minute=minute, second=second[0] if second else 0)
        return day.isoformat() + (match["offset"] or "-05:00")
    except ValueError:
        return None


def parse_transaction(text: str, subject: str = "", sender: str = "") -> TransactionAnalysis | None:
    if should_ignore(text, subject):
        return None
    combined = subject + "\n" + text
    for kind, template in _TEMPLATES:
        match = template.search(combined)
        if not match:
            continue
        try:
            amount = _amount(match["amount"].rstrip(".,"))
        except (ValueError, InvalidOperation):
            continue
        merchant = normalize_merchant(match["merchant"].strip(" .,:"))
        if not merchant:
            continue
        return TransactionAnalysis(
            is_transaction=True, amount=amount,
            currency="USD" if match["currency"].upper() in {"US$", "USD"} else "COP",
            merchant=merchant, description=f"{'Ingreso' if kind == 'deposit' else 'Pago'} {merchant}",
            transaction_type=kind, date=_date(combined),
        )
    return None
