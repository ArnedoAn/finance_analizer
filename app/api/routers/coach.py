"""Weekly coach: ``GET /coach/tips`` (read) and ``POST /coach/tips/{id}/feedback`` (review). See ``app.services.coach``."""
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.api.dependencies import READ, ReviewPrincipal
from app.api.routers.common import DB
from app.core.timezone import now_utc
from app.db.models import CoachTip
from app.services import audit, coach

router = APIRouter()


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    useful: bool


@router.get("/coach/tips", dependencies=[READ])
async def list_tips(db: DB, limit: Annotated[int, Query(ge=1, le=200)] = 20, offset: Annotated[int, Query(ge=0)] = 0):
    total = await db.scalar(select(func.count()).select_from(CoachTip))
    rows = (await db.scalars(select(CoachTip).order_by(CoachTip.week.desc(), CoachTip.id).limit(limit).offset(offset))).all()
    return {"items": [coach.public(row) for row in rows], "total": total}


@router.post("/coach/tips/{tip_id}/feedback")
async def tip_feedback(tip_id: int, body: FeedbackBody, db: DB, principal: ReviewPrincipal):
    tip = await db.get(CoachTip, tip_id, populate_existing=True)
    if tip is None:
        raise HTTPException(404, detail={"detail": "El hallazgo no existe.", "code": "tip_not_found"})
    tip.useful, tip.feedback_at = body.useful, now_utc()
    await audit.record(db, action="coach.feedback", target_type="coach_tip", target_id=tip.id, principal=principal, detail={"useful": body.useful, "kind": tip.kind})
    await db.commit()
    return coach.public(tip)
