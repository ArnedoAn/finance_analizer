"""Merchant → category rules applied during extraction (Phase 5 A6, contract section 7).

``apply`` is called by ``Processor._extract`` after merchant normalization and before the
review decision. It may set ``item.category`` from a matching rule; it never changes status.
"""
from sqlalchemy import select, update

from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.models import MerchantCorrection
from app.db.models_rules import Merchant, MerchantItem, Rule
from app.db.repositories import InboxRepository
from app.parsers import normalize_merchant
from app.services import audit

logger = get_logger(__name__)


MAX_TAGS = 10
MAX_TAG = 40
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
    rules = [rule for rule in (await session.scalars(select(Rule).where(Rule.active.is_(True)))).all() if rule.pattern and _matches(rule, name, text)]
    # Priority: exact > contains > text; then manual before learned; then most recent.
    rules.sort(key=lambda r: (_PRIORITY.get(r.match, len(MATCHES)), r.source != "manual", -r.created_at.timestamp(), -r.id))
    return rules[0] if rules else None


async def apply(session, item) -> None:
    """Count the merchant once per item; if the item has no category, set it from the best
    matching rule (adding the rule's tags to the item's), else from the merchant's default category.
    Never touches status or other fields."""
    name = merchant_name(item.merchant)
    merchant = await _count_item(session, item, name) if name else None
    if item.category:
        return
    # text_contains looks at the subject/title and the original text, not only the subject.
    rule = await _first_rule(session, name, f"{item.subject_or_title or ''} {item.raw_text or ''}")
    if rule is not None:
        item.category = rule.category
        if rule.tags:
            item.tags = [*(item.tags or []), *(tag for tag in rule.tags if tag not in (item.tags or []))]
        await session.execute(
            update(Rule).where(Rule.id == rule.id).values(hits=Rule.hits + 1).execution_options(synchronize_session=False)
        )
        await session.refresh(rule, ["hits"])
    elif merchant is not None and merchant.default_category:
        item.category = merchant.default_category


def clean_tags(values) -> list[str] | None:
    """Normalize rule tags like ``ledger._clean_tags`` (strip, drop repeats, at most 10 of 1-40 characters); ``None`` stays ``None``.

    Raises ValueError.
    """
    if values is None:
        return None
    if not isinstance(values, list | tuple) or len(values) > MAX_TAGS:
        raise ValueError(f"Rule tags must be a list of at most {MAX_TAGS} tags")
    tags = []
    for tag in values:
        tag = tag.strip() if isinstance(tag, str) else ""
        if not tag or len(tag) > MAX_TAG:
            raise ValueError(f"Each rule tag must have between 1 and {MAX_TAG} characters")
        if tag not in tags:
            tags.append(tag)
    return tags


async def create_rule(session, *, match, pattern, category, merchant=None, source="manual", tags=None) -> Rule:
    """Create a rule, or update the category of the existing rule with the same match and pattern.

    ``tags`` (``None`` keeps the existing rule's tags; a list replaces them). Does not commit.
    Raises ValueError for an unknown ``match``/``source``, invalid tags, or an empty pattern or category.
    """
    if match not in MATCHES:
        raise ValueError("Unknown rule match: " + str(match))
    if source not in SOURCES:
        raise ValueError("Unknown rule source: " + str(source))
    pattern = normalize_pattern(match, pattern or "")
    category = (category or "").strip()
    if not pattern or not category:
        raise ValueError("Rules require a pattern and a category")
    tags = clean_tags(tags)
    rule = await session.scalar(select(Rule).where(Rule.match == match, Rule.pattern == pattern))
    if rule is None:
        rule = Rule(match=match, pattern=pattern, category=category, merchant=merchant, source=source, hits=0, tags=tags or None)
        session.add(rule)
    else:
        rule.category = category
        rule.active = True
        rule.merchant = merchant or rule.merchant
        if tags is not None:
            rule.tags = tags or None
        if source == "manual":
            rule.source = "manual"
    await session.flush()
    return rule


def rule_data(rule) -> dict:
    return {
        "id": rule.id, "match": rule.match, "pattern": rule.pattern, "category": rule.category,
        "merchant": rule.merchant, "source": rule.source, "hits": rule.hits, "created_at": rule.created_at,
        "tags": list(rule.tags or []),
    }


