"""Firefly categories the owner explicitly asked for.

One implementation behind ``POST /categories`` and the assistant's category proposals (``create_category``,
``rename_category``, ``delete_category``), so both validate the name, skip a category that already exists (any case
or accents), invalidate the processor's cache and leave the same audit entry. Failures the owner can fix are
``ValueError`` with a clear message; a Firefly failure propagates and nothing is recorded.
"""
import unicodedata
from typing import Annotated

from pydantic import AfterValidator, StringConstraints
from sqlalchemy import func, select, update

from app.db.models import InboxItem
from app.db.models_rules import Merchant, Rule
from app.parsers.common import fold
from app.services import audit


def _clean(value: str) -> str:
    if "$" in value or any(unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in value):
        raise ValueError("must not contain line breaks, '$' or control characters")
    return value


CategoryName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64), AfterValidator(_clean)]


def find(names, wanted: str) -> str | None:
    """The Firefly spelling in ``names`` that equals ``wanted`` ignoring case and accents, or None."""
    target = fold(wanted)
    return next((name for name in names if fold(name) == target), None)


async def create(session, processor, name: str, principal) -> tuple[str, bool]:
    """Create the category in Firefly unless it exists; returns ``(name as Firefly spells it, created)``. Commits."""
    existing = find(await processor.categories(), name)
    if existing is not None:
        return existing, False
    created = await processor.firefly.create_category(name)
    processor.invalidate_categories()
    await audit.record(session, action="category.create", target_type="category", target_id=created, detail={"name": created}, principal=principal)
    await session.commit()
    return created, True


async def _existing(processor, name: str) -> str:
    current = find(await processor.categories(), name)
    if current is None:
        raise ValueError(f"Unknown category: {name}")
    return current


async def rename(session, processor, name: str, new_name: str, principal) -> str:
    """Rename a category in Firefly and in the analyzer's own rows that name it; returns the new spelling. Commits."""
    current = await _existing(processor, name)
    clash = find(await processor.categories(), new_name)
    if clash is not None and fold(clash) != fold(current):
        raise ValueError(f"A category named «{clash}» already exists")
    stored = await processor.firefly.rename_category(current, new_name)
    processor.invalidate_categories()
    await session.execute(update(Rule).where(Rule.category == current).values(category=stored))
    await session.execute(update(Merchant).where(Merchant.default_category == current).values(default_category=stored))
    await session.execute(update(InboxItem).where(InboxItem.category == current).values(category=stored))
    await audit.record(session, action="category.rename", target_type="category", target_id=stored, detail={"from": current, "to": stored}, principal=principal)
    await session.commit()
    return stored


async def delete(session, processor, name: str, principal) -> str:
    """Delete a category from Firefly (its transactions keep no category); returns the deleted name. Commits.

    A category an active rule still assigns is refused: the owner deletes or changes those rules first.
    """
    current = await _existing(processor, name)
    rules_using = await session.scalar(select(func.count()).select_from(Rule).where(Rule.category == current, Rule.active.is_(True)))
    if rules_using:
        raise ValueError(f"{rules_using} active rule(s) still assign «{current}»; delete or change them first")
    await processor.firefly.delete_category(current)
    processor.invalidate_categories()
    await audit.record(session, action="category.delete", target_type="category", target_id=current, detail={"name": current}, principal=principal)
    await session.commit()
    return current
