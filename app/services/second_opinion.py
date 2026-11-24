"""Second opinion on an extraction: another model reads the same REDACTED text and extracts again.

``Processor._extract`` asks for it after every LLM extraction and after a parser extraction whose amount is above
``min_amount_cop`` (setting ``second_opinion`` of ``GET/PUT /settings/ai-features``). The two extractions must agree on the
amount, the type, the own account they resolve to and the normalized merchant: then the item is ``verified`` and the
auto-approval policy lets it through up to ``verified_cap`` (default twice the normal cap); otherwise it goes to review with
the reason «segunda opinión en desacuerdo: …». The call is automatic and brakable (``ai_budget.allow`` kind
``second_opinion``): when it is braked, unavailable or fails there is no verdict and the normal cap applies. It is part of
processing a message, so it is never queued for the valley.

Functions flush but never commit; the caller owns the transaction.
"""
from decimal import Decimal

from app.core.logging import get_logger
from app.parsers.common import fold
from app.security.redaction import redact
from app.services import ai_budget, ai_features

logger = get_logger(__name__)

KIND = "second_opinion"
DISAGREE_PREFIX = "segunda opinión en desacuerdo: "
_VALUE_CHARS = 40


async def config(session) -> dict:
    """``{"enabled", "model", "min_amount_cop", "verified_max_amount_cop"}`` (strings for money, None = default)."""
    return (await ai_features.get(session))["second_opinion"]


def verified_cap(settings: dict, policy_cap: Decimal) -> Decimal:
    """Highest amount a verified extraction may auto-approve: the configured cap or twice ``policy_cap``, never below it."""
    configured = settings.get("verified_max_amount_cop")
    cap = Decimal(configured) if configured else policy_cap * 2
    return max(cap, policy_cap)


def wanted(settings: dict, method: str | None, amount: Decimal | None) -> bool:
    """LLM extractions always get a second opinion; parser ones only above ``min_amount_cop``."""
    if not settings["enabled"] or amount is None:
        return False
    return method == "llm" or amount > Decimal(settings["min_amount_cop"])


async def ask(session, llm, prompt_text: str, cycle_id: str, model: str):
    """The second model's ``Extraction``, or None when the budget guard refuses or the model fails. Records the call."""
    if not await ai_budget.allow(session, KIND, 1):
        return None
    usage: dict = {}
    try:
        return await llm.analyze(prompt_text, cycle_id=cycle_id, usage=usage, model=model)
    except Exception as exc:  # no verdict is not a failure of the message: it keeps the normal cap
        logger.warning("second_opinion_failed", error_type=type(exc).__name__)
        return None
    finally:
        try:
            await ai_budget.record(session, KIND, usage.get("calls", 1), usage)
        except Exception as exc:  # accounting must never fail the work
            logger.warning("second_opinion_record_failed", error_type=type(exc).__name__)


def party(extraction, clean) -> str:
    """Folded name of the other side: the merchant, or for a transfer the counterparty. ``clean`` is the processor's merchant cleaner."""
    name = ""
    if extraction.kind == "transfer" and extraction.counterparty_hint:
        name = clean(extraction.counterparty_hint)
    return fold(name or clean(extraction.merchant) or clean(extraction.counterparty_hint) or "")


def _shown(value) -> str:
    return " ".join(redact(str(value) if value else "ninguno").split())[:_VALUE_CHARS]


def compare(first, second, *, clean, first_account: str | None, second_account: str | None) -> dict:
    """``{"status": "verified" | "disagree", "fields": [...], "reason": str | None}`` for two extractions of the same message."""
    fields: list[str] = []
    parts: list[str] = []

    def differ(name: str, left, right):
        fields.append(name)
        parts.append(f"{name} ({_shown(left)} ≠ {_shown(right)})")

    if first.amount != second.amount:
        differ("monto", first.amount, second.amount)
    if first.kind != second.kind:
        differ("tipo", first.kind, second.kind)
    if (first_account or "") != (second_account or ""):
        differ("cuenta", first_account, second_account)
    if party(first, clean) != party(second, clean):
        differ("comercio", clean(first.merchant) or clean(first.counterparty_hint), clean(second.merchant) or clean(second.counterparty_hint))
    if not fields:
        return {"status": "verified", "fields": [], "reason": None}
    return {"status": "disagree", "fields": fields, "reason": (DISAGREE_PREFIX + ", ".join(parts))[:300]}
