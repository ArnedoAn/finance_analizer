"""Ingestion: phone webhook, connection check and owner manual entries (contract section 2)."""
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse

from app.api.dependencies import ANY_KEY, INGEST, REVIEW, get_firefly, get_llm
from app.api.routers.common import DB, Service, item_data
from app.core.logging import get_logger
from app.db.database import get_db_session
from app.db.repositories import InboxRepository
from app.models.schemas import ManualTransaction, WebhookPayload
from app.services.processor import Processor

logger = get_logger(__name__)
router = APIRouter()


async def process_background(item_id):
    try:
        async with get_db_session() as db:
            item = await InboxRepository(db).get(item_id)
            if item is not None:
                processor = Processor(db, get_firefly(), get_llm())
                await processor.process(item)
                await db.commit()
    except Exception:
        logger.error("background_processing_failed", inbox_id=item_id)


@router.get("/auth/check", dependencies=[ANY_KEY])
async def auth_check():
    return {"ok": True}


@router.post("/notifications/webhook", dependencies=[INGEST])
async def webhook(payload: WebhookPayload, background: BackgroundTasks, service: Service, db: DB):
    item, accepted = await service.ingest_notification(payload)
    await db.commit()
    if not accepted:
        return {"accepted": False, "status": "ignored" if item.status == "ignored" else "duplicate", "inbox_id": item.id}
    background.add_task(process_background, item.id)
    return JSONResponse(status_code=202, content={"accepted": True, "status": "queued", "inbox_id": item.id})


@router.post("/transactions/manual", status_code=201, dependencies=[REVIEW])
async def manual(payload: ManualTransaction, service: Service, db: DB):
    try:
        item = await service.manual(payload)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    await db.commit()
    if item.status != "created":
        return JSONResponse(status_code=502, content={"inbox_id": item.id, "status": "failed", "error": item.last_error})
    return {**item_data(item), "inbox_id": item.id}
