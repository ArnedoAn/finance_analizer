"""Server errors for the app: ``GET /system/errors`` (see ``app.core.error_log``)."""
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import REVIEW
from app.api.routers.common import DB
from app.core import error_log

router = APIRouter()


@router.get("/system/errors", dependencies=[REVIEW])
async def list_errors(
    db: DB, since: datetime | None = None, component: str | None = None,
    limit: Annotated[int, Query(ge=1, le=error_log.MAX_ROWS)] = 50, offset: Annotated[int, Query(ge=0)] = 0,
):
    rows, total = await error_log.entries(db, since=since, component=component, limit=limit, offset=offset)
    return {"items": [error_log.error_data(row) for row in rows], "total": total}
