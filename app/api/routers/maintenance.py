"""One-off corrections of data already written: ``POST /maintenance/descriptions/fix`` and the admin-only
``POST /maintenance/reset`` (full reset of movements, see ``app.services.server_reset``)."""
import asyncio
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict

from app.api.dependencies import ADMIN, Principal, ReviewPrincipal, get_firefly, get_llm
from app.api.routers.common import DB, Service
from app.core.logging import get_logger
from app.db.database import get_db_session
from app.services import audit, descriptions, server_reset
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


class ResetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry_run: bool = True
    confirm: str | None = None
    keep_rules: bool = False
    reset_mail_cursor: bool = False


@router.post("/maintenance/reset")
async def reset(service: Service, db: DB, principal: Annotated[Principal, ADMIN], response: Response, body: ResetBody | None = None):
    """Delete everything about movements to re-import from the mailbox. ``admin`` scope only.

    ``dry_run`` (default) answers 200 with the counts per table and the Firefly movements and accounts that would go,
    changing nothing. A real run needs ``confirm: "BORRAR MOVIMIENTOS"`` (422 ``confirmation_required``), runs in the
    background, one at a time (409 ``reset_running``), and answers 202 ``{id}``; ``GET /maintenance/reset/{id}`` shows it."""
    body = body or ResetBody()
    options = server_reset.ResetOptions(keep_rules=body.keep_rules, reset_mail_cursor=body.reset_mail_cursor)
    if body.dry_run:
        return await server_reset.preview(db, service.firefly, options)
    if body.confirm != server_reset.CONFIRMATION:
        raise HTTPException(422, detail={"detail": f"Para borrar escribe exactamente «{server_reset.CONFIRMATION}» en confirm.", "code": "confirmation_required"})
    try:
        run_id = server_reset.start(service.firefly, get_db_session, options, principal)
    except server_reset.ResetBusyError:
        raise HTTPException(409, detail={"detail": "Ya hay un borrado en curso.", "code": "reset_running"}) from None
    response.status_code = 202
    return {"id": run_id}


@router.get("/maintenance/reset/{run_id}", dependencies=[ADMIN])
async def reset_status(run_id: str):
    state = server_reset.get(run_id)
    if state is None:
        raise HTTPException(404, detail={"detail": "No existe ese borrado (el servidor pudo reiniciarse; repite el modo de prueba).", "code": "reset_not_found"})
    return state
