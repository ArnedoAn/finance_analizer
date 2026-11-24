"""Merchants, rules, transfer pairing and anomalies (contract sections 7 and 8). Owner: Phase 5 (A6)."""
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.api.dependencies import READ, ReviewPrincipal
from app.api.routers.common import DB, Service, item_data
from app.db.models_rules import Merchant, Rule
from app.parsers.common import fold
from app.services import ai_anomalies, anomalies, categories, rules, transfers

# Suggested starting categories (the AI only picks from the owner's Firefly categories), in display order.
DEFAULT_CATEGORIES = (
    "Mercado", "Comida", "Transporte", "Servicios", "Suscripciones", "Arriendo", "Salud", "Ingresos", "Compras",
    "Entretenimiento", "Educación", "Hogar", "Viajes", "Cuidado personal", "Mascotas", "Comisiones e impuestos",
    "Regalos y donaciones",
)
router = APIRouter()


class MerchantUpdate(BaseModel):
    default_category: str = Field(min_length=1, max_length=255)
    create_rule: bool = True


class RuleCreate(BaseModel):
    match: Literal["merchant_exact", "merchant_contains", "text_contains"]
    pattern: str = Field(min_length=1, max_length=255)
    category: str = Field(min_length=1, max_length=255)
    merchant: str | None = Field(default=None, max_length=255)
    tags: list[str] | None = Field(default=None, max_length=10)


class TransferPair(BaseModel):
    out_id: int
    in_id: int


class CategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: categories.CategoryName


async def _known_category(service, value: str) -> str:
    """Return the Firefly spelling of ``value`` or raise 422 (categories live in Firefly)."""
    wanted = value.strip().casefold()
    for name in await service.categories():
        if name.casefold() == wanted:
            return name
    raise HTTPException(422, "Unknown category")


async def _audit(db, principal, action, target_type, target_id, detail):
    from app.services import audit

    await audit.record(db, action=action, target_type=target_type, target_id=target_id, detail=detail, principal=principal)


@router.get("/merchants", dependencies=[READ])
async def list_merchants(db: DB, q: str | None = None, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    query = select(Merchant)
    if q:
        query = query.where(func.lower(Merchant.name).contains(q.strip().lower(), autoescape=True))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    page = await db.scalars(query.order_by(Merchant.count.desc(), Merchant.name).limit(limit).offset(offset))
    return {"items": [rules.merchant_data(merchant) for merchant in page], "total": total}


@router.put("/merchants/{merchant_id}")
async def update_merchant(merchant_id: int, body: MerchantUpdate, db: DB, service: Service, principal: ReviewPrincipal):
    merchant = await db.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(404, "Merchant not found")
    category = await _known_category(service, body.default_category)
    merchant.default_category = category
    rule = None
    if body.create_rule:
        rule = await rules.create_rule(db, match="merchant_exact", pattern=merchant.name, category=category, merchant=merchant.name)
    result = {"merchant": rules.merchant_data(merchant), "rule": rules.rule_data(rule) if rule else None}
    await _audit(db, principal, "merchant.update", "merchant", merchant.id, {"default_category": category, "rule_id": rule.id if rule else None})
    await db.commit()
    return result


@router.get("/rules", dependencies=[READ])
async def list_rules(db: DB):
    found = (await db.scalars(select(Rule).where(Rule.active.is_(True)).order_by(Rule.id))).all()
    return {"items": [rules.rule_data(rule) for rule in found], "total": len(found)}


@router.post("/rules", status_code=201)
async def create_rule(body: RuleCreate, db: DB, service: Service, principal: ReviewPrincipal):
    try:
        return await rules.add_for_owner(db, service, match=body.match, pattern=body.pattern, category=body.category, merchant=body.merchant, tags=body.tags, principal=principal)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/categories")
async def create_category(body: CategoryCreate, response: Response, db: DB, service: Service, principal: ReviewPrincipal):
    """Create a Firefly category the owner explicitly chose; a name that exists (any case or accents) is returned as is."""
    name, created = await categories.create(db, service, body.name, principal)
    if created:
        response.status_code = 201
    return {"name": name, "created": created}


@router.post("/categories/defaults")
async def create_default_categories(db: DB, service: Service, principal: ReviewPrincipal):
    """Create the suggested categories Firefly lacks; idempotent, and a failure midway keeps what was created."""
    known = {fold(name): name for name in await service.categories()}
    created, existing = [], []
    try:
        for name in DEFAULT_CATEGORIES:
            if fold(name) in known:
                existing.append(known[fold(name)])
                continue
            created.append(await service.firefly.create_category(name))
    finally:
        if created:
            service.invalidate_categories()
            await _audit(db, principal, "category.defaults", "category", "defaults", {"created": created})
            await db.commit()
    return {"created": created, "existing": existing}


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int, db: DB, principal: ReviewPrincipal):
    if await rules.remove_for_owner(db, rule_id, principal) is None:
        raise HTTPException(404, "Rule not found")
    return {"deleted": True}


@router.get("/transfers/pending", dependencies=[READ])
async def pending_transfers(db: DB, service: Service):
    pairs = await transfers.pending(db, service.settings)
    return {"items": [
        {"out_item": item_data(pair["out_item"]), "in_item": item_data(pair["in_item"]),
         "amount": f"{pair['amount']:.2f}", "currency": pair["currency"], "minutes_apart": pair["minutes_apart"]}
        for pair in pairs
    ]}


@router.post("/transfers/pair")
async def pair_transfer(body: TransferPair, db: DB, service: Service, principal: ReviewPrincipal):
    try:
        out, incoming = await transfers.pair(service, body.out_id, body.in_id, principal=principal)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except transfers.PairConflictError as exc:
        raise HTTPException(409, {"detail": str(exc), "code": "conflict"}) from exc
    except ValueError as exc:  # includes PermanentError (unknown Firefly account)
        raise HTTPException(422, str(exc)) from exc
    result = {"out_item": item_data(out), "in_item": item_data(incoming)}
    await _audit(db, principal, "transfer.pair", "inbox_item", out.id, {"in_id": incoming.id})
    await db.commit()
    return result


@router.get("/anomalies", dependencies=[READ])
async def list_anomalies(db: DB, service: Service, since: datetime | None = None):
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return {"items": await ai_anomalies.attach(db, await anomalies.detect(db, service.settings, since=since))}
