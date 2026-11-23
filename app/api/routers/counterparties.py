"""Consolidate counterparties: suggestions, merge and 7-day undo (see ``app.services.counterparties``)."""
from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.api.dependencies import READ, ReviewPrincipal
from app.api.routers.common import DB, Service
from app.services import counterparties

router = APIRouter()

# Firefly ids are strings; a client may send them as JSON numbers.
AccountId = Annotated[str, BeforeValidator(lambda value: str(value) if isinstance(value, int) and not isinstance(value, bool) else value), Field(min_length=1, max_length=100)]


class MergeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: AccountId
    source_ids: list[AccountId] = Field(min_length=1, max_length=counterparties.MAX_SOURCES)


def _error(exc: counterparties.CounterpartyError) -> HTTPException:
    return HTTPException(exc.status, detail={"detail": exc.detail, "code": exc.code})


@router.get("/counterparties/merge-suggestions", dependencies=[READ])
async def merge_suggestions(db: DB, service: Service):
    result = await counterparties.suggestions(db, service.firefly, service.llm)
    await db.commit()  # LLM usage and verdict cache
    return result


@router.post("/counterparties/merge")
async def merge(body: MergeBody, db: DB, service: Service, principal: ReviewPrincipal):
    try:
        record = await counterparties.merge(db, service.firefly, principal, body.target_id, body.source_ids)
    except counterparties.CounterpartyError as exc:
        raise _error(exc) from exc
    return counterparties.merge_data(record)


@router.post("/counterparties/merge/{merge_id}/undo")
async def undo_merge(merge_id: int, db: DB, service: Service, principal: ReviewPrincipal):
    try:
        record, failed = await counterparties.undo(db, service.firefly, principal, merge_id)
    except counterparties.CounterpartyError as exc:
        raise _error(exc) from exc
    return {**counterparties.merge_data(record), "failed": failed}
