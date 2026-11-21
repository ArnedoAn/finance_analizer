"""Widget summary, category suggestions, quick entry and auto-approval flags (contract sections 13, 14). Owner: Phase 5 (A7)."""
import asyncio
import calendar
from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.dependencies import READ, ReviewPrincipal, get_firefly
from app.api.routers.common import DB, Service, get_item, item_data
from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.models import InboxItem
from app.services import flags, quick, suggestions

logger = get_logger(__name__)
router = APIRouter()

PHONE_SOURCES = ("sms", "notification")
STALE_REVIEW = timedelta(hours=2)
BUDGET_TIMEOUT_SECONDS = 5


class QuickRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200)
    client_ref: str = Field(min_length=1, max_length=240)
    account: str | None = Field(default=None, max_length=255)


def _money(value) -> str:
    return str(Decimal(str(value if value is not None else 0)).quantize(Decimal("0.01")))


async def _budgets(today):
    start = today.replace(day=1)
    end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    try:
        rows = await asyncio.wait_for(get_firefly().budget_progress(start, end), BUDGET_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("widget_budgets_unavailable", error_type=type(exc).__name__)
        return []
    budgets = []
    for row in rows:
        limit, spent = Decimal(str(row["limit"])), Decimal(str(row["spent"]))
        pct = round(float(spent / limit * 100), 1) if limit > 0 else 0.0
        budgets.append({"name": row["name"], "spent": _money(spent), "limit": _money(limit), "pct": pct})
    return sorted(budgets, key=lambda budget: budget["pct"], reverse=True)[:3]


async def _system(db, settings, now):
    last_event = await db.scalar(select(func.max(InboxItem.created_at)))
    last_phone = await db.scalar(select(func.max(InboxItem.created_at)).where(InboxItem.source.in_(PHONE_SOURCES)))
    failed = await db.scalar(select(func.count()).select_from(InboxItem).where(InboxItem.status == "failed"))
    stale_review = await db.scalar(
        select(func.count()).select_from(InboxItem).where(InboxItem.status == "needs_review", InboxItem.created_at < now - STALE_REVIEW)
    )
    silent = last_phone is not None and now - last_phone > timedelta(hours=settings.phone_silence_hours)
    status = "red" if failed or silent else "yellow" if stale_review else "green"
    return {"status": status, "last_event_at": last_event.isoformat() if last_event else None}


@router.get("/widgets/summary", dependencies=[READ])
async def widgets_summary(db: DB, service: Service):
    from app.services import reports

    settings = service.settings
    now = now_utc()
    forecast = await reports.forecast(db, settings)
    pending = await db.scalar(select(func.count()).select_from(InboxItem).where(InboxItem.status == "needs_review"))
    recent = (await db.scalars(
        select(InboxItem).where(InboxItem.status == "created").order_by(InboxItem.occurred_at.desc(), InboxItem.id.desc()).limit(5)
    )).all()
    today = now.astimezone(service.zone).date()
    return {
        "generated_at": now.isoformat(),
        "currency": "COP",
        "month": {
            "income": _money(forecast.get("income")),
            "expenses": _money(forecast.get("spent")),
            "balance": _money(forecast.get("balance")),
        },
        "daily": {
            "available": _money(forecast.get("daily_available")),
            "spent_today": _money(forecast.get("spent_today")),
            "days_left": forecast.get("days_left"),
        },
        "pending_review": pending or 0,
        "budgets": await _budgets(today),
        "recent": [
            {
                "id": row.id, "occurred_at": row.occurred_at.isoformat(), "merchant": row.merchant,
                "amount": _money(row.amount), "currency": row.currency,
                "transaction_type": row.transaction_type, "category": row.category,
            }
            for row in recent
        ],
        "system": await _system(db, settings, now),
    }


@router.get("/inbox/{item_id}/suggestions", dependencies=[READ])
async def item_suggestions(item_id: int, db: DB):
    item = await get_item(db, item_id)
    return {"item_id": item.id, "categories": await suggestions.suggest(db, item, 3)}


@router.post("/transactions/quick", status_code=201)
async def quick_entry(payload: QuickRequest, principal: ReviewPrincipal, service: Service, db: DB):
    try:
        item = await quick.create_entry(
            service, text=payload.text, client_ref=payload.client_ref, account=payload.account, principal=principal
        )
    except quick.UnparseableError:
        return JSONResponse(status_code=422, content={"detail": "Could not read an amount from the text", "code": "unparseable"})
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    await db.commit()
    if item.status == "failed":
        return JSONResponse(status_code=502, content={"inbox_id": item.id, "status": "failed", "error": item.last_error})
    return item_data(item)


@router.get("/flags", dependencies=[READ])
async def flags_status(db: DB):
    return await flags.status(db)


@router.post("/flags/auto-approve/resume")
async def resume_auto_approve(principal: ReviewPrincipal, db: DB):
    from app.services import audit

    await flags.resume(db)
    await audit.record(
        db, action="flags.resume_auto_approve", target_type="flag", target_id="auto_approve_paused", principal=principal
    )
    await db.commit()
    return {"paused": False}
