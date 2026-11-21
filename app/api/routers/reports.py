"""Forecast, subscriptions, compare, capture and backup status (contract sections 4, 6, 11). Owner: Phase 5 (A5)."""
from datetime import date as calendar_date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.dependencies import READ
from app.api.routers.common import DB
from app.core.config import Settings, get_settings
from app.services import backup, reports

router = APIRouter()
AppSettings = Annotated[Settings, Depends(get_settings)]


@router.get("/reports/forecast", dependencies=[READ])
async def forecast(db: DB, settings: AppSettings, date: calendar_date | None = None, tag: str | None = None):
    return await reports.forecast(db, settings, date, tag)


@router.get("/reports/subscriptions", dependencies=[READ])
async def subscriptions(db: DB, settings: AppSettings, date: calendar_date | None = None, tag: str | None = None):
    return await reports.subscriptions(db, settings, date, tag)


@router.get("/reports/compare", dependencies=[READ])
async def compare(
    db: DB, settings: AppSettings, a_from: Annotated[calendar_date, Query()], a_to: Annotated[calendar_date, Query()],
    b_from: Annotated[calendar_date, Query()], b_to: Annotated[calendar_date, Query()], tag: str | None = None,
):
    try:
        return await reports.compare(db, settings, a_from, a_to, b_from, b_to, tag)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.get("/system/capture", dependencies=[READ])
async def capture(db: DB, settings: AppSettings):
    return await reports.capture_status(db, settings)


@router.get("/backup/status", dependencies=[READ])
async def backup_status(settings: AppSettings):
    return backup.read_status(settings)