async def add_for_owner(session, processor, *, match, pattern, category, merchant, principal, tags=None) -> dict:
    """A rule the owner chose (``POST /rules``): the category must exist in Firefly (its spelling is stored). Audits and commits.

    Raises ValueError("Unknown category") or the ``create_rule`` errors.
    """
    wanted = (category or "").strip().casefold()
    spelled = next((name for name in await processor.categories() if name.casefold() == wanted), None)
    if spelled is None:
        raise ValueError("Unknown category")
    rule = await create_rule(session, match=match, pattern=pattern, category=spelled, merchant=merchant, tags=tags)
    data = rule_data(rule)
    await audit.record(
        session, action="rule.create", target_type="rule", target_id=rule.id,
        detail={"match": rule.match, "pattern": rule.pattern, "category": spelled, "tags": data["tags"]}, principal=principal,
    )
    await session.commit()
    return data


async def remove_for_owner(session, rule_id: int, principal) -> dict | None:
    """Delete a rule (``DELETE /rules/{id}``); its data, or None when it does not exist. Audits and commits."""
    rule = await session.get(Rule, rule_id)
    if rule is None:
        return None
    detail = {"match": rule.match, "pattern": rule.pattern, "category": rule.category}
    data = rule_data(rule)
    await session.delete(rule)
    await audit.record(session, action="rule.delete", target_type="rule", target_id=rule_id, detail=detail, principal=principal)
    await session.commit()
    return data


def merchant_data(merchant) -> dict:
    return {
        "id": merchant.id, "name": merchant.name, "default_category": merchant.default_category,
        "count": merchant.count, "last_seen_at": merchant.last_seen_at,
    }


LEARN_STREAK = 2  # same category chosen this many consecutive times for one merchant creates the rule


async def learn_from_correction(session, *, merchant, category, previous_category=None, item_id=None, principal=None) -> dict:
    """The owner (or an applied AI suggestion) set ``category`` on an item of ``merchant``; learn from it. Does not commit.

    * An active learned ``merchant_exact`` rule with another category is contradicted: it is switched off
      (``rule.deactivate`` audit); the correction counts as the first of a new streak.
    * Per normalized merchant the consecutive corrections to the same category are counted in ``merchant_corrections``;
      at ``LEARN_STREAK`` an active rule ``merchant_exact`` with source ``learned`` is created or reactivated
      (``rule.learn`` audit).
    * Manual rules are the owner's explicit decision: while one is active nothing is learned for that merchant.

    Returns ``{"learned": Rule | None, "deactivated": Rule | None}``.
    """
    result = {"learned": None, "deactivated": None}
    name = merchant_name(merchant)
    category = (category or "").strip()
    if not name or not category or (previous_category or "").strip().casefold() == category.casefold():
        return result  # nothing usable, or the owner only confirmed what was already there
    rule = await session.scalar(select(Rule).where(Rule.match == "merchant_exact", Rule.pattern == name))
    if rule is not None and rule.active and rule.source == "manual":
        return result
    if rule is not None and rule.active and rule.category.casefold() != category.casefold():
        rule.active = False
        await audit.record(
            session, action="rule.deactivate", target_type="rule", target_id=rule.id, principal=principal,
            detail={"match": rule.match, "pattern": rule.pattern, "category": rule.category, "contradicted_by": category, "source": rule.source, "item_id": item_id},
        )
        result["deactivated"] = rule
    streak = await session.scalar(select(MerchantCorrection).where(MerchantCorrection.merchant == name))
    if streak is None:
        streak = MerchantCorrection(merchant=name, category=category, streak=1)
        session.add(streak)
    elif streak.category.casefold() == category.casefold():
        streak.streak += 1
    else:
        streak.category, streak.streak = category, 1
    streak.updated_at = now_utc()
    if streak.streak >= LEARN_STREAK and (rule is None or not rule.active):
        rule = await create_rule(session, match="merchant_exact", pattern=name, category=category, merchant=name, source="learned")
        await audit.record(
            session, action="rule.learn", target_type="rule", target_id=rule.id, principal=principal,
            detail={"match": rule.match, "pattern": rule.pattern, "category": rule.category, "source": "learned", "item_id": item_id},
        )
        streak.streak = 0
        result["learned"] = rule
    await session.flush()
    return result


async def learn_item(session, item_id: int, previous_category: str | None, principal=None) -> None:
    """``learn_from_correction`` for the item's current merchant and category, then commit.

    Best-effort: callers run it after the review the owner asked for has been committed, and a failure here
    (logged by class only) must never turn that finished review into an error.
    """
    try:
        item = await InboxRepository(session).get(item_id)
        if item is None or not item.category:
            return
        async with session.begin_nested():
            await learn_from_correction(session, merchant=item.merchant, category=item.category, previous_category=previous_category, item_id=item_id, principal=principal)
        await session.commit()
    except Exception as exc:
        logger.error("rule_learning_failed", item_id=item_id, error_type=type(exc).__name__)
