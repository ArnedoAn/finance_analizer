"""Quick free-text entry such as "25k taxi" (contract section 13).

``parse`` is pure; ``create_entry`` wires it to ``Processor.manual`` so the entry is the owner's own, approved and
written to Firefly, and idempotent by ``client_ref``; above ``REVIEW_AMOUNT_COP`` it stays ``needs_review`` instead.
"""
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.models.schemas import ManualTransaction
from app.parsers import normalize_merchant
from app.services import own_accounts, suggestions

MAX_AMOUNT = Decimal("9999999999.99")  # ManualTransaction allows 12 digits, 2 decimals
THOUSANDS = re.compile(r"^\d{1,3}(?:\.\d{3})+$")
NUMBER = re.compile(
    r"(?<![\w.,])(?:(?P<currency>\$)\s*)?(?P<number>\d+(?:[.,]\d+)*)"
    r"(?:(?P<short>[km])|\s?(?P<word>millones|millon|mil))?(?![^\W_])",
    re.IGNORECASE,
)
MULTIPLIERS = {"k": 1_000, "mil": 1_000, "m": 1_000_000, "millon": 1_000_000, "millones": 1_000_000}

AMBIGUOUS_AMOUNT = "Indica el monto con $ o k"


class UnparseableError(ValueError):
    """The text has no usable amount."""


@dataclass(frozen=True)
class QuickEntry:
    amount: Decimal
    description: str
    merchant: str | None
    account: str | None


def _number(raw: str, scaled: bool) -> Decimal | None:
    """Colombian notation: '.' groups thousands, ',' is the decimal mark."""
    dots, commas = raw.count("."), raw.count(",")
    if dots and commas:
        decimal = "." if raw.rfind(".") > raw.rfind(",") else ","
        group = "," if decimal == "." else "."
        if raw.count(decimal) != 1 or raw.rfind(group) > raw.rfind(decimal):
            return None
        normalized = raw.replace(group, "").replace(decimal, ".")
    elif commas:
        normalized = raw.replace(",", "") if commas > 1 else raw.replace(",", ".")
    elif dots > 1:
        if not THOUSANDS.match(raw):
            return None
        normalized = raw.replace(".", "")
    elif dots and not scaled and THOUSANDS.match(raw):
        normalized = raw.replace(".", "")
    else:
        normalized = raw
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _find_amount(text: str):
    """Best amount token: explicit ``$`` first, then a k/mil/m suffix. Several bare numbers are ambiguous."""
    best, best_key, candidates = None, None, 0
    for match in NUMBER.finditer(text):
        suffix = (match["short"] or match["word"] or "").casefold()
        value = _number(match["number"], bool(suffix))
        if value is None:
            continue
        amount = (value * MULTIPLIERS.get(suffix, 1)).quantize(Decimal("0.01"), ROUND_HALF_UP)
        if amount <= 0 or amount > MAX_AMOUNT:
            continue
        candidates += 1
        key = (bool(match["currency"]), bool(suffix), amount)
        if best_key is None or key > best_key:
            best, best_key = (match, amount), key
    if best_key is not None and not (best_key[0] or best_key[1]) and candidates > 1:
        # "pago 45.000 ref 2000000": guessing the largest would write a reference number as the amount.
        raise UnparseableError(AMBIGUOUS_AMOUNT)
    return best


def _find_account(text: str, accounts):
    for account in accounts:
        for alias in account.aliases:
            found = re.search(rf"(?<!\w){re.escape(alias.strip())}(?!\w)", text, re.IGNORECASE) if alias.strip() else None
            if found:
                return account.firefly_account, found.span()
    return None, None


def parse(text: str, accounts) -> QuickEntry:
    """Interpret "25k taxi", "taxi 25.000", "$1.2m arriendo" or "25000 almuerzo nequi"; ``accounts`` are the own accounts whose aliases may name the account."""
    text = " ".join((text or "").split())
    found = _find_amount(text)
    if found is None:
        raise UnparseableError("No amount found in the text")
    match, amount = found
    rest = f"{text[:match.start()]} {text[match.end():]}"
    account, span = _find_account(rest, accounts)
    if span:
        rest = f"{rest[:span[0]]} {rest[span[1]:]}"
    rest = " ".join(rest.split()).strip(" -,:;")
    return QuickEntry(
        amount=amount,
        description=rest or text,
        merchant=normalize_merchant(rest) or None,
        account=account,
    )


async def create_entry(service, *, text, client_ref, account=None, principal=None):
    """Create (or replay) the quick entry; returns the inbox item.

    Raises ``UnparseableError`` or ``ValueError`` (validation) before anything is written.
    """
    from app.services import audit

    ref = f"quick:{client_ref}"
    existing = await service.inbox.by_ref(f"manual:{ref}")
    if existing is not None:
        return existing
    entry = parse(text, await own_accounts.load(service.session))
    category = None
    suggested = await suggestions.suggest_for_merchant(service.session, entry.merchant, text=entry.description, limit=1, overall_fallback=False)
    if suggested:
        try:
            known = await service.categories()
        except Exception:  # Firefly down: the entry is created uncategorized and fails/retries below
            known = []
        category = next((name for name in known if name.casefold() == suggested[0].casefold()), None)
    payload = ManualTransaction(
        client_ref=ref, amount=entry.amount, description=entry.description, type="withdrawal",
        category=category, account=account or entry.account, merchant=entry.merchant,
    )
    # An unusually large figure typed fast is more likely a typo or a reference number than a deliberate amount:
    # above the review threshold the entry waits for approval instead of being written to Firefly.
    hold = "quick entry over the review threshold" if entry.amount > service.settings.review_amount_cop else None
    item = await service.manual(payload, hold=hold)
    await audit.record(
        service.session, action="transactions.quick", target_type="inbox_item", target_id=item.id,
        detail={"client_ref": client_ref, "amount": str(entry.amount), "category": category, "status": item.status},
        principal=principal,
    )
    return item
