"""Monthly budgets: spreadsheet import, status, and the assignment of every expense to a budget.

Reads need ``read``; everything that writes or spends needs ``review``. Errors are ``{"detail", "code"}``. The owner's own
requests (column detection, the month sweep) only record their LLM calls in ``ai_budget``; the automatic assignment after each
cycle lives in ``app.services.budget_assign`` (called by ``ai_insights.after_cycle``) and is braked by the guard.
"""
import asyncio
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.dependencies import READ, ReviewPrincipal, get_firefly, get_llm
from app.api.routers.common import DB, Service, get_item, item_data
from app.core.logging import get_logger
from app.db.database import get_db_session
from app.services import audit, budget_assign, budget_import, budget_status
from app.services.processor import Processor

logger = get_logger(__name__)

router = APIRouter()

# One month sweep at a time. Per process (one uvicorn worker); a restart loses only this handle and the request can be repeated.
_assign_task: asyncio.Task | None = None


class ApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[int] = Field(default_factory=list, max_length=500)
    all_above: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _something_to_apply(self):
        if not self.ids and self.all_above is None:
            raise ValueError("Indica ids o all_above")
        return self


def _month(value: str | None, settings) -> str:
    month = value or budget_assign.current_month(ZoneInfo(settings.timezone))
    if not budget_assign.valid_month(month):
        raise HTTPException(422, detail={"detail": "El mes debe tener la forma AAAA-MM.", "code": "invalid_month"})
    return month


@router.post("/budgets/import")
async def import_budgets(
    db: DB, service: Service, principal: ReviewPrincipal,
    file: UploadFile = File(...), month: str | None = Form(None), dry_run: bool = Form(True), repeat_months: int = Form(0),
):
    """Read a .xlsx/.csv (category, amount) and create/update the Firefly budgets and the limit of ``month``. ``dry_run`` plans only."""
    month = _month(month, service.settings)
    data = await file.read(budget_import.MAX_BYTES + 1)  # nothing of the file is kept
    try:
        return await budget_import.run(
            db, service.firefly, service.llm, principal, filename=file.filename, data=data, month=month, dry_run=dry_run, repeat_months=repeat_months,
        )
    except budget_import.BudgetImportError as exc:
        raise HTTPException(exc.status, detail={"detail": exc.detail, "code": exc.code}) from exc


@router.get("/budgets/status", dependencies=[READ])
async def budgets_status(service: Service, month: str | None = Query(None)):
    return await budget_status.status(service.firefly, _month(month, service.settings), ZoneInfo(service.settings.timezone))


def _assign_running() -> bool:
    return _assign_task is not None and not _assign_task.done()


async def _run_assign(ids: list[int], settings) -> None:
    try:
        async with get_db_session() as db:
            counts = await budget_assign.run_batch(db, get_firefly(), get_llm(), settings, auto=False, ids=ids)
        logger.info("budget_assign_done", **{key: value for key, value in counts.items() if value})
    except Exception as exc:
        logger.error("budget_assign_failed", error_type=type(exc).__name__)


@router.post("/budgets/assign", status_code=202)
async def assign_month(db: DB, service: Service, principal: ReviewPrincipal, month: str | None = Query(None)):
    """Decide, in the background, the budget of every expense of ``month`` that has none (at most 500 per request)."""
    global _assign_task
    busy = HTTPException(409, detail={"detail": "Ya hay una asignación de presupuestos en curso.", "code": "budget_assign_running"})
    if _assign_running():
        raise busy
    month = _month(month, service.settings)
    ids = await budget_assign.pending_ids(db, ZoneInfo(service.settings.timezone), month=month)
    if not ids:
        return {"queued": 0}
    await audit.record(db, action="budgets.assign", target_type="budgets", target_id=month, detail={"month": month, "queued": len(ids)}, principal=principal)
    await db.commit()
    if _assign_running():  # another request started one while this one was committing
        raise busy
    _assign_task = asyncio.create_task(_run_assign(ids, service.settings))
    return {"queued": len(ids)}


def _suggestion(row, item) -> dict:
    return {
        "id": row.id, "status": row.status, "item": item_data(item), "budget": row.budget, "confidence": round(row.confidence, 2),
        "source": row.source, "reason": row.reason, "model": row.model, "group_id": row.group_id,
        "created_at": row.created_at, "updated_at": row.updated_at, "applied_at": row.applied_at,
    }


@router.get("/budgets/suggestions", dependencies=[READ])
async def budget_suggestions(
    db: DB, status: Literal["pending", "auto_applied", "applied", "discarded", "existing", "none", "stale", "error", "all"] = "pending",
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
):
    pairs, total = await budget_assign.listing(db, status=status, limit=limit, offset=offset)
    await db.commit()  # persists the ``stale`` marks
    return {"items": [_suggestion(row, item) for row, item in pairs], "total": total}


@router.post("/budgets/suggestions/apply")
async def apply_budget_suggestions(body: ApplyBody, db: DB, service: Service, principal: ReviewPrincipal):
    """Write the chosen budgets to Firefly (``all_above`` adds every pending one with confidence at or above it)."""
    processor = Processor(db, service.firefly, service.llm, service.settings)
    applied, failed = await budget_assign.apply(db, processor, principal, ids=body.ids, all_above=body.all_above)
    await audit.record(
        db, action="budgets.apply_suggestions", target_type="budgets", target_id="suggestions", principal=principal,
        detail={"applied": len(applied), "failed": len(failed), "all_above": body.all_above, "ids": len(body.ids)},
    )
    await db.commit()
    return {"applied": applied, "failed": failed}


@router.post("/budgets/suggestions/{suggestion_id}/discard")
async def discard_budget_suggestion(suggestion_id: int, db: DB, principal: ReviewPrincipal):
    try:
        row = await budget_assign.discard(db, suggestion_id, principal)
    except budget_assign.ApplyFailedError as exc:
        raise HTTPException(409, detail={"detail": str(exc), "code": "suggestion_not_pending"}) from exc
    if row is None:
        raise HTTPException(404, detail={"detail": "La sugerencia no existe.", "code": "suggestion_not_found"})
    return _suggestion(row, await get_item(db, row.item_id))
