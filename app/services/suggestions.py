"""Likely categories for a merchant and the conversational category question (contract section 13).

Order: learned/manual rules, then categories most used for the same merchant in created
items, then the most used categories overall. Results never repeat a category.
"""
from decimal import Decimal

from sqlalchemy import func, select

from app.db.models import InboxItem

try:
    from app.db.models_rules import Rule
except ImportError:  # rules table not available: continue with frequency only
    Rule = None


def format_amount(amount) -> str:
    """Colombian format without currency symbol: 12.000 or 12.000,50."""
    value = Decimal(amount).quantize(Decimal("0.01"))
    whole, cents = f"{value:,.2f}".split(".")
    whole = whole.replace(",", ".")
    return whole if cents == "00" else f"{whole},{cents}"


def _rule_matches(rule, merchant: str, text: str) -> bool:
    pattern = (rule.pattern or "").casefold()
    if not pattern:
        return False
    if rule.match == "merchant_exact":
        return merchant == pattern
    if rule.match == "merchant_contains":
        return pattern in merchant
    if rule.match == "text_contains":
        return pattern in text
    return False


async def _frequent(session, merchant=None, exclude_id=None) -> list[str]:
    count = func.count().label("uses")
    query = select(InboxItem.category, count).where(InboxItem.status == "created", InboxItem.category.is_not(None), InboxItem.category != "")
    if merchant:
        query = query.where(func.lower(InboxItem.merchant) == merchant.casefold())
    if exclude_id is not None:
        query = query.where(InboxItem.id != exclude_id)
    query = query.group_by(InboxItem.category).order_by(count.desc(), InboxItem.category).limit(20)
    return [row[0] for row in (await session.execute(query)).all()]


async def suggest_for_merchant(session, merchant, *, text="", exclude_id=None, limit=3, overall_fallback=True) -> list[str]:
    merchant_key = (merchant or "").casefold()
    text_key = (text or "").casefold()
    ordered: list[str] = []
    if Rule is not None and (merchant_key or text_key):
        rules = (await session.scalars(select(Rule).order_by(Rule.hits.desc(), Rule.id))).all()
        ordered.extend(rule.category for rule in rules if rule.category and _rule_matches(rule, merchant_key, text_key))
    if merchant_key:
        ordered.extend(await _frequent(session, merchant, exclude_id))
    if overall_fallback:
        ordered.extend(await _frequent(session, None, exclude_id))
    result, seen = [], set()
    for category in ordered:
        if category.casefold() not in seen:
            seen.add(category.casefold())
            result.append(category)
    return result[:limit]


async def suggest(session, item, limit=3) -> list[str]:
    text = "\n".join(part for part in (item.subject_or_title, item.raw_text) if part)
    return await suggest_for_merchant(session, item.merchant, text=text, exclude_id=item.id, limit=limit)


async def question_for(session, item) -> dict | None:
    if item.status != "needs_review" or not item.amount or not item.merchant or not item.asset_account or item.category:
        return None
    options = await suggest(session, item, 3)
    if not options:
        return None
    return {
        "question": f"¿Este cobro de {item.merchant} por ${format_amount(item.amount)} es {options[0]}?",
        "options": options,
    }
