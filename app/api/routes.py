"""Single-owner API surface. Only liveness/readiness bypass API-key auth."""
from datetime import date as calendar_date
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    get_db,
    get_firefly,
    get_llm,
    get_processor,
    require_api_key,
    require_phone_or_admin_key,
)
from app.clients.imap import IMAPClient
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.database import get_db_session
from app.db.models import Notification
from app.db.repositories import InboxRepository, KnownSenderRepository, MailStateRepository
from app.models.schemas import ManualTransaction, SenderInput, WebhookPayload
from app.services.processor import Processor

logger = get_logger(__name__)
router = APIRouter()
private = APIRouter(dependencies=[Depends(require_api_key)])
phone = APIRouter(dependencies=[Depends(require_phone_or_admin_key)])
DB = Annotated[AsyncSession, Depends(get_db)]
Service = Annotated[Processor, Depends(get_processor)]
_scheduler = None


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


async def get_item(db, item_id):
    item = await InboxRepository(db).get(item_id)
    if item is None:
        raise HTTPException(404, "Inbox item not found")
    return item


async def apply_review(db, service, item_id, action, corrections=None):
    item = await get_item(db, item_id)
    try:
        result = await service.review(item, action, corrections)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    await db.commit()
    return item_data(result if result is not None else item)


PUBLIC_ITEM_FIELDS = (
    "id", "source", "source_ref", "account_name", "message_id", "app", "sender",
    "subject_or_title", "occurred_at", "status", "method", "amount", "currency",
    "merchant", "category", "asset_account", "transaction_type", "destination_account",
    "original_kind", "template_id", "review_reason", "firefly_transaction_id",
    "duplicate_of_id", "attempts", "next_attempt_at", "last_error", "created_at", "updated_at",
)


def item_data(item, *, include_raw=False):
    data = {name: getattr(item, name) for name in PUBLIC_ITEM_FIELDS}
    if include_raw:
        data["raw_text"] = item.raw_text
    return data


async def process_background(item_id):
    try:
        async with get_db_session() as db:
            item = await InboxRepository(db).get(item_id)
            if item is not None:
                processor = Processor(db, get_firefly(), get_llm())
                await processor.process(item)
                await db.commit()
                if item.status in {"created", "needs_review", "failed"}:
                    await processor.notify_batch({item.status: 1})
    except Exception:
        logger.error("background_processing_failed", inbox_id=item_id)


async def run_cycle():
    async with get_db_session() as db:
        return await Processor(db, get_firefly(), get_llm()).cycle()


async def learn_senders():
    async with get_db_session() as db:
        return await Processor(db, get_firefly(), get_llm()).learn_senders()


async def run_reconcile():
    async with get_db_session() as db:
        return await Processor(db, get_firefly(), get_llm()).reconcile()


async def run_digest(weekly=False):
    async with get_db_session() as db:
        return await Processor(db, get_firefly(), get_llm()).digest(weekly=weekly)


async def run_weekly_digest():
    return await run_digest(weekly=True)


@phone.get("/auth/check")
async def auth_check():
    return {"ok": True}


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
    states = {row.name: row for row in await MailStateRepository(db).all()}
    mail = {}
    for account in get_settings().mail_accounts:
        state = states.get(account.name)
        last_poll = state.last_poll_at if state else None
        failures = state.consecutive_failures if state else 0
        stale = last_poll is None or last_poll < now_utc() - timedelta(hours=2)
        mail[account.name] = {
            "ok": not stale and failures == 0,
            "status": "stale" if stale else "error" if failures else "ok",
            "last_poll_at": last_poll,
            "consecutive_failures": failures,
        }
    services = {"database": True, "firefly": await get_firefly().check_connection(), "llm": await get_llm().check_connection(), "mail": mail}
    configured = get_settings().own_accounts
    missing = []
    if services["firefly"]:
        available = {row["name"] for row in await get_firefly().accounts() if row["type"] in {"asset", "liability", "liabilities"}}
        missing = [account.firefly_account for account in configured if account.firefly_account not in available]
    else:
        missing = [account.firefly_account for account in configured]
    services["missing_own_accounts"] = missing
    return {"status": "ok" if services["firefly"] and services["llm"] and all(state["ok"] for state in mail.values()) and not missing else "degraded", "version": "3.0.0", "services": services}


@phone.post("/notifications/webhook")
async def webhook(payload: WebhookPayload, background: BackgroundTasks, service: Service, db: DB):
    item, accepted = await service.ingest_notification(payload)
    await db.commit()
    if not accepted:
        return {"accepted": False, "status": "ignored" if item.status == "ignored" else "duplicate", "inbox_id": item.id}
    background.add_task(process_background, item.id)
    return JSONResponse(status_code=202, content={"accepted": True, "status": "queued", "inbox_id": item.id})


@private.post("/transactions/manual", status_code=201)
async def manual(payload: ManualTransaction, service: Service, db: DB):
    try:
        item = await service.manual(payload)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    await db.commit()
    if item.status != "created":
        return JSONResponse(status_code=502, content={"inbox_id": item.id, "status": "failed", "error": item.last_error})
    return {**item_data(item), "inbox_id": item.id}


@private.post("/processing/run")
async def processing_run(service: Service, dry_run: bool = False):
    return await service.cycle(dry_run=dry_run)


