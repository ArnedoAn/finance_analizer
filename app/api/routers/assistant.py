"""Assistant batches, proposals, apply/revert and audit log (contract sections 9 and 14). Owner: Phase 4.

Errors raise ``HTTPException(status, {"detail": ..., "code": ...})``; ``main.py`` renders that
as the flat ``{"detail", "code"}`` body of contract section 14.
"""
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from app.api.dependencies import ReadPrincipal, ReviewPrincipal
from app.api.routers.common import DB, Service, item_data
from app.services import assistant, assistant_errors, audit

router = APIRouter()


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    from_: AwareDatetime = Field(alias="from")
    to: AwareDatetime


class BatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window: Window
    instruction: str = Field(min_length=1, max_length=2000)


class ProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: int | None = None
    target_ref: str | int | None = None
    field: str = Field(min_length=1, max_length=30)
    new_value: Any = None
    reason: str = Field(min_length=1, max_length=1000)


class ProposalBulk(BaseModel):
    """Elements stay raw so that a malformed one fails alone (``ProposalCreate`` validates each)."""

    model_config = ConfigDict(extra="forbid")
    proposals: list[Any] = Field(min_length=1, max_length=assistant.MAX_PROPOSALS)


class ApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved_ids: list[int] = Field(default_factory=list, max_length=assistant.MAX_PROPOSALS)
    discard_others: bool = False


@contextmanager
def _coded_errors():
    try:
        yield
    except assistant_errors.AssistantError as exc:
        raise HTTPException(exc.status, detail={"detail": exc.detail, "code": exc.code}) from exc


def _proposal(proposal):
    return {
        "id": proposal.id, "batch_id": proposal.batch_id, "target_id": proposal.target_id, "target_ref": proposal.target_ref,
        "subject": proposal.subject, "field": proposal.field,
        "old_value": proposal.old_value, "new_value": proposal.new_value, "reason": proposal.reason,
        "risky": proposal.risky, "status": proposal.status, "error": proposal.error,
        "applied_at": proposal.applied_at, "reverted_at": proposal.reverted_at,
    }


async def _batch(db, batch):
    proposals, items = await assistant.batch_contents(db, batch)
    return {
        "id": batch.id, "window": {"from": batch.window_from, "to": batch.window_to}, "instruction": batch.instruction,
        "status": batch.status, "created_at": batch.created_at, "applied_at": batch.applied_at,
        "revert_deadline": assistant.revert_deadline(batch),
        "proposals": [{**_proposal(proposal), "target": item_data(items[proposal.target_id], redacted_raw=True) if proposal.target_id is not None else None} for proposal in proposals],
    }


@router.post("/assistant/batches", status_code=201)
async def create_batch(body: BatchCreate, db: DB, principal: ReadPrincipal):
    with _coded_errors():
        batch = await assistant.create_batch(db, window_from=body.window.from_, window_to=body.window.to, instruction=body.instruction, principal=principal)
        return await _batch(db, batch)


@router.post("/assistant/batches/{batch_id}/proposals", status_code=201)
async def create_proposal(batch_id: int, body: ProposalCreate, db: DB, service: Service, principal: ReadPrincipal):
    with _coded_errors():
        batch = await assistant.get_batch(db, batch_id)
        proposal = await assistant.propose(db, service, batch, **body.model_dump(), principal=principal)
        return _proposal(proposal)


def _entry(raw) -> dict | assistant_errors.AssistantError:
    try:
        return ProposalCreate.model_validate(raw).model_dump()
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        return assistant_errors.AssistantError(422, "validation_error", f"Invalid proposal{' at ' + where if where else ''}: {first['msg']}")


@router.post("/assistant/batches/{batch_id}/proposals/bulk")
async def create_proposals_bulk(batch_id: int, body: ProposalBulk, db: DB, service: Service, principal: ReadPrincipal):
    """Up to 50 proposals in one call; each is validated and created like ``POST .../proposals`` and a bad one fails alone."""
    with _coded_errors():
        results = await assistant.propose_bulk(db, service, batch_id, [_entry(raw) for raw in body.proposals], principal)
    items = [
        {"ok": False, "error": {"detail": result.detail, "code": result.code}} if isinstance(result, assistant_errors.AssistantError) else {"ok": True, "proposal": _proposal(result)}
        for result in results
    ]
    failed = sum(1 for item in items if not item["ok"])
    return {"items": items, "created": len(items) - failed, "failed": failed}


@router.get("/assistant/batches/{batch_id}")
async def get_batch(batch_id: int, db: DB, principal: ReadPrincipal):
    with _coded_errors():
        return await _batch(db, await assistant.get_batch(db, batch_id))


@router.post("/assistant/batches/{batch_id}/apply")
async def apply_batch(
    batch_id: int, body: ApplyBody, db: DB, service: Service, principal: ReviewPrincipal,
    idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
):
    with _coded_errors():
        applied, failed = await assistant.apply(
            db, service, batch_id, approved_ids=body.approved_ids, discard_others=body.discard_others, principal=principal,
            idempotency_key=idempotency_key or None,
        )
        return {"batch": await _batch(db, await assistant.get_batch(db, batch_id)), "applied": applied, "failed": failed}


@router.post("/assistant/batches/{batch_id}/revert")
async def revert_batch(batch_id: int, db: DB, service: Service, principal: ReviewPrincipal):
    with _coded_errors():
        reverted, failed = await assistant.revert(db, service, batch_id, principal=principal)
        return {"batch": await _batch(db, await assistant.get_batch(db, batch_id)), "reverted": reverted, "failed": failed}


@router.get("/audit")
async def audit_log(db: DB, principal: ReadPrincipal, since: AwareDatetime | None = None, action: str | None = None, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    rows, total = await audit.entries(db, since=since, action=action, limit=limit, offset=offset)
    items = [
        {"id": row.id, "at": row.at, "device_id": row.device_id, "actor": row.actor, "action": row.action, "target_type": row.target_type, "target_id": row.target_id, "detail": row.detail}
        for row in rows
    ]
    return {"items": items, "total": total}
