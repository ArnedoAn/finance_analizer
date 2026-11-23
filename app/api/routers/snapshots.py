"""Instant dashboard: ``GET /dashboard/snapshot`` and ``POST /dashboard/snapshot/refresh`` (see ``app.services.snapshots``)."""
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.api.dependencies import READ, REVIEW, get_firefly
from app.api.routers.common import DB
from app.core.config import get_settings
from app.services import snapshots

router = APIRouter()
MonthParam = Annotated[str, Query(description="YYYY-MM")]


def _month(value: str) -> str:
    settings = get_settings()
    latest = snapshots.current_month(settings)
    year, number = int(latest[:4]), int(latest[5:])
    horizon = f"{year + (number == 12):04d}-{number % 12 + 1:02d}"  # next month at most
    if not snapshots.valid_month(value) or value > horizon:
        raise HTTPException(422, detail={"detail": "month must be YYYY-MM, from 2000 up to next month", "code": "invalid_month"})
    return value


async def _compute(month: str, db, *, again: bool = False) -> bool:
    """Compute and store in line; joins a recomputation already running for the month."""
    return await snapshots.run(month, (db, get_firefly(), get_settings()), again=again)


def _unavailable() -> HTTPException:
    return HTTPException(502, detail={"detail": "External service unavailable", "code": "upstream_unavailable"})


@router.get("/dashboard/snapshot", dependencies=[READ])
async def get_snapshot(db: DB, month: MonthParam):
    """The stored month. Missing: computed in line. Older than two hours: served with ``stale=true`` and recomputed behind."""
    month = _month(month)
    row = await snapshots.get(db, month)
    if row is None:
        if not await _compute(month, db):
            raise _unavailable()
        row = await snapshots.get(db, month)
        if row is None:
            raise _unavailable()
    elif snapshots.is_stale(row):
        snapshots.schedule(month)
    return snapshots.public(row)


@router.post("/dashboard/snapshot/refresh", dependencies=[REVIEW])
async def refresh_snapshot(db: DB, month: MonthParam):
    month = _month(month)
    if not await _compute(month, db, again=True):
        raise _unavailable()
    row = await snapshots.get(db, month)
    if row is None:
        raise _unavailable()
    return snapshots.public(row)
