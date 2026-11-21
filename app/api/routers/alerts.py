"""Alerts polled by the app instead of bot pushes (contract sections 5 and 13). Owner: Phase 3."""
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.api.dependencies import READ
from app.api.routers.common import DB
from app.core.timezone import now_utc
from app.db.models_alerts import Alert
from app.services.alerts import alert_data

router = APIRouter()


@router.get("/alerts", dependencies=[READ])
async def list_alerts(
    db: DB,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    include_acked: bool = False,
):
    query = select(Alert).where(Alert.id > after_id)
    if not include_acked:
        query = query.where(Alert.acked_at.is_(None))
    rows = (await db.scalars(query.order_by(Alert.id).limit(limit))).all()
    return {"items": [alert_data(row) for row in rows], "last_id": rows[-1].id if rows else after_id}


@router.post("/alerts/{alert_id}/ack", dependencies=[READ])
async def ack_alert(alert_id: int, db: DB):
    alert = await db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(404, "Alert not found")
    if alert.acked_at is None:
        alert.acked_at = now_utc()
        await db.commit()
    return alert_data(alert)
