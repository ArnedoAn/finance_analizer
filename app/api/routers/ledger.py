"""Manual ledger editing: create, edit, delete, bulk edit and undo (contract section 15).

Errors raise ``HTTPException(status, {"detail": ..., "code": ...})``; ``main.py`` renders that as the flat
``{"detail", "code"}`` body of contract section 14. Reads need ``read``; every write needs ``review``.
"""
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Query

from app.api.dependencies import ReadPrincipal, ReviewPrincipal
from app.api.routers.common import DB, Service
from app.core.exceptions import PermanentError
from app.services import ledger
from app.services.ledger import LedgerBulk, LedgerDraft

router = APIRouter()


@contextmanager
def _coded_errors():
    try:
        yield
    except ledger.LedgerError as exc:
        raise HTTPException(exc.status, detail={"detail": exc.detail, "code": exc.code}) from exc
    except PermanentError as exc:
        error = ledger.firefly_rejection(exc)
        raise HTTPException(error.status, detail={"detail": error.detail, "code": error.code}) from exc


@router.get("/ledger/transactions/{firefly_id}")
async def get_ledger_transaction(firefly_id: str, db: DB, service: Service, principal: ReadPrincipal):
    with _coded_errors():
        return await ledger.get(db, service, firefly_id)


@router.post("/ledger/transactions", status_code=201)
async def create_ledger_transaction(body: LedgerDraft, db: DB, service: Service, principal: ReviewPrincipal):
    with _coded_errors():
        transaction, change = await ledger.create(db, service, body, principal)
        return {"transaction": transaction, "change": ledger.change_data(change)}


@router.put("/ledger/transactions/{firefly_id}")
async def update_ledger_transaction(firefly_id: str, body: LedgerDraft, db: DB, service: Service, principal: ReviewPrincipal):
    with _coded_errors():
        transaction, change = await ledger.update(db, service, firefly_id, body, principal)
        return {"transaction": transaction, "change": ledger.change_data(change)}


@router.delete("/ledger/transactions/{firefly_id}")
async def delete_ledger_transaction(firefly_id: str, db: DB, service: Service, principal: ReviewPrincipal):
    with _coded_errors():
        return {"change": ledger.change_data(await ledger.delete(db, service, firefly_id, principal))}


@router.post("/ledger/bulk")
async def bulk_ledger_edit(body: LedgerBulk, db: DB, service: Service, principal: ReviewPrincipal):
    with _coded_errors():
        group_id, changed, failed = await ledger.bulk(db, service, body, principal)
        return {"group_id": group_id, "changed": changed, "failed": failed}


@router.get("/ledger/changes")
async def list_ledger_changes(db: DB, principal: ReadPrincipal, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    rows, total = await ledger.list_changes(db, limit=limit, offset=offset)
    return {"items": [ledger.change_data(row) for row in rows], "total": total}


@router.post("/ledger/changes/{change_id}/undo")
async def undo_ledger_change(change_id: int, db: DB, service: Service, principal: ReviewPrincipal):
    with _coded_errors():
        change, transaction = await ledger.undo(db, service, change_id, principal)
        return {"change": ledger.change_data(change), "transaction": transaction}


@router.post("/ledger/groups/{group_id}/undo")
async def undo_ledger_group(group_id: str, db: DB, service: Service, principal: ReviewPrincipal):
    with _coded_errors():
        undone, failed = await ledger.undo_group(db, service, group_id, principal)
        return {"undone": undone, "failed": failed}
