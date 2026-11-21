"""Ingestion: phone webhook, connection check and owner manual entries (contract section 2)."""
import math
import time
from collections import deque
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse

from app.api.dependencies import ANY_KEY, INGEST, REVIEW, Principal, get_firefly, get_llm
from app.api.routers.common import DB, Service, item_data
from app.core.logging import get_logger
from app.db.database import get_db_session
from app.db.repositories import InboxRepository
from app.models.schemas import ManualTransaction, WebhookPayload
from app.services import alerts as alerting
from app.services.processor import Processor

logger = get_logger(__name__)
router = APIRouter()

INGEST_WINDOW_SECONDS = 3600
# Webhook calls per ingest device in the last hour (monotonic timestamps), keyed by ``Principal.device_id``
# (None = the server-side admin key). Per process: production runs a single uvicorn worker (Dockerfile.prod),
# so with N workers the effective limit would be N times INGEST_MAX_EVENTS_PER_HOUR; a restart resets it.
_recent_events: dict[int | None, deque[float]] = {}


def _clock() -> float:
    return time.monotonic()


def _register_event(device_id: int | None, limit: int) -> int | None:
    """Count one webhook call; return the ``Retry-After`` seconds when the device is over its hourly limit."""
    now = _clock()
    window = _recent_events.setdefault(device_id, deque())
    while window and now - window[0] >= INGEST_WINDOW_SECONDS:
        window.popleft()
    if len(window) >= limit:
        return max(1, math.ceil(window[0] + INGEST_WINDOW_SECONDS - now))
    window.append(now)
    return None


async def _alert_flood(db, principal: Principal, limit: int) -> None:
    """One ``system_failure`` per device and hour; the alert must never replace the 429."""
    who = "admin" if principal.device_id is None else str(principal.device_id)
    try:
        await alerting.emit(
            db, "system_failure", title="Un dispositivo de captura supera el límite de eventos",
            body=f"El dispositivo «{principal.name}» envió más de {limit} eventos en una hora y el servidor rechaza los siguientes. "
                 "Si no es el ritmo normal de tu teléfono, revoca su clave en Dispositivos.",
            dedupe_key=f"ingest-flood:{who}", min_interval_minutes=INGEST_WINDOW_SECONDS // 60,
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.error("ingest_flood_alert_failed", error_type=type(exc).__name__)


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


@router.post("/notifications/webhook")
async def webhook(payload: WebhookPayload, background: BackgroundTasks, service: Service, db: DB, principal: Annotated[Principal, INGEST]):
    limit = service.settings.ingest_max_events_per_hour
    retry_after = _register_event(principal.device_id, limit)
    if retry_after is not None:
        await _alert_flood(db, principal, limit)
        raise HTTPException(
            429, detail={"detail": "Too many events from this device", "code": "rate_limited"},
            headers={"Retry-After": str(retry_after)},
        )
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
        return JSONResponse(status_code=502, content={
            "detail": "External service unavailable", "code": "upstream_unavailable", "inbox_id": item.id, "status": item.status,
        })
    return {**item_data(item), "inbox_id": item.id}
