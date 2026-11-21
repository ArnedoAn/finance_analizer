"""Merchant → category rules applied during extraction (Phase 5 A6, contract section 7).

``apply`` is called by ``Processor._extract`` after merchant normalization and before the
review decision. It may set ``item.category`` from a matching rule; it never changes status.
"""
from sqlalchemy import select, update

from app.db.models_rules import Merchant, MerchantItem, Rule
from app.parsers import normalize_merchant

MATCHES = ("merchant_exact", "merchant_contains", "text_contains")
SOURCES = ("manual", "learned")
_PRIORITY = {match: rank for rank, match in enumerate(MATCHES)}


def _squash(value: str) -> str:
    return " ".join(value.split())


def merchant_name(value: str | None) -> str:
    """Canonical merchant key shared by merchants and merchant rules (empty if unusable)."""
    name = normalize_merchant(value or "") or _squash((value or "").upper())
    return name if len(name) <= 255 else ""


def normalize_pattern(match: str, pattern: str) -> str:
    if match == "merchant_exact":
        return merchant_name(pattern)
    if match == "merchant_contains":
        return _squash(pattern.upper())[:255]
    return _squash(pattern.casefold())[:255]


async def _count_item(session, item, name: str) -> Merchant:
    """Upsert the merchant and count ``item`` once, however many times it is reprocessed.

    The counter is incremented in SQL (``count = count + 1``) so concurrent workers never lose an update.
    """
    merchant = await session.scalar(select(Merchant).where(Merchant.name == name))
    if merchant is None:
        merchant = Merchant(name=name, count=0)
        session.add(merchant)
        await session.flush()
    if await session.get(MerchantItem, item.id) is None:
        session.add(MerchantItem(item_id=item.id, merchant_id=merchant.id))
        await session.flush()
        await session.execute(
            update(Merchant).where(Merchant.id == merchant.id).values(count=Merchant.count + 1)
            .execution_options(synchronize_session=False)
        )
        await session.refresh(merchant, ["count"])
    if merchant.last_seen_at is None or item.occurred_at > merchant.last_seen_at:
        merchant.last_seen_at = item.occurred_at
    await session.flush()
    return merchant


def _matches(rule: Rule, name: str, text: str) -> bool:
    if rule.match == "merchant_exact":
        return bool(name) and rule.pattern == name
    if rule.match == "merchant_contains":
        return bool(name) and rule.pattern in name
    return rule.pattern in text


async def _first_rule(session, name: str, text: str) -> Rule | None:
    text = _squash(text.casefold())
    rules = [rule for rule in (await session.scalars(select(Rule))).all() if rule.pattern and _matches(rule, name, text)]
    # Priority: exact > contains > text; then manual before learned; then most recent.
    rules.sort(key=lambda r: (_PRIORITY.get(r.match, len(MATCHES)), r.source != "manual", -r.created_at.timestamp(), -r.id))
    return rules[0] if rules else None


async def apply(session, item) -> None:
    """Count the merchant once per item; if the item has no category, set it from the best
    matching rule, else from the merchant's default category. Never touches status or other fields."""
    name = merchant_name(item.merchant)
    merchant = await _count_item(session, item, name) if name else None
    if item.category:
        return
    # text_contains looks at the subject/title and the original text, not only the subject.
    rule = await _first_rule(session, name, f"{item.subject_or_title or ''} {item.raw_text or ''}")
    if rule is not None:
        item.category = rule.category
        await session.execute(
            update(Rule).where(Rule.id == rule.id).values(hits=Rule.hits + 1).execution_options(synchronize_session=False)
        )
        await session.refresh(rule, ["hits"])
    elif merchant is not None and merchant.default_category:
        item.category = merchant.default_category


async def create_rule(session, *, match, pattern, category, merchant=None, source="manual") -> Rule:
    """Create a rule, or update the category of the existing rule with the same match and pattern.

    Does not commit. Raises ValueError for an unknown ``match``/``source`` or an empty pattern or category.
    """
    if match not in MATCHES:
        raise ValueError("Unknown rule match: " + str(match))
    if source not in SOURCES:
        raise ValueError("Unknown rule source: " + str(source))
    pattern = normalize_pattern(match, pattern or "")
    category = (category or "").strip()
    if not pattern or not category:
        raise ValueError("Rules require a pattern and a category")
    rule = await session.scalar(select(Rule).where(Rule.match == match, Rule.pattern == pattern))
    if rule is None:
        rule = Rule(match=match, pattern=pattern, category=category, merchant=merchant, source=source, hits=0)
        session.add(rule)
    else:
        rule.category = category
        rule.merchant = merchant or rule.merchant
        if source == "manual":
            rule.source = "manual"
    await session.flush()
    return rule


def rule_data(rule) -> dict:
    return {
        "id": rule.id, "match": rule.match, "pattern": rule.pattern, "category": rule.category,
        "merchant": rule.merchant, "source": rule.source, "hits": rule.hits, "created_at": rule.created_at,
    }


def merchant_data(merchant) -> dict:
    return {
        "id": merchant.id, "name": merchant.name, "default_category": merchant.default_category,
        "count": merchant.count, "last_seen_at": merchant.last_seen_at,
    }
