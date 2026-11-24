"""Credit card advice (``app.services.cards``): next cut and payment, interest-free days and which card to use today."""
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.api.dependencies import READ
from app.api.routers.common import DB, Service
from app.core.timezone import now_utc
from app.services import cards

router = APIRouter()


@router.get("/cards/advice", dependencies=[READ])
async def cards_advice(db: DB, service: Service, date: Annotated[str | None, Query(max_length=10)] = None):
    today = now_utc().astimezone(service.zone).date()
    try:
        day = cards.parse_day(date, today)
    except ValueError as exc:
        raise HTTPException(422, detail={"detail": "La fecha debe tener el formato AAAA-MM-DD.", "code": "invalid_date"}) from exc
    return await cards.advice(db, service.firefly, day)
