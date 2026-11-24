"""Trips and events detected among the expenses (``app.services.ai_events``): ``GET /ai/events`` (read) and the owner's decisions (review)."""
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import READ, ReviewPrincipal
from app.api.routers.common import DB, Service
from app.services import ai_events

router = APIRouter()


class ApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, min_length=2, max_length=60)


def _error(exc: ai_events.EventError) -> HTTPException:
    return HTTPException(exc.status, detail={"detail": exc.detail, "code": exc.code})


@router.get("/ai/events", dependencies=[READ])
async def list_events(
    db: DB, status: Annotated[str, Query(pattern="^(pending|applied|discarded|stale|all)$")] = "pending",
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
):
    items, total = await ai_events.listing(db, status=status, limit=limit, offset=offset)
    await db.commit()  # persists proposals that became stale
    return {"items": items, "total": total}


@router.post("/ai/events/{event_id}/apply")
async def apply_event(event_id: int, db: DB, service: Service, principal: ReviewPrincipal, body: ApplyBody | None = None):
    try:
        row = await ai_events.apply(db, service, principal, event_id, body.label if body else None)
    except ai_events.EventError as exc:
        raise _error(exc) from exc
    return ai_events.public(row)


@router.post("/ai/events/{event_id}/discard")
async def discard_event(event_id: int, db: DB, principal: ReviewPrincipal):
    try:
        row = await ai_events.discard(db, event_id, principal)
    except ai_events.EventError as exc:
        raise _error(exc) from exc
    return ai_events.public(row)