@private.get("/processing/statistics")
async def statistics(db: DB):
    return await InboxRepository(db).statistics()


@private.get("/inbox")
async def inbox(db: DB, status: str | None = None, source: str | None = None, method: str | None = None, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    rows = await InboxRepository(db).list(status, source, method, limit, offset)
    return {"items": [item_data(row) for row in rows], "total": await InboxRepository(db).count(status, source, method)}


@private.post("/inbox/retry-failed")
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


@private.get("/inbox/{item_id}")
async def inbox_detail(item_id: int, db: DB, include_raw: bool = False):
    item = await InboxRepository(db).get(item_id)
    if item is None:
        raise HTTPException(404, "Inbox item not found")
    return item_data(item, include_raw=include_raw)

@private.post("/inbox/{item_id}/reprocess")
async def retry(item_id: int, db: DB, service: Service):
    item = await get_item(db, item_id)
    requeued = await InboxRepository(db).requeue(item_id)
    if not requeued:
        raise HTTPException(409, "Inbox item cannot be reprocessed in its current state")
    await db.commit()
    await service.process(item)
    await db.commit()
    return {"requeued": requeued, "item": item_data(item)}


@private.post("/inbox/{item_id}/approve")
async def approve(item_id: int, db: DB, service: Service, corrections: ReviewCorrections | None = Body(default=None)):
    return await apply_review(db, service, item_id, "approve", corrections.model_dump(exclude_unset=True) if corrections else None)


@private.post("/inbox/{item_id}/reject")
async def reject(item_id: int, db: DB, service: Service):
    return await apply_review(db, service, item_id, "reject")


@private.patch("/inbox/{item_id}")
async def update_item(item_id: int, corrections: ReviewCorrections, db: DB, service: Service):
    return await apply_review(db, service, item_id, "edit", corrections.model_dump(exclude_unset=True))


@private.post("/inbox/{item_id}/void")
async def void_item(item_id: int, db: DB, service: Service):
    return await apply_review(db, service, item_id, "void")


@private.get("/mail/accounts")
async def mail_accounts(db: DB):
    states = {row.name: row for row in await MailStateRepository(db).all()}
    result = []
    for account in get_settings().mail_accounts:
        data = account.model_dump(exclude={"password"})
        state = states.get(account.name)
        data.update({key: getattr(state, key, None) for key in ("last_poll_at", "last_error", "last_uid", "consecutive_failures")})
        result.append(data)
    return {"accounts": result}


@private.post("/mail/accounts/{name}/test")
async def test_mail_account(name: str, db: DB):
    account = next((a for a in get_settings().mail_accounts if a.name == name), None)
    if account is None:
        raise HTTPException(404, "Mail account not found")
    # Reserve the test slot durably before any IMAP login. This key is separate
    # from notification delivery and never sends a Telegram message.
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    now = now_utc()
    key = f"mail-test:{name}"
    reservation = insert(Notification).values(key=key, last_sent_at=now)
    reservation = reservation.on_conflict_do_update(
        index_elements=[Notification.key],
        set_={"last_sent_at": now},
        where=Notification.last_sent_at <= now - timedelta(minutes=1),
    ).returning(Notification.key)
    reserved = await db.scalar(reservation)
    await db.commit()
    if reserved is None:
        raise HTTPException(429, "Mail account test limited to once per minute", headers={"Retry-After": "60"})
    return await IMAPClient(account).test()


@private.post("/mail/accounts/{name}/repoll")
async def repoll_mail(name: str, service: Service, since: calendar_date = Query(...)):
    if not any(account.name == name for account in get_settings().mail_accounts):
        raise HTTPException(404, "Mail account not found")
    return await service.poll_mail(since=since, account_name=name)


@private.get("/senders")
async def senders(db: DB):
    return {"senders": jsonable_encoder(await KnownSenderRepository(db).all(include_inactive=True))}


@private.post("/senders")
async def add_sender(payload: SenderInput, db: DB):
    try:
        return jsonable_encoder(await KnownSenderRepository(db).add(**payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc

@private.delete("/senders/{sender_id}")
async def delete_sender(sender_id: int, db: DB):
    if not await KnownSenderRepository(db).delete(sender_id):
        raise HTTPException(404, "Sender not found")
    return {"deleted": True}


@private.post("/senders/{sender_id}/approve")
async def approve_sender(sender_id: int, db: DB):
    try:
        row = await KnownSenderRepository(db).approve(sender_id)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(404, "Sender not found")
    return jsonable_encoder(row)




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


@private.get("/reports/summary")
async def report_summary(service: Service, period: Literal["day", "week", "month"] = "day", date: calendar_date | None = None):
    return await service.report(period, date)


@private.post("/reconcile/run")
async def reconcile(service: Service):
    return await service.reconcile()


@private.get("/scheduler/status")
async def scheduler_status():
    return _scheduler.status() if _scheduler is not None else {"running": False, "jobs": []}


@private.post("/scheduler/jobs/{job_id}/trigger")
async def trigger_job(job_id: str):
    if _scheduler is not None and job_id not in _scheduler.callbacks:
        raise HTTPException(404, "Unknown scheduler job")
    if _scheduler is None:
        raise HTTPException(503, "Scheduler unavailable")
    return await _scheduler.trigger(job_id)


router.include_router(private)
router.include_router(phone)
