"""Inbox and review (contract sections 3 and 14)."""
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from app.api.dependencies import READ, REVIEW, ReadPrincipal, ReviewPrincipal
from app.api.routers.common import DB, Service, get_item, item_data
from app.db.repositories import InboxRepository
from app.parsers import normalize_merchant
from app.services import audit, flags

router = APIRouter()

Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class ReviewCorrections(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    merchant: str | None = Field(default=None, min_length=1, max_length=100)
    category: str | None = None
    firefly_account: str | None = None
    destination_account: str | None = None
    currency: Literal["COP", "USD"] | None = None
    transaction_type: Literal["withdrawal", "deposit", "transfer", "refund"] | None = None
    occurred_at: datetime | None = None
    description: str | None = Field(default=None, max_length=500)
    tags: list[Tag] | None = Field(default=None, max_length=10)


class ClassifyBody(ReviewCorrections):
    create_rule: bool = False


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suspicious: bool = False


async def _review(db, service, item, action, principal, corrections=None):
    """Run a review action as ``principal`` (audited by the processor) and commit."""
    try:
        result = await service.review(item, action, corrections, principal=principal)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    await db.commit()
    return result if result is not None else item


@router.get("/inbox", dependencies=[READ])
async def inbox(
    db: DB,
    status: str | None = None,
    source: str | None = None,
    method: str | None = None,
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
    review_reason: str | None = None,
    q: str | None = None,
    tag: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    filters = {"since": since, "until": until, "review_reason": review_reason, "q": q, "tag": tag}
    repository = InboxRepository(db)
    rows = await repository.list(status, source, method, limit, offset, **filters)
    return {"items": [item_data(row) for row in rows], "total": await repository.count(status, source, method, **filters)}


@router.get("/inbox/unparsed", dependencies=[READ])
async def inbox_unparsed(db: DB, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    repository = InboxRepository(db)
    rows = await repository.list(limit=limit, offset=offset, unparsed=True)
    return {"items": [item_data(row, redacted_raw=True) for row in rows], "total": await repository.count(unparsed=True)}


@router.post("/inbox/retry-failed", dependencies=[REVIEW])
async def retry_failed(db: DB, service: Service):
    repository = InboxRepository(db)
    item_ids = []
    offset = 0
    while True:
        rows = await repository.list(status="failed", limit=500, offset=offset)
        item_ids.extend(row.id for row in rows)
        if len(rows) < 500:
            break
        offset += len(rows)
    requeued = await repository.requeue(failed_only=True)
    await db.commit()
    for item_id in item_ids:
        item = await repository.get(item_id)
        if item is not None and item.status == "pending":
            await service.process(item)
    await db.commit()
    return {"requeued": requeued}


@router.get("/inbox/{item_id}")
async def inbox_detail(item_id: int, db: DB, principal: ReadPrincipal, include_raw: bool = False):
    if include_raw and not principal.allows("review"):
        raise HTTPException(403, "include_raw requires review scope")
    return item_data(await get_item(db, item_id), redacted_raw=True, include_raw=include_raw)


@router.post("/inbox/{item_id}/reprocess", dependencies=[REVIEW])
async def retry(item_id: int, db: DB, service: Service):
    item = await get_item(db, item_id)
    requeued = await InboxRepository(db).requeue(item_id)
    if not requeued:
        raise HTTPException(409, "Inbox item cannot be reprocessed in its current state")
    await db.commit()
    await service.process(item)
    await db.commit()
    return {"requeued": requeued, "item": item_data(item)}


@router.post("/inbox/{item_id}/approve")
async def approve(item_id: int, db: DB, service: Service, principal: ReviewPrincipal, corrections: ReviewCorrections | None = Body(default=None)):
    item = await get_item(db, item_id)
    return item_data(await _review(db, service, item, "approve", principal, corrections.model_dump(exclude_unset=True) if corrections else None))


@router.post("/inbox/{item_id}/classify")
async def classify(item_id: int, body: ClassifyBody, db: DB, service: Service, principal: ReviewPrincipal):
    """Classify by hand (an approve with corrections) and optionally learn a merchant rule."""
    item = await get_item(db, item_id)
    corrections = body.model_dump(exclude_unset=True, exclude={"create_rule"})
    if body.create_rule and not (normalize_merchant(corrections.get("merchant") or item.merchant or "") and (corrections.get("category") or item.category)):
        raise HTTPException(422, "A rule needs a clean merchant and a category")
    item = await _review(db, service, item, "approve", principal, corrections)
    rule_payload = None
    if body.create_rule:
        from app.services import rules  # lazy: the rules catalog is a separate module

        try:
            rule = await rules.create_rule(db, match="merchant_exact", pattern=normalize_merchant(item.merchant or ""), category=item.category, source="learned")
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        await audit.record(db, action="rule.create", target_type="rule", target_id=rule.id, detail={"match": rule.match, "pattern": rule.pattern, "category": rule.category, "source": "learned", "item_id": item.id}, principal=principal)
        await db.commit()
        rule_payload = rules.rule_data(rule)
    return {"item": item_data(item), "rule": rule_payload}


@router.post("/inbox/{item_id}/reject")
async def reject(item_id: int, db: DB, service: Service, principal: ReviewPrincipal, body: RejectBody | None = Body(default=None)):
    item = await _review(db, service, await get_item(db, item_id), "reject", principal)
    if body is not None and body.suspicious:
        await flags.mark_suspicious(db, item, principal)
        await db.commit()
    return item_data(item)


@router.patch("/inbox/{item_id}")
async def update_item(item_id: int, corrections: ReviewCorrections, db: DB, service: Service, principal: ReviewPrincipal):
    item = await get_item(db, item_id)
    return item_data(await _review(db, service, item, "edit", principal, corrections.model_dump(exclude_unset=True)))


@router.post("/inbox/{item_id}/void")
async def void_item(item_id: int, db: DB, service: Service, principal: ReviewPrincipal):
    return item_data(await _review(db, service, await get_item(db, item_id), "void", principal))
