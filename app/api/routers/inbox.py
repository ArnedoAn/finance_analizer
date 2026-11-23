"""Inbox and review (contract sections 3 and 14)."""
import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.api.dependencies import READ, ReadPrincipal, ReviewPrincipal, get_firefly, get_llm
from app.api.routers.common import DB, Service, get_item, item_data
from app.core.logging import get_logger
from app.db.database import get_db_session
from app.db.repositories import InboxRepository
from app.parsers import normalize_merchant
from app.services import ai_review, audit, flags, rules
from app.services.processor import Processor

logger = get_logger(__name__)

router = APIRouter()

# One re-evaluation at a time. Per process: production runs a single uvicorn worker; a restart loses only this handle,
# never items, and the request can simply be repeated.
_resolve_task: asyncio.Task | None = None
# Same for the owner-requested AI suggestion run (POST /inbox/ai-suggest).
_suggest_task: asyncio.Task | None = None

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
    """Run a review action as ``principal`` (audited by the processor) and commit.

    A category the owner sets here feeds the rules that learn by themselves (``rules.learn_from_correction``).
    """
    previous_category, item_id = item.category, item.id
    try:
        result = await service.review(item, action, corrections, principal=principal)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    await db.commit()
    if action in {"approve", "edit"} and corrections and corrections.get("category"):
        await rules.learn_item(db, item_id, previous_category, principal)
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
    source_ref: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    filters = {"since": since, "until": until, "review_reason": review_reason, "q": q, "tag": tag, "source_ref": source_ref}
    repository = InboxRepository(db)
    rows = await repository.list(status, source, method, limit, offset, **filters)
    return {"items": [item_data(row) for row in rows], "total": await repository.count(status, source, method, **filters)}


