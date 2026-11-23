"""One-off corrections of data already written: ``POST /maintenance/descriptions/fix``."""
import asyncio
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Response

from app.api.dependencies import Principal, ReviewPrincipal, get_firefly, get_llm
from app.api.routers.common import DB
from app.core.logging import get_logger
from app.db.database import get_db_session
from app.services import audit, descriptions
from app.services.processor import Processor

logger = get_logger(__name__)
router = APIRouter()

# One run at a time, per process (production runs a single uvicorn worker). A restart loses only this handle: the run
# can be repeated, because fixed items no longer qualify.
_fix_task: asyncio.Task | None = None


def _running() -> bool:
    return _fix_task is not None and not _fix_task.done()


async def _run_fix(ids: list[int], principal: Principal, group_id: str) -> None:
    try:
        async with get_db_session() as db:
            result = await descriptions.fix(db, Processor(db, get_firefly(), get_llm()), ids, principal, group_id)
        logger.info("descriptions_fix_done", **result)
    except Exception as exc:
        logger.error("descriptions_fix_failed", error_type=type(exc).__name__)


@router.post("/maintenance/descriptions/fix")
async def fix_descriptions(db: DB, principal: ReviewPrincipal, response: Response, dry_run: bool = False):
    """Rename, in Firefly and in the inbox, the created e-mail movements that carry a generic subject as description.

    ``dry_run=true`` only counts them and shows ``{id, before, after}`` for the first 10. Otherwise the work runs in the
    background (202 ``{queued, group_id}``; ``POST /ledger/groups/{group_id}/undo`` reverts the whole run)."""
    global _fix_task
    busy = HTTPException(409, detail={"detail": "Ya hay una corrección de descripciones en curso.", "code": "descriptions_fix_running"})
    if _running() and not dry_run:
        raise busy
    found = await descriptions.candidates(db)
    if dry_run:
        return {"dry_run": True, "count": len(found), "examples": [descriptions.example(item, new) for item, new in found[:descriptions.EXAMPLES]]}
    if not found:
        return {"queued": 0, "group_id": None}
    group_id = uuid4().hex
    await audit.record(db, action="maintenance.descriptions_fix", target_type="inbox", target_id="descriptions",
                       detail={"queued": len(found), "group_id": group_id}, principal=principal)
    await db.commit()
    if _running():  # another request started one while this one was committing
        raise busy
    _fix_task = asyncio.create_task(_run_fix([item.id for item, _ in found], principal, group_id))
    response.status_code = 202
    return {"queued": len(found), "group_id": group_id}
