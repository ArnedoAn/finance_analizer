"""PSE (ACH Colombia) payments: recognizing the receipt, its state, the paid entity and the duplicates it creates.

The owner pays utilities and other services through PSE from several banks. What always arrives is the receipt by e-mail
from the PSE sender; the bank's own notification (Dale, Bancolombia...) comes only sometimes. Both describe one payment.

* ``tag`` marks the movement with the tag ``pse`` and cleans the paid entity (no NIT, no legal suffix) while the item is
  being extracted; ``check`` sends to review a receipt whose «Estado» says the payment was not approved although the model
  proposed to register it; both are hooked in ``Processor._extract``.
* ``supersedes`` is asked by ``Processor.process`` when ``InboxRepository.find_duplicate`` found an earlier movement for
  the same payment: when the new item names the paid entity and the earlier one does not (a bank notification that only
  says «Pago PSE»), the earlier one is voided and the new one is registered; otherwise the earlier one stays.
* The duplicate window for PSE payments (2 days, same amount and own account, different source or sender) lives in
  ``InboxRepository.find_duplicate`` and uses ``is_pse_item`` / ``DUPLICATE_WINDOW`` from here.

The own account is the BANK that pays: the PSE sender serves several accounts, so each account that uses PSE needs the PSE
sender among its ``email:`` sources and the bank name among its aliases; the extraction returns the bank as
``account_hint`` (see the extraction prompt).
"""
import re
from datetime import timedelta

from app.core.logging import get_logger
from app.parsers.common import fold, normalize_merchant

logger = get_logger(__name__)

TAG = "pse"
DESCRIPTION_PREFIX = "Pago PSE a "
DUPLICATE_WINDOW = timedelta(days=2)
# Names that say «a PSE payment» but not to whom.
GENERIC_ENTITIES = frozenset({
    "PSE", "PAGO PSE", "PAGOS PSE", "PAGO POR PSE", "PAGO CON PSE", "COMPRA PSE", "PAGO", "PAGOS", "COMPRA", "TRANSACCION",
    "ACH", "ACH COLOMBIA", "PSE ACH COLOMBIA", "PAGOS SEGUROS EN LINEA", "PAGO SEGURO EN LINEA",
})

_IS_PSE = re.compile(r"\bpse\b|pagos?\s+seguros?\s+en\s+linea|\bach\s+colombia\b")
_STATE_LABEL = re.compile(r"\bestado(?:\s+(?:de\s+la\s+transaccion|del\s+pago|de\s+pago|de\s+la\s+transferencia))?\s*[:\-]?\s*([a-z]+(?:\s+[a-z]+)?)")
_REJECTED = re.compile(r"rechazad|fallid|no\s+(?:fue\s+)?(?:exitos|aprobad|autorizad|aceptad)|declinad|cancelad|expirad|denegad|anulad")
_PENDING = re.compile(r"pendiente|en\s+proceso|en\s+espera|procesando|por\s+confirmar")
_APPROVED = re.compile(r"aprobad|exitos|aceptad|autorizad|confirmad")
_NIT = re.compile(r"\b(?:nit|n\.?\s?i\.?\s?t\.?)\b[\s:.\-#]*[\d.\-]*.*$", re.IGNORECASE)
_LEGAL_TAIL = re.compile(r"[\s,.]+(?:s\s?\.?\s?a\s?\.?\s?s|s\s?\.?\s?a|e\s?\.?\s?s\s?\.?\s?p|ltda|e\s?\.?\s?i\s?\.?\s?c\s?\.?\s?e|s\s?\.?\s?c\s?\.?\s?a)[\s,.]*$", re.IGNORECASE)


def is_pse(text: str | None) -> bool:
    """The message is about a PSE payment."""
    return bool(_IS_PSE.search(fold(text or "")))


def _classify(words: str) -> str | None:
    for state, pattern in (("rejected", _REJECTED), ("pending", _PENDING), ("approved", _APPROVED)):
        if pattern.search(words):
            return state
    return None


def status(text: str | None) -> str | None:
    """``approved``, ``rejected`` or ``pending`` from the receipt's «Estado», else from the words of the whole text; None if unknown.

    The labelled state wins: footers such as «si su pago fue rechazado, contacte...» must not turn an approved receipt into a rejected one.
    """
    folded = fold(text or "")
    labelled = _STATE_LABEL.search(folded)
    if labelled and (state := _classify(labelled.group(1))):
        return state
    return _classify(folded)


def check(text: str | None, kind: str | None) -> list[str]:
    """Review reasons for a PSE message the model would register although the state says otherwise (empty when fine)."""
    if kind in {"ignore", "review"} or not is_pse(text):
        return []
    state = status(text)
    if state == "rejected":
        return ["PSE payment was not approved"]
    if state == "pending":
        return ["PSE payment is pending"]
    return []


def clean_entity(name: str | None) -> str:
    """The paid entity without NIT and legal suffix («Aguas de Cartagena S.A. E.S.P. NIT 890.399.011-3» -> «AGUAS DE CARTAGENA»), normalized."""
    text = _NIT.sub("", name or "").strip(" ,.;:-")
    for _ in range(3):
        trimmed = _LEGAL_TAIL.sub("", text).strip(" ,.;:-")
        if trimmed == text or not trimmed:
            break
        text = trimmed
    return normalize_merchant(text)


def tag(item, text: str | None) -> bool:
    """Mark a withdrawal that is a PSE payment: tag ``pse`` and a clean paid entity. True when the item was marked."""
    if item.transaction_type != "withdrawal" or not is_pse(text):
        return False
    if TAG not in (item.tags or []):
        item.tags = [*(item.tags or []), TAG]
    if item.merchant and (entity := clean_entity(item.merchant)):
        item.merchant = entity
    return True


def is_pse_item(item) -> bool:
    return TAG in (item.tags or []) or (item.description or "").startswith(DESCRIPTION_PREFIX)


def has_entity(merchant: str | None) -> bool:
    """The merchant names who was paid (not empty and not just «PSE»)."""
    name = normalize_merchant(merchant or "")
    return bool(name) and name not in GENERIC_ENTITIES


async def supersedes(processor, item, duplicate) -> bool:
    """True when ``duplicate`` (an earlier registered movement of the same payment) was voided because ``item`` is better.

    Better means: ``item`` is a PSE payment that names the paid entity and ``duplicate`` does not. A failure to void
    keeps the earlier movement (``item`` then becomes the duplicate as usual).
    """
    if not (is_pse_item(item) or is_pse_item(duplicate)) or not has_entity(item.merchant) or has_entity(duplicate.merchant):
        return False
    try:
        await processor.review(duplicate, "void", principal=None)
    except Exception as exc:
        logger.warning("pse_supersede_failed", error_type=type(exc).__name__)
        await processor.session.rollback()
        return False
    return True