@router.get("/inbox/unparsed", dependencies=[READ])
async def inbox_unparsed(db: DB, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    repository = InboxRepository(db)
    rows = await repository.list(limit=limit, offset=offset, unparsed=True)
    return {"items": [item_data(row, redacted_raw=True) for row in rows], "total": await repository.count(unparsed=True)}


@router.post("/inbox/retry-failed")
async def retry_failed(db: DB, service: Service, principal: ReviewPrincipal):
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
    await audit.record(db, action="inbox.retry_failed", target_type="inbox", target_id="failed", detail={"requeued": requeued}, principal=principal)
    await db.commit()
    for item_id in item_ids:
        item = await repository.get(item_id)
        if item is not None and item.status == "pending":
            await service.process(item)
    await db.commit()
    return {"requeued": requeued}


def _resolve_running() -> bool:
    return _resolve_task is not None and not _resolve_task.done()


async def _run_auto_resolve(ids: list[int]) -> None:
    try:
        async with get_db_session() as db:
            counts = await Processor(db, get_firefly(), get_llm()).reevaluate_ids(ids)
        logger.info("auto_resolve_done", processed=sum(counts.values()), created=counts.get("created", 0))
    except Exception as exc:
        logger.error("auto_resolve_failed", error_type=type(exc).__name__)


@router.post("/inbox/auto-resolve", status_code=202)
async def auto_resolve(db: DB, service: Service, principal: ReviewPrincipal):
    """Decide again, in the background and with the current policy, every automatic ``needs_review`` item."""
    global _resolve_task
    busy = HTTPException(409, detail={"detail": "Ya hay una reevaluación de la bandeja en curso.", "code": "auto_resolve_running"})
    if _resolve_running():
        raise busy
    ids = await service.reevaluation_ids()
    if not ids:
        return {"queued": 0}
    await audit.record(db, action="inbox.auto_resolve", target_type="inbox", target_id="needs_review", detail={"queued": len(ids)}, principal=principal)
    await db.commit()
    if _resolve_running():  # another request started one while this one was committing
        raise busy
    _resolve_task = asyncio.create_task(_run_auto_resolve(ids))
    return {"queued": len(ids)}


class ApplySuggestions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[int] = Field(default_factory=list, max_length=500)
    all_above: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _something_to_apply(self):
        if not self.ids and self.all_above is None:
            raise ValueError("Indica ids o all_above")
        return self


def _suggestion_data(suggestion, item) -> dict:
    return {
        "id": suggestion.id, "status": suggestion.status, "item": item_data(item),
        "proposal": {
            "action": suggestion.action, "firefly_account": suggestion.firefly_account, "category": suggestion.category,
            "merchant": suggestion.merchant, "transaction_type": suggestion.transaction_type,
            "amount": f"{suggestion.amount:.2f}" if suggestion.amount is not None else None,
            "duplicate_of_id": suggestion.duplicate_of_id,
        },
        "confidence": suggestion.confidence, "reason": suggestion.reason, "model": suggestion.model, "error": suggestion.error,
        "created_at": suggestion.created_at, "updated_at": suggestion.updated_at, "applied_at": suggestion.applied_at,
    }


def _suggest_running() -> bool:
    return _suggest_task is not None and not _suggest_task.done()


async def _run_ai_suggest(ids: list[int]) -> None:
    try:
        async with get_db_session() as db:
            counts = await ai_review.suggest_items(db, get_firefly(), get_llm(), ids, auto=False)
        logger.info("ai_suggest_done", **counts)
    except Exception as exc:
        logger.error("ai_suggest_failed", error_type=type(exc).__name__)


@router.post("/inbox/ai-suggest", status_code=202)
async def ai_suggest(db: DB, principal: ReviewPrincipal):
    """Ask the LLM, in the background, for a resolution of every ``needs_review`` item without a current suggestion."""
    global _suggest_task
    busy = HTTPException(409, detail={"detail": "Ya hay una revisión con IA en curso.", "code": "ai_suggest_running"})
    if _suggest_running():
        raise busy
    ids = await ai_review.eligible_ids(db, auto=False)
    if not ids:
        await db.commit()
        return {"queued": 0}
    await audit.record(db, action="inbox.ai_suggest", target_type="inbox", target_id="needs_review", detail={"queued": len(ids)}, principal=principal)
    await db.commit()
    if _suggest_running():  # another request started one while this one was committing
        raise busy
    _suggest_task = asyncio.create_task(_run_ai_suggest(ids))
    return {"queued": len(ids)}


@router.get("/inbox/ai-suggestions")
async def ai_suggestions(
    db: DB, principal: ReviewPrincipal,
    status: Literal["pending", "error", "discarded", "applied", "stale", "all"] = "pending",
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
):
    pairs, total = await ai_review.list_suggestions(db, status=status, limit=limit, offset=offset)
    await db.commit()  # persists the ``stale`` marks
    return {"items": [_suggestion_data(suggestion, item) for suggestion, item in pairs], "total": total}


@router.post("/inbox/ai-suggestions/apply")
async def apply_ai_suggestions(body: ApplySuggestions, db: DB, service: Service, principal: ReviewPrincipal):
    """Apply chosen suggestions through the normal review path. Amount changes and ignore/duplicate only by id."""
    applied, failed = await ai_review.apply(db, service, principal, ids=body.ids, all_above=body.all_above)
    await audit.record(
        db, action="inbox.ai_apply", target_type="inbox", target_id="ai_suggestions", principal=principal,
        detail={"applied": len(applied), "failed": len(failed), "all_above": body.all_above, "ids": len(body.ids)},
    )
    await db.commit()
    return {"applied": applied, "failed": failed}


@router.post("/inbox/ai-suggestions/{suggestion_id}/discard")
async def discard_ai_suggestion(suggestion_id: int, db: DB, principal: ReviewPrincipal):
    try:
        row = await ai_review.discard(db, suggestion_id, principal)
    except ai_review.ApplyFailedError as exc:
        raise HTTPException(409, detail={"detail": str(exc), "code": "suggestion_not_pending"}) from exc
    if row is None:
        raise HTTPException(404, detail={"detail": "La sugerencia no existe.", "code": "suggestion_not_found"})
    return _suggestion_data(row, await get_item(db, row.item_id))


@router.get("/inbox/{item_id}")
async def inbox_detail(item_id: int, db: DB, principal: ReadPrincipal, include_raw: bool = False):
    if include_raw and not principal.allows("review"):
        raise HTTPException(403, "include_raw requires review scope")
    item = await get_item(db, item_id)
    if include_raw:
        # The read itself is audited (never its content).
        await audit.record(db, action="inbox.raw_view", target_type="inbox_item", target_id=item.id, principal=principal)
        await db.commit()
    return item_data(item, redacted_raw=True, include_raw=include_raw)


@router.post("/inbox/{item_id}/reprocess")
async def retry(item_id: int, db: DB, service: Service, principal: ReviewPrincipal):
    item = await get_item(db, item_id)
    previous = item.status
    requeued = await InboxRepository(db).requeue(item_id)
    if not requeued:
        raise HTTPException(409, "Inbox item cannot be reprocessed in its current state")
    await audit.record(db, action="inbox.reprocess", target_type="inbox_item", target_id=item_id, detail={"previous_status": previous}, principal=principal)
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
    item = await get_item(db, item_id)
    if body is None or not body.suspicious:
        return item_data(await _review(db, service, item, "reject", principal))
    # "I don't recognize it": a created (auto-approved) charge is voided, anything else rejected; then the
    # auto-approval pause is raised. Retrying after an interrupted attempt skips the already finished step.
    if item.status not in {"voided", "rejected"}:
        item = await _review(db, service, item, "void" if item.status == "created" else "reject", principal)
    try:
        await flags.mark_suspicious(db, item, principal)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(503, detail={
            "detail": "The item was removed but the suspicious flag could not be saved; repeat the request (safe to retry)",
            "code": "suspicious_not_marked",
        }) from exc
    return item_data(item)


@router.patch("/inbox/{item_id}")
async def update_item(item_id: int, corrections: ReviewCorrections, db: DB, service: Service, principal: ReviewPrincipal):
    item = await get_item(db, item_id)
    return item_data(await _review(db, service, item, "edit", principal, corrections.model_dump(exclude_unset=True)))


@router.post("/inbox/{item_id}/void")
async def void_item(item_id: int, db: DB, service: Service, principal: ReviewPrincipal):
    return item_data(await _review(db, service, await get_item(db, item_id), "void", principal))
