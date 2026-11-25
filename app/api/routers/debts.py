"""Debts (finance plan contract, ``docs/contract-additions/DebtsIncomes.md``): CRUD over Firefly liabilities, schedules and payoff plans.

Reads need ``read``; writes ``review`` (``admin`` implies both). Money travels as decimal strings; errors are
``{detail, code}``. Firefly owns the balance; the analyzer never deletes a liability in Firefly.
"""
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import READ, REVIEW, Principal
from app.api.routers.common import DB, Service
from app.services import debts

router = APIRouter()

_Money = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=2)]


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extra_monthly: _Money = Decimal(0)
    extra_once: _Money | None = None
    strategy: Literal["avalanche", "snowball", "both"] = "both"


def _http(exc: debts.DebtError) -> HTTPException:
    return HTTPException(exc.status, detail={"detail": exc.detail, "code": exc.code})


@router.get("/debts", dependencies=[READ])
async def list_debts(db: DB, service: Service):
    try:
        return await debts.listing(db, service.firefly, service.settings)
    except debts.DebtError as exc:
        raise _http(exc) from exc


@router.post("/debts/plan", dependencies=[READ])
async def plan_debts(payload: PlanIn, db: DB, service: Service):
    try:
        return await debts.plan(
            db, service.firefly, service.settings, extra_monthly=payload.extra_monthly,
            extra_once=payload.extra_once or Decimal(0), strategy=payload.strategy,
        )
    except debts.DebtError as exc:
        raise _http(exc) from exc


@router.post("/debts", status_code=201)
async def create_debt(payload: debts.DebtIn, db: DB, service: Service, principal: Annotated[Principal, REVIEW]):
    try:
        return await debts.create(db, service.firefly, service.settings, payload.model_dump(), principal)
    except debts.DebtError as exc:
        raise _http(exc) from exc


@router.put("/debts/{debt_id}")
async def update_debt(debt_id: int, payload: debts.DebtPatch, db: DB, service: Service, principal: Annotated[Principal, REVIEW]):
    fields = payload.model_dump(exclude_unset=True)
    for name in debts.NOT_NULL:
        if name in fields and fields[name] is None:
            raise HTTPException(422, detail={"detail": f"{name} no puede ser nulo.", "code": "validation_error"})
    try:
        return await debts.update(db, service.firefly, service.settings, debt_id, fields, principal)
    except debts.DebtError as exc:
        raise _http(exc) from exc


@router.delete("/debts/{debt_id}")
async def delete_debt(debt_id: int, db: DB, principal: Annotated[Principal, REVIEW]):
    try:
        await debts.deactivate(db, debt_id, principal)
    except debts.DebtError as exc:
        raise _http(exc) from exc
    return {"deleted": True}


@router.get("/debts/{debt_id}/schedule", dependencies=[READ])
async def debt_schedule(
    debt_id: int, db: DB, service: Service,
    extra_monthly: Annotated[Decimal, Query(ge=0, max_digits=18, decimal_places=2)] = Decimal(0),
    extra_once: Annotated[Decimal, Query(ge=0, max_digits=18, decimal_places=2)] = Decimal(0),
    extra_once_month: Annotated[int, Query(ge=1, le=600)] = 1,
    mode: Literal["term", "installment"] = "term",
):
    try:
        return await debts.schedule(
            db, service.firefly, service.settings, debt_id, extra_monthly=extra_monthly, extra_once=extra_once,
            extra_once_month=extra_once_month, mode=mode,
        )
    except debts.DebtError as exc:
        raise _http(exc) from exc
