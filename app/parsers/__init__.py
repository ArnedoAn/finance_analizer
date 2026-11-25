"""Bank-specific deterministic parsing; unknown formats fall through to the LLM.

Only ``parse`` is the public extraction entrypoint. Conflicting or ambiguous
messages produce a review decision, never an arbitrary successful transaction.
"""

import re
from dataclasses import replace

from . import bancolombia, daviplata, davivienda, lulo, nequi, nu
from .common import (
    DECLINED_REASON,
    DECLINED_TEMPLATE,
    NON_TRANSACTION,
    REFUND,
    candidates,
    canonical_mask,
    fold,
    is_declined,
    is_masked_counterparty,
    is_placeholder_merchant,
    match_masked_counterparty,
    normalize_merchant,
    parse_amount,
)
from .types import Extraction, Kind

__all__ = [
    "DECLINED_REASON", "DECLINED_TEMPLATE", "Extraction", "Kind", "canonical_mask", "is_declined", "is_masked_counterparty",
    "is_placeholder_merchant", "match_masked_counterparty", "normalize_merchant", "parse", "parse_amount",
]

_BANKS = {
    "bancolombia": bancolombia, "nequi": nequi, "daviplata": daviplata,
    "davivienda": davivienda, "lulo": lulo, "nu": nu,
}


def parse(text: str, subject: str = "", sender: str = "") -> Extraction | None:
    combined = subject + "\n" + text if subject else text
    normalized = fold(combined)
    identity = fold(sender + "\n" + subject + "\n" + text)
    identities = [(match.start(), name) for name in _BANKS
                  if (match := re.search(rf"\b{name}\b", identity))]
    bank = min(identities)[1] if identities else None
    if re.search(r"\b(?:87400|890099)\b", sender):
        bank = "bancolombia"
    modules = [(bank, _BANKS[bank])] if bank else list(_BANKS.items())
    found = [candidate for name, module in modules
             for candidate in candidates(combined, name, module.TEMPLATES)]
    # Nested templates ('devolucion de compra' / 'compra') describe one event.
    events = []
    for candidate in sorted(found, key=lambda item: (item.start, -item.end)):
        if not events or candidate.start >= events[-1].end:
            events.append(candidate)
    first_sentence = re.split(r"[.!?;\n](?:\s|$)", text, maxsplit=1)[0]
    ignore_term = NON_TRANSACTION.search(fold(subject)) or NON_TRANSACTION.search(fold(first_sentence))
    if is_declined(combined):
        return Extraction(kind="ignore", template_id=DECLINED_TEMPLATE, reason=DECLINED_REASON)
    if bank == "davivienda" and (notice := davivienda.notice(combined)) is not None:
        # A labelled movement notice carries its own vocabulary («Clase de Movimiento: Descuento ...»), so a subject word
        # such as «descuento» does not make it non-transactional.
        return notice
    if len(events) > 1:
        return replace(events[0].extraction, kind="review", template_id="review.mixed",
                       reason="Multiple transaction events in one message")
    if ignore_term:
        if events:
            return replace(events[0].extraction, kind="review", template_id="review.ignore_conflict",
                           reason="Transaction template conflicts with non-transaction introduction")
        return Extraction(kind="ignore", template_id="ignore.non_transaction", reason="Non-transaction introduction")
    if not events:
        return None
    extraction = events[0].extraction
    if REFUND.search(normalized) and extraction.kind != "review":
        return replace(extraction, kind="refund", template_id=f"{bank or 'generic'}.refund")
    return extraction
