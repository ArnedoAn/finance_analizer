"""Utility bills (phase 17): list, register by hand, edit, mark paid, delete and the upcoming view for the cash flow.

Errors are ``{"detail", "code"}``. Reads need ``read``; every write needs ``review``.
"""
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from app.api.dependencies import ReadPrincipal, ReviewPrincipal
from app.api.routers.common import DB, Service
from app.core.config import get_settings
from app.services import bills
from app.services.bills import BillCreate, BillPatch

router = APIRouter()


class MarkPaid(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: int | None = None


def _http(exc: bills.BillError) -> HTTPException:
    return HTTPException(exc.status, detail={"detail": exc.detail, "code": exc.code})


@router.get("/bills")
async def list_bills(
    db: DB, principal: ReadPrincipal, status: Literal["pending", "paid", "overdue"] | None = None, month: str | None = None,
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
):
    if month is not None and not bills.valid_month(month):
        raise HTTPException(422, detail={"detail": "month debe ser AAAA-MM.", "code": "validation_error"})
    items, total = await bills.listing(db, status=status, month=month, limit=limit, offset=offset)
    return {"items": items, "total": total}


@router.get("/bills/upcoming")
async def upcoming_bills(db: DB, principal: ReadPrincipal, days: int = Query(30, ge=1, le=120)):
    return await bills.upcoming(db, days=days)


@router.post("/bills", status_code=201)
async def create_bill(body: BillCreate, db: DB, principal: ReviewPrincipal):
    try:
        bill, created = await bills.create_manual(db, body, principal, get_settings())
    except bills.BillError as exc:
        raise _http(exc) from exc
    if not created:
        raise HTTPException(409, detail={"detail": "Ya existe una factura de esa entidad con ese monto y esa fecha límite.", "code": "bill_exists"})
    return bills.bill_out(bill, bills.local_today())


@router.patch("/bills/{bill_id}")
async def patch_bill(bill_id: int, body: BillPatch, db: DB, principal: ReviewPrincipal):
    try:
        bill = await bills.update(db, await bills.get(db, bill_id), body, principal, get_settings())
    except bills.BillError as exc:
        raise _http(exc) from exc
    return bills.bill_out(bill, bills.local_today())


@router.post("/bills/{bill_id}/mark-paid")
async def mark_bill_paid(bill_id: int, body: MarkPaid, db: DB, service: Service, principal: ReviewPrincipal):
    try:
        bill = await bills.mark_paid(service, await bills.get(db, bill_id), body.item_id, principal)
    except bills.BillError as exc:
        raise _http(exc) from exc
    return bills.bill_out(bill, bills.local_today())


@router.delete("/bills/{bill_id}")
async def delete_bill(bill_id: int, db: DB, principal: ReviewPrincipal):
    try:
        await bills.delete_bill(db, await bills.get(db, bill_id), principal)
    except bills.BillError as exc:
        raise _http(exc) from exc
    return {"deleted": True}
