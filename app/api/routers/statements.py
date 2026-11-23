"""Bank statement reconciliation (phase 9, experimental): upload a PDF, compare it with the ledger, import what is missing.

Errors are ``{"detail", "code"}``. Reads need ``read``; upload, import and delete need ``review``. The upload is the
only route with a large body: Caddy must allow 12 MB for ``POST /api/v1/statements`` (see
``docs/contract-additions/ServerStatements.md``). The PDF and its password are handed to a background task and never stored.
"""
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import ReadPrincipal, ReviewPrincipal, get_firefly, get_llm
from app.api.routers.common import DB, Service
from app.core.logging import get_logger
from app.db.database import get_db_session
from app.db.repositories import OwnAccountRepository
from app.services import pdf_text, statements

logger = get_logger(__name__)
router = APIRouter()


class ImportMissing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_ids: list[int] = Field(min_length=1, max_length=500)
    confirm_unbalanced: bool = False


def _http(exc: statements.StatementError) -> HTTPException:
    return HTTPException(exc.status, detail={"detail": exc.detail, "code": exc.code})


async def process_background(statement_id: int, data: bytes, password: str | None) -> None:
    try:
        async with get_db_session() as db:
            await statements.process(db, get_firefly(), get_llm(), statement_id, data, password)
    except Exception as exc:  # never include the exception text: it could echo statement content
        logger.error("statement_background_failed", statement_id=statement_id, error_type=type(exc).__name__)


@router.post("/statements", status_code=202)
async def upload_statement(
    background: BackgroundTasks, db: DB, principal: ReviewPrincipal,
    file: UploadFile = File(...), own_account_id: int = Form(...), password: str | None = Form(None, max_length=256),
):
    account = await OwnAccountRepository(db).get(own_account_id)
    if account is None:
        raise HTTPException(404, detail={"detail": "Cuenta propia no encontrada.", "code": "own_account_not_found"})
    data = await file.read(pdf_text.MAX_PDF_BYTES + 1)
    try:
        statement, run = await statements.register(db, account, data, principal)
    except statements.StatementError as exc:
        raise _http(exc) from exc
    if run:
        background.add_task(process_background, statement.id, data, password or None)
    return {"id": statement.id, "status": statement.status}


@router.get("/statements")
async def list_statements(db: DB, principal: ReadPrincipal, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    items, total = await statements.listing(db, limit=limit, offset=offset)
    return {"items": items, "total": total}


@router.get("/statements/{statement_id}")
async def get_statement(statement_id: int, db: DB, principal: ReadPrincipal):
    try:
        return await statements.detail(db, await statements.get(db, statement_id))
    except statements.StatementError as exc:
        raise _http(exc) from exc


@router.post("/statements/{statement_id}/import-missing")
async def import_missing(statement_id: int, body: ImportMissing, db: DB, service: Service, principal: ReviewPrincipal):
    try:
        statement = await statements.get(db, statement_id)
        return await statements.import_missing(
            db, service, statement, body.row_ids, principal, confirm_unbalanced=body.confirm_unbalanced,
        )
    except statements.StatementError as exc:
        raise _http(exc) from exc


@router.delete("/statements/{statement_id}")
async def delete_statement(statement_id: int, db: DB, principal: ReviewPrincipal):
    try:
        await statements.delete_statement(db, await statements.get(db, statement_id), principal)
    except statements.StatementError as exc:
        raise _http(exc) from exc
    return {"deleted": True}
