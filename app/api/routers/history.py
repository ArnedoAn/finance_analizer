"""Owner-started history import (contract section 11): mailboxes by date range and the phone's SMS inbox.

Both paths count first (``dry_run``) so the owner sees how many messages would reach the LLM before
spending tokens, then store and process the new ones right away. Nothing is ever imported twice:
mail is matched by Message-ID and reference, SMS by the same reference the live capture uses.
"""
import asyncio
import math
import time
from collections import deque
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.api.dependencies import REVIEW, Principal, get_firefly, get_llm
from app.api.routers.common import DB, Service
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.database import get_db_session
from app.models.schemas import HistoryMailRequest, HistorySmsBatch
from app.services.processor import Processor

logger = get_logger(__name__)
router = APIRouter()

WINDOW_SECONDS = 3600
# Messages per device and hour, apart from the live webhook's limit: an import is a deliberate burst.
SMS_MAX_PER_HOUR = 2000
SMS_PREVIEW_MAX_PER_HOUR = 10000
_sms_windows: dict[tuple[str, int | None], deque[tuple[float, int]]] = {}
# One mail import at a time. Per process: production runs a single uvicorn worker; a restart loses only
# this status, never stored items, and the import can simply run again.
_mail_job: dict | None = None
_mail_task: asyncio.Task | None = None
_sms_progress = {"queued": 0, "processed": 0}
_sms_lock = asyncio.Lock()


def _clock() -> float:
    return time.monotonic()


def _take(kind: str, device_id: int | None, amount: int, limit: int) -> int | None:
    """Reserve ``amount`` messages in the device's hourly window; return ``Retry-After`` seconds when full."""
    now = _clock()
    window = _sms_windows.setdefault((kind, device_id), deque())
    while window and now - window[0][0] >= WINDOW_SECONDS:
        window.popleft()
    if sum(count for _, count in window) + amount > limit:
        return max(1, math.ceil(window[0][0] + WINDOW_SECONDS - now)) if window else WINDOW_SECONDS
    window.append((now, amount))
    return None


def _today():
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


async def _run_mail(job: dict, principal: Principal) -> None:
    try:
        async with get_db_session() as db:
            from app.services import audit

            processor = Processor(db, get_firefly(), get_llm())
            result, ids = await processor.history_mail(job["since"], job["until"], job["dry_run"],
                                                       progress=lambda counts: job.update(counts=dict(counts)))
            job.update(counts=result["counts"], errors=result["errors"])
            if not job["dry_run"]:
                await audit.record(db, action="history.mail", target_type="history", target_id="mail", principal=principal,
                                   detail={"since": job["since"].isoformat(), "until": job["until"].isoformat(), "new": len(ids)})
                await db.commit()
                job.update(phase="processing", total=len(ids))
                await processor.process_ids(ids, progress=lambda done: job.update(processed=done))
        job["state"] = "failed" if job["errors"] and len(job["errors"]) == len(get_settings().mail_accounts) else "done"
    except Exception as exc:
        logger.error("history_mail_failed", error_type=type(exc).__name__)
        job.update(state="failed", error="La importación se detuvo por un error del servidor; puedes repetirla sin duplicar nada.")
    finally:
        job["finished_at"] = now_utc()


async def _process_sms(ids: list[int]) -> None:
    async with _sms_lock:
        try:
            async with get_db_session() as db:
                await Processor(db, get_firefly(), get_llm()).process_ids(
                    ids, progress=lambda _: _sms_progress.update(processed=_sms_progress["processed"] + 1))
        except Exception as exc:
            logger.error("history_sms_processing_failed", error_type=type(exc).__name__)


@router.post("/history/mail", status_code=202)
async def start_mail_import(payload: HistoryMailRequest, principal: Annotated[Principal, REVIEW]):
    global _mail_job, _mail_task
    if payload.until > _today():
        raise HTTPException(422, detail={"detail": "until must not be in the future", "code": "invalid_range"})
    if not get_settings().mail_accounts:
        raise HTTPException(422, detail={"detail": "No mailbox is configured", "code": "no_mail_accounts"})
    if _mail_job is not None and _mail_job["state"] == "running":
        raise HTTPException(409, detail={"detail": "A mail import is already running", "code": "history_running"})
    _mail_job = {
        "state": "running", "phase": "scanning", "since": payload.since, "until": payload.until, "dry_run": payload.dry_run,
        "counts": Processor._history_counts(), "errors": {}, "total": 0, "processed": 0, "error": None,
        "started_at": now_utc(), "finished_at": None,
    }
    _mail_task = asyncio.create_task(_run_mail(_mail_job, principal))
    return {"job": _mail_job}


@router.get("/history", dependencies=[REVIEW])
async def history_status():
    return {"mail": _mail_job, "sms": dict(_sms_progress)}


@router.post("/history/sms")
async def import_sms(payload: HistorySmsBatch, background: BackgroundTasks, service: Service, db: DB,
                     principal: Annotated[Principal, REVIEW]):
    kind, limit = ("preview", SMS_PREVIEW_MAX_PER_HOUR) if payload.dry_run else ("import", SMS_MAX_PER_HOUR)
    retry_after = _take(kind, principal.device_id, len(payload.items), limit)
    if retry_after is not None:
        raise HTTPException(429, detail={"detail": "Too many imported messages this hour", "code": "rate_limited"},
                            headers={"Retry-After": str(retry_after)})
    counts, ids = await service.history_sms(payload.items, payload.dry_run)
    if ids:
        from app.services import audit

        await audit.record(db, action="history.sms", target_type="history", target_id="sms", principal=principal, detail={"new": len(ids)})
        await db.commit()
        _sms_progress["queued"] += len(ids)
        background.add_task(_process_sms, ids)
    return {"counts": counts, "queued": len(ids)}
