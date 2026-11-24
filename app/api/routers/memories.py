"""Assistant memory (see ``app.services.memories``): CRUD, rule proposals and the prompt context."""
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import READ, ReviewPrincipal
from app.api.routers.common import DB, Service
from app.core.logging import get_logger
from app.services import audit, memories
from app.services.memories import MemoryRejectedError

logger = get_logger(__name__)

router = APIRouter()


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=2000)
    kind: Literal["preference", "fact", "rule"]
    origin: Literal["owner", "assistant"] = "owner"


class MemoryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = Field(default=None, max_length=2000)
    kind: Literal["preference", "fact", "rule"] | None = None
    status: Literal["active", "archived"] | None = None


def _http(exc: MemoryRejectedError) -> HTTPException:
    return HTTPException(exc.status, detail={"detail": exc.detail, "code": exc.code})


async def _catalogs(service) -> tuple[list[str], list[str]]:
    """Firefly categories and budget names; a failing lookup gives an empty list (no rule can then be validated)."""
    try:
        categories = [str(name) for name in await service.categories()]
    except Exception as exc:
        logger.warning("memory_categories_failed", error_type=type(exc).__name__)
        categories = []
    try:
        budgets = [str(row["name"]) for row in await service.firefly.budgets()]
    except Exception as exc:
        logger.warning("memory_budgets_failed", error_type=type(exc).__name__)
        budgets = []
    return categories, budgets


async def _propose(db, service, row) -> None:
    categories, budgets = await _catalogs(service)
    proposal = await memories.propose_rule(db, service.llm, row, categories, budgets, service.settings.max_amount_cop)
    await audit.record(db, action="memory.propose_rule", target_type="memory", target_id=row.id,
                       detail={"rule_type": proposal["type"] if proposal else None})
    await db.commit()


@router.get("/memories", dependencies=[READ])
async def list_memories(
    db: DB, status: Literal["active", "archived", "all"] = "active", kind: Literal["preference", "fact", "rule"] | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100, offset: Annotated[int, Query(ge=0)] = 0,
):
    rows, total = await memories.listing(db, status=status, kind=kind, limit=limit, offset=offset)
    return {"items": [memories.memory_data(row) for row in rows], "total": total}


@router.get("/memories/context", dependencies=[READ])
async def memory_context(db: DB):
    result = await memories.context(db)
    await db.commit()  # persists last_used_at
    return result


@router.post("/memories", status_code=201)
async def create_memory(body: MemoryCreate, db: DB, service: Service, principal: ReviewPrincipal):
    try:
        row = await memories.create(db, text=body.text, kind=body.kind, origin=body.origin, principal=principal)
    except MemoryRejectedError as exc:
        raise _http(exc) from exc
    await db.commit()
    if row.kind != "fact":
        await _propose(db, service, row)
    return memories.memory_data(row)


@router.patch("/memories/{memory_id}")
async def patch_memory(memory_id: int, body: MemoryPatch, db: DB, service: Service, principal: ReviewPrincipal):
    try:
        row = await memories.get(db, memory_id)
        previous = row.text
        await memories.update_memory(db, row, text=body.text, kind=body.kind, status=body.status, principal=principal)
    except MemoryRejectedError as exc:
        await db.rollback()
        raise _http(exc) from exc
    await db.commit()
    if row.text != previous and row.kind != "fact" and row.status == "active":
        await _propose(db, service, row)
    return memories.memory_data(row)


@router.delete("/memories/{memory_id}")
async def delete_memory(memory_id: int, db: DB, principal: ReviewPrincipal):
    try:
        row = await memories.get(db, memory_id)
    except MemoryRejectedError as exc:
        raise _http(exc) from exc
    await memories.delete(db, row, principal)
    await db.commit()
    return {"deleted": True}


@router.post("/memories/{memory_id}/propose-rule")
async def propose_memory_rule(memory_id: int, db: DB, service: Service, principal: ReviewPrincipal):
    """Ask the model again for a rule (after a model failure or a changed catalog); never replaces an applied rule."""
    try:
        row = await memories.get(db, memory_id)
    except MemoryRejectedError as exc:
        raise _http(exc) from exc
    if row.rule_ref:
        raise _http(MemoryRejectedError(409, "rule_already_applied", "La regla de esta memoria ya se creó."))
    if row.status != "active":
        raise _http(MemoryRejectedError(409, "memory_archived", "La memoria está archivada."))
    await _propose(db, service, row)
    return memories.memory_data(row)


@router.post("/memories/{memory_id}/apply-rule")
async def apply_memory_rule(memory_id: int, db: DB, service: Service, principal: ReviewPrincipal):
    try:
        row = await memories.get(db, memory_id)
        categories, budgets = await _catalogs(service)
        rule = await memories.apply_rule(db, row, categories, budgets, principal)
    except MemoryRejectedError as exc:
        await db.rollback()
        raise _http(exc) from exc
    except ValueError as exc:
        await db.rollback()
        raise _http(MemoryRejectedError(422, "invalid_rule", str(exc))) from exc
    await db.commit()
    return {"memory": memories.memory_data(row), "rule": rule}
