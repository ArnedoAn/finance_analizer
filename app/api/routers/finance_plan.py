"""Incomes, TRM and the plan of the month (finance plan contract, ``docs/contract-additions/DebtsIncomes.md``).

Reads need ``read``; writes ``review`` (``admin`` implies both).
"""
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.api.dependencies import READ, REVIEW, Principal
from app.api.routers.common import DB, Service
from app.services import audit, debts, finance_plan, fx, incomes

router = APIRouter()

_Pattern = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
_Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
_Amount = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)]
_Day = Annotated[int, Field(ge=1, le=31, strict=True)]


class IncomeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: _Name
    kind: Literal["fixed", "variable"]
    currency: Literal["COP", "USD"]
    amount: _Amount
    day_of_month: _Day | None = None
    match: list[_Pattern] = Field(default_factory=list, max_length=20)


class IncomePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: _Name | None = None
    kind: Literal["fixed", "variable"] | None = None
    currency: Literal["COP", "USD"] | None = None
    amount: _Amount | None = None
    day_of_month: _Day | None = None
    match: list[_Pattern] | None = Field(default=None, max_length=20)
    active: bool | None = None


class ThresholdIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_cop: Annotated[Decimal, Field(gt=0, le=incomes.MAX_THRESHOLD, max_digits=18, decimal_places=2)]


def _http(exc: incomes.IncomeError) -> HTTPException:
    return HTTPException(exc.status, detail={"detail": exc.detail, "code": exc.code})


@router.get("/incomes", dependencies=[READ])
async def list_incomes(db: DB):
    return {"items": [incomes.view(row) for row in await incomes.active(db)]}


@router.post("/incomes", status_code=201)
async def create_income(payload: IncomeIn, db: DB, principal: Annotated[Principal, REVIEW]):
    return await incomes.create(db, payload.model_dump(), principal)


@router.put("/incomes/{income_id}")
async def update_income(income_id: int, payload: IncomePatch, db: DB, principal: Annotated[Principal, REVIEW]):
    fields = payload.model_dump(exclude_unset=True)
    for name in ("name", "kind", "currency", "amount", "active"):
        if name in fields and fields[name] is None:
            raise HTTPException(422, detail={"detail": f"{name} no puede ser nulo.", "code": "validation_error"})
    try:
        return await incomes.update(db, income_id, fields, principal)
    except incomes.IncomeError as exc:
        raise _http(exc) from exc


@router.delete("/incomes/{income_id}")
async def delete_income(income_id: int, db: DB, principal: Annotated[Principal, REVIEW]):
    try:
        await incomes.deactivate(db, income_id, principal)
    except incomes.IncomeError as exc:
        raise _http(exc) from exc
    return {"deleted": True}


@router.get("/settings/extra-income", dependencies=[READ])
async def get_extra_income_threshold(db: DB):
    return {"amount_cop": debts.money(await incomes.threshold(db)), "default_cop": debts.money(incomes.DEFAULT_THRESHOLD)}


@router.put("/settings/extra-income")
async def set_extra_income_threshold(payload: ThresholdIn, db: DB, principal: Annotated[Principal, REVIEW]):
    amount = await incomes.set_threshold(db, payload.amount_cop)
    await audit.record(db, action="income.threshold", target_type="runtime_flag", target_id=incomes.THRESHOLD_KEY,
                       detail={"amount_cop": str(amount)}, principal=principal)
    await db.commit()
    return {"amount_cop": debts.money(amount), "default_cop": debts.money(incomes.DEFAULT_THRESHOLD)}


@router.get("/fx/trm", dependencies=[READ])
async def get_trm(db: DB, service: Service, date_: Annotated[date | None, Query(alias="date")] = None):
    day = date_ or debts.today(service.settings)
    found = await fx.quote(db, service.settings, day)
    await db.commit()  # a rate fetched just now is cached
    if found is None:
        raise HTTPException(503, detail={"detail": "No hay una TRM disponible todavía.", "code": "trm_unavailable"})
    return {"date": day.isoformat(), "rate": debts.money(found.rate), "source": fx.SOURCE, "fetched_at": found.fetched_at, "stale": found.stale}


@router.get("/plan/month", dependencies=[READ])
async def plan_month(db: DB, service: Service, month: Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None):
    plan = await finance_plan.month_plan(db, service.firefly, service.settings, month)
    await db.commit()  # the TRM it needed may have been fetched and cached
    return plan
