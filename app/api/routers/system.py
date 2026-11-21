"""Health, mail, senders, sync, processing, scheduler and reconciliation (contract sections 6, 7, 11)."""
from datetime import date as calendar_date
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.encoders import jsonable_encoder

from app.api.dependencies import ADMIN, READ, get_firefly, get_llm
from app.api.routers.common import DB, Service
from app.clients.imap import IMAPClient
from app.core.config import get_settings
from app.core.timezone import now_utc
from app.db.models import Notification
from app.db.repositories import InboxRepository, KnownSenderRepository, MailStateRepository
from app.models.schemas import SenderInput

router = APIRouter()
_scheduler = None


@router.get("/health/live")
async def live():
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(db: DB):
    # Readiness means local durable storage is available, not third-party uptime.
    await InboxRepository(db).list(limit=1)
    return {"status": "ready"}


@router.get("/health", dependencies=[READ])
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
    return {"status": "ok" if services["firefly"] and services["llm"] and all(state["ok"] for state in mail.values()) and not missing else "degraded", "version": "4.0.0", "services": services}


@router.post("/processing/run", dependencies=[ADMIN])
async def processing_run(service: Service, dry_run: bool = False):
    return await service.cycle(dry_run=dry_run)


@router.get("/processing/statistics", dependencies=[READ])
async def statistics(db: DB):
    return await InboxRepository(db).statistics()


@router.get("/mail/accounts", dependencies=[READ])
async def mail_accounts(db: DB):
    states = {row.name: row for row in await MailStateRepository(db).all()}
    result = []
    for account in get_settings().mail_accounts:
        data = account.model_dump(exclude={"password"})
        state = states.get(account.name)
        data.update({key: getattr(state, key, None) for key in ("last_poll_at", "last_error", "last_uid", "consecutive_failures")})
        result.append(data)
    return {"accounts": result}


@router.post("/mail/accounts/{name}/test", dependencies=[ADMIN])
async def test_mail_account(name: str, db: DB):
    account = next((a for a in get_settings().mail_accounts if a.name == name), None)
    if account is None:
        raise HTTPException(404, "Mail account not found")
    # Reserve the test slot durably before any IMAP login. This key is separate
    # from alert delivery.
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


@router.post("/mail/accounts/{name}/repoll", dependencies=[ADMIN])
async def repoll_mail(name: str, service: Service, since: calendar_date = Query(...)):
    if not any(account.name == name for account in get_settings().mail_accounts):
        raise HTTPException(404, "Mail account not found")
    return await service.poll_mail(since=since, account_name=name)


@router.get("/senders", dependencies=[ADMIN])
async def senders(db: DB):
    return {"senders": jsonable_encoder(await KnownSenderRepository(db).all(include_inactive=True))}


@router.post("/senders", dependencies=[ADMIN])
async def add_sender(payload: SenderInput, db: DB):
    try:
        return jsonable_encoder(await KnownSenderRepository(db).add(**payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.delete("/senders/{sender_id}", dependencies=[ADMIN])
async def delete_sender(sender_id: int, db: DB):
    if not await KnownSenderRepository(db).delete(sender_id):
        raise HTTPException(404, "Sender not found")
    return {"deleted": True}


@router.post("/senders/{sender_id}/approve", dependencies=[ADMIN])
async def approve_sender(sender_id: int, db: DB):
    try:
        row = await KnownSenderRepository(db).approve(sender_id)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(404, "Sender not found")
    return jsonable_encoder(row)


@router.post("/senders/learn", dependencies=[ADMIN])
async def sender_learning(service: Service):
    return await service.learn_senders()


@router.post("/sync/all", dependencies=[ADMIN])
async def sync(service: Service):
    return await service.sync(force=True)


@router.get("/sync/accounts", dependencies=[READ])
async def accounts(service: Service):
    return await service.accounts()


@router.get("/sync/categories", dependencies=[READ])
async def categories(service: Service):
    return await service.categories()


@router.get("/reports/summary", dependencies=[READ])
async def report_summary(service: Service, period: Literal["day", "week", "month"] = "day", date: calendar_date | None = None):
    return await service.report(period, date)


@router.post("/reconcile/run", dependencies=[ADMIN])
async def reconcile(service: Service):
    return await service.reconcile()


@router.get("/scheduler/status", dependencies=[ADMIN])
async def scheduler_status():
    return _scheduler.status() if _scheduler is not None else {"running": False, "jobs": []}


@router.post("/scheduler/jobs/{job_id}/trigger", dependencies=[ADMIN])
async def trigger_job(job_id: str):
    if _scheduler is not None and job_id not in _scheduler.callbacks:
        raise HTTPException(404, "Unknown scheduler job")
    if _scheduler is None:
        raise HTTPException(503, "Scheduler unavailable")
    return await _scheduler.trigger(job_id)
