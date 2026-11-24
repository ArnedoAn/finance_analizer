"""Receipts by photo (phase 15): upload an image, read it with a vision model, match it and split it by category.

Errors are ``{"detail", "code"}``. Reads need ``read``; upload, apply and delete need ``review``. The upload is a large
body: Caddy must allow 8 MB for ``POST /api/v1/receipts`` (see ``docs/contract-additions/ServerCapture.md``). The image is
handed to a background task and never stored.
"""
import re
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict

from app.api.dependencies import ReadPrincipal, ReviewPrincipal, get_firefly
from app.api.routers.common import DB, Service
from app.core.logging import get_logger
from app.db.database import get_db_session
from app.services import image_clean, receipts

logger = get_logger(__name__)
router = APIRouter()


class ApplyReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    split: bool = False
    create_cash: bool = False
    item_id: int | None = None


def _http(exc: receipts.ReceiptError) -> HTTPException:
    return HTTPException(exc.status, detail={"detail": exc.detail, "code": exc.code})


async def process_background(receipt_id: int, image: bytes, mime: str) -> None:
    try:
        async with get_db_session() as db:
            await receipts.process(db, get_firefly(), receipt_id, image, mime)
    except Exception as exc:  # never include the exception text: it could echo receipt content
        logger.error("receipt_background_failed", receipt_id=receipt_id, error_type=type(exc).__name__)


@router.post("/receipts", status_code=202)
async def upload_receipt(
    background: BackgroundTasks, db: DB, principal: ReviewPrincipal,
    file: UploadFile = File(...), item_id: str | None = Form(None),
):
    if item_id is not None and item_id.strip() and not re.fullmatch(r"\d{1,12}", item_id.strip()):
        raise HTTPException(422, detail={"detail": "item_id debe ser un entero.", "code": "validation_error"})
    linked = int(item_id) if item_id and item_id.strip() else None
    data = await file.read(image_clean.MAX_IMAGE_BYTES + 1)
    try:
        receipt, run, clean, mime = await receipts.register(db, data, linked, principal)
    except receipts.ReceiptError as exc:
        raise _http(exc) from exc
    if run:
        background.add_task(process_background, receipt.id, clean, mime)
    return {"id": receipt.id, "status": receipt.status}


@router.get("/receipts")
async def list_receipts(
    db: DB, principal: ReadPrincipal, status: Literal["processing", "ready", "needs_review", "failed"] | None = None,
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
):
    items, total = await receipts.listing(db, status=status, limit=limit, offset=offset)
    return {"items": items, "total": total}


@router.get("/receipts/{receipt_id}")
async def get_receipt(receipt_id: int, db: DB, principal: ReadPrincipal):
    try:
        return await receipts.detail(db, await receipts.get(db, receipt_id))
    except receipts.ReceiptError as exc:
        raise _http(exc) from exc


@router.post("/receipts/{receipt_id}/apply")
async def apply_receipt(receipt_id: int, body: ApplyReceipt, db: DB, service: Service, principal: ReviewPrincipal):
    try:
        receipt = await receipts.get(db, receipt_id)
        return await receipts.apply(
            db, service, receipt, principal, split=body.split, create_cash=body.create_cash, item_id=body.item_id,
        )
    except receipts.ReceiptError as exc:
        raise _http(exc) from exc


@router.delete("/receipts/{receipt_id}")
async def delete_receipt(receipt_id: int, db: DB, principal: ReviewPrincipal):
    try:
        await receipts.delete_receipt(db, await receipts.get(db, receipt_id), principal)
    except receipts.ReceiptError as exc:
        raise _http(exc) from exc
    return {"deleted": True}
