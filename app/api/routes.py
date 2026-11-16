"""Single-owner API surface. Only liveness/readiness bypass API-key auth."""
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_firefly, get_llm, get_processor, require_api_key
from app.clients.imap import IMAPClient
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.database import get_db_session
from app.db.repositories import InboxRepository, KnownSenderRepository, MailStateRepository
from app.models.schemas import ManualTransaction, SenderInput, WebhookPayload
from app.services.processor import Processor

logger = get_logger(__name__)
router = APIRouter()
private = APIRouter(dependencies=[Depends(require_api_key)])
DB = Annotated[AsyncSession, Depends(get_db)]
Service = Annotated[Processor, Depends(get_processor)]
_scheduler = None


def item_data(item, detail=False):
    return {column.name: getattr(item, column.name) for column in item.__table__.columns if detail or column.name != "raw_text"}


async def process_background(item_id):
    try:
        async with get_db_session() as db:
            item = await InboxRepository(db).get(item_id)
            if item is not None:
                await Processor(db, get_firefly(), get_llm()).process(item)
    except Exception:
        logger.exception("background_processing_failed", inbox_id=item_id)


async def run_cycle():
    async with get_db_session() as db:
        return await Processor(db, get_firefly(), get_llm()).cycle()


async def learn_senders():
    async with get_db_session() as db:
        return await Processor(db, get_firefly(), get_llm()).learn_senders()


@router.get("/health/live")
async def live():
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(db: DB):
    # Readiness means local durable storage is available, not third-party uptime.
    await InboxRepository(db).list(limit=1)
    return {"status": "ready"}


@private.get("/health")
async def health(db: DB):
    await InboxRepository(db).list(limit=1)
    mail = {}
    for account in get_settings().mail_accounts:
        mail[account.name] = (await IMAPClient(account).test())["ok"]
    services = {"database": True, "firefly": await get_firefly().check_connection(), "llm": await get_llm().check_connection(), "mail": mail}
    return {"status": "ok" if services["firefly"] and services["llm"] and all(mail.values()) else "degraded", "version": "2.0.0", "services": services}


@private.post("/notifications/webhook")
async def webhook(payload: WebhookPayload, background: BackgroundTasks, service: Service, db: DB):
    item, accepted = await service.ingest_notification(payload)
    await db.commit()
    if not accepted:
        return {"accepted": False, "status": "ignored" if item.status == "ignored" else "duplicate"}
    background.add_task(process_background, item.id)
    return JSONResponse(status_code=202, content={"accepted": True, "status": "queued", "inbox_id": item.id})


@private.post("/transactions/manual", status_code=201)
async def manual(payload: ManualTransaction, service: Service, db: DB):
    item = await service.manual(payload)
    await db.commit()
    if item.status != "created":
        raise HTTPException(503, detail={"inbox_id": item.id, "status": item.status, "error": item.last_error})
    return {"inbox_id": item.id, "firefly_transaction_id": item.firefly_transaction_id, "status": "created"}


@private.post("/processing/run")
async def processing_run(service: Service, dry_run: bool = False):
    return await service.cycle(dry_run=dry_run)


@private.get("/processing/statistics")
async def statistics(db: DB):
    return await InboxRepository(db).statistics()


@private.get("/inbox")
async def inbox(db: DB, status: str | None = None, source: str | None = None, method: str | None = None, limit: int = Query(50, ge=1, le=500)):
    rows = await InboxRepository(db).list(status, source, method, limit)
    return {"items": [item_data(row) for row in rows], "total": await InboxRepository(db).count(status, source, method)}


@private.post("/inbox/retry-failed")
async def retry_failed(db: DB):
    return {"requeued": await InboxRepository(db).requeue(failed_only=True)}


@private.get("/inbox/{item_id}")
async def inbox_detail(item_id: int, db: DB):
    item = await InboxRepository(db).get(item_id)
    if item is None:
        raise HTTPException(404, "Inbox item not found")
    return item_data(item, detail=True)

@private.post("/inbox/{item_id}/retry")
async def retry(item_id: int, db: DB):
    if await InboxRepository(db).get(item_id) is None:
        raise HTTPException(404, "Inbox item not found")
    return {"requeued": await InboxRepository(db).requeue(item_id)}


@private.get("/mail/accounts")
async def mail_accounts(db: DB):
    states = {row.name: row for row in await MailStateRepository(db).all()}
    result = []
    for account in get_settings().mail_accounts:
        data = account.model_dump(exclude={"password"})
        state = states.get(account.name)
        data.update({key: getattr(state, key, None) for key in ("last_poll_at", "last_error", "last_uid")})
        result.append(data)
    return {"accounts": result}


@private.post("/mail/accounts/{name}/test")
async def test_mail_account(name: str):
    account = next((a for a in get_settings().mail_accounts if a.name == name), None)
    if account is None:
        raise HTTPException(404, "Mail account not found")
    return await IMAPClient(account).test()


@private.get("/senders")
async def senders(db: DB):
    return {"senders": jsonable_encoder(await KnownSenderRepository(db).all())}


@private.post("/senders")
async def add_sender(payload: SenderInput, db: DB):
    return jsonable_encoder(await KnownSenderRepository(db).add(payload.keyword, payload.name))

@private.delete("/senders/{keyword}")
async def delete_sender(keyword: str, db: DB):
    if not await KnownSenderRepository(db).delete(keyword):
        raise HTTPException(404, "Sender not found")
    return {"deleted": True}


@private.post("/senders/seed")
async def seed_senders(db: DB):
    return {"added": await KnownSenderRepository(db).seed()}


@private.post("/senders/learn")
async def sender_learning(service: Service):
    return await service.learn_senders()


@private.post("/sync/all")
async def sync(service: Service):
    return await service.sync(force=True)


@private.get("/sync/accounts")
async def accounts(service: Service):
    return await service.accounts()


@private.get("/sync/categories")
async def categories(service: Service):
    return await service.categories()


@private.get("/scheduler/status")
async def scheduler_status():
    return _scheduler.status() if _scheduler is not None else {"running": False, "jobs": []}


@private.post("/scheduler/jobs/{job_id}/trigger")
async def trigger_job(job_id: str):
    if job_id not in {"cycle", "sender_learning"}:
        raise HTTPException(404, "Unknown scheduler job")
    if _scheduler is None:
        raise HTTPException(503, "Scheduler unavailable")
    return await _scheduler.trigger(job_id)


router.include_router(private)
