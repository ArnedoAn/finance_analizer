"""AI functions approved by the owner: natural-language search, narrated report, budget proposals, category/tag proposals,
subscription verdicts and the switches that govern them. Reads need ``read``; everything that writes or spends needs ``review``.

Errors are ``{"detail", "code"}``. The owner's own requests (search, narrative on demand) only record their LLM calls in
``ai_budget``; the automatic jobs live in ``app.services.ai_insights`` and are braked by it.
"""
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import READ, ReviewPrincipal
from app.api.routers.common import DB, Service
from app.services import (
    ai_categorize,
    ai_features,
    ai_narrative,
    ai_search,
    ai_subscriptions,
    audit,
    budget_suggest,
    snapshots,
)

router = APIRouter()


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str = Field(min_length=1, max_length=ai_search.MAX_QUESTION_CHARS)


class Switches(BaseModel):
    model_config = ConfigDict(extra="forbid")
    categorize: bool | None = None
    narrative: bool | None = None
    anomalies: bool | None = None
    subscriptions: bool | None = None
    tags: bool | None = None
    month_close: bool | None = None
    coach: bool | None = None


class SecondOpinionBody(BaseModel):
    """Partial update of the second opinion; ``verified_max_amount_cop: null`` goes back to twice the normal auto-approval cap."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    model: str | None = Field(default=None, max_length=100)
    min_amount_cop: Decimal | None = Field(default=None, ge=0, le=ai_features.MAX_SECOND_OPINION_AMOUNT, decimal_places=2)
    verified_max_amount_cop: Decimal | None = Field(default=None, gt=0, le=ai_features.MAX_SECOND_OPINION_AMOUNT, decimal_places=2)


class FeaturesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    features: Switches | None = None
    confidence_threshold: float | None = Field(default=None, ge=ai_features.MIN_THRESHOLD, le=1)
    tags: list[str] | None = Field(default=None, max_length=ai_features.MAX_VOCABULARY)
    second_opinion: SecondOpinionBody | None = None


class ApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[int] = Field(default_factory=list, max_length=200)
    all_above: float | None = Field(default=None, ge=ai_features.MIN_THRESHOLD, le=1)


def _error(status: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status, detail={"detail": detail, "code": code})


# --- Search ---------------------------------------------------------------------------------------------------------------


@router.post("/search/parse", dependencies=[READ])
async def parse_search(body: SearchBody, db: DB, service: Service):
    try:
        result = await ai_search.parse(db, service.firefly, service.llm, service.settings, body.q)
    except ValueError as exc:
        raise _error(422, "validation_error", str(exc)) from exc
    except RuntimeError as exc:
        await db.commit()  # keeps the usage record of the failed call
        raise _error(502, "llm_unavailable", "El modelo no está disponible; intenta de nuevo.") from exc
    await db.commit()  # persists the usage record
    return result


# --- Narrated monthly report ----------------------------------------------------------------------------------------------


@router.get("/reports/narrative", dependencies=[READ])
async def get_narrative(db: DB, month: Annotated[str | None, Query()] = None):
    if month is not None and not snapshots.valid_month(month):
        raise _error(422, "invalid_month", "El mes debe tener el formato AAAA-MM.")
    row = await ai_narrative.get(db, month)
    if row is None:
        raise _error(404, "narrative_not_found", "Todavía no hay informe narrado para ese mes.")
    return ai_narrative.public(row)


@router.post("/reports/narrative")
async def create_narrative(db: DB, service: Service, principal: ReviewPrincipal, month: Annotated[str | None, Query()] = None):
    month = month or ai_narrative.previous_month(service.settings)
    try:
        row = await ai_narrative.generate(db, service.firefly, service.llm, service.settings, month, auto=False, source="owner")
    except ai_narrative.NarrativeError as exc:
        await db.commit()  # keeps the usage record of a call whose answer was unusable
        raise _error(exc.status, exc.code, exc.detail) from exc
    await audit.record(db, action="ai_narrative.generate", target_type="ai_narrative", target_id=month, principal=principal, detail={"month": month})
    await db.commit()
    return ai_narrative.public(row)


# --- Budget proposals -----------------------------------------------------------------------------------------------------


@router.post("/budgets/suggest")
async def suggest_budgets(db: DB, service: Service, principal: ReviewPrincipal):
    return await budget_suggest.suggest(db, service.firefly, service.settings)


# --- Switches -------------------------------------------------------------------------------------------------------------


@router.get("/settings/ai-features", dependencies=[READ])
async def get_ai_features(db: DB):
    return await ai_features.get(db)


@router.put("/settings/ai-features")
async def put_ai_features(body: FeaturesBody, db: DB, principal: ReviewPrincipal):
    try:
        state = await ai_features.update(
            db, features=body.features.model_dump(exclude_none=True) if body.features else None,
            confidence_threshold=body.confidence_threshold, tags=body.tags,
            second_opinion=body.second_opinion.model_dump(exclude_unset=True) if body.second_opinion else None,
        )
    except ai_features.SecondOpinionError as exc:
        raise _error(422, "invalid_second_opinion", str(exc)) from exc
    except ValueError as exc:
        raise _error(422, "invalid_tags", str(exc)) from exc
    await audit.record(
        db, action="settings.ai_features", target_type="settings", target_id="ai_features", principal=principal,
        detail={
            "features": state["features"], "confidence_threshold": state["confidence_threshold"], "tags": state["tags"],
            "second_opinion": state["second_opinion"],
        },
    )
    await db.commit()
    return state


# --- Category and tag proposals -------------------------------------------------------------------------------------------


@router.get("/ai/category-suggestions", dependencies=[READ])
async def list_category_suggestions(
    db: DB, status: Annotated[str, Query(pattern="^(pending|auto_applied|applied|discarded|stale|none|error|all)$")] = "pending",
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
):
    items, total = await ai_categorize.listing(db, status=status, limit=limit, offset=offset)
    await db.commit()  # persists proposals that became stale
    return {"items": items, "total": total}


@router.post("/ai/category-suggestions/apply")
async def apply_category_suggestions(body: ApplyBody, db: DB, service: Service, principal: ReviewPrincipal):
    if not body.ids and body.all_above is None:
        raise _error(422, "validation_error", "Indica ids o all_above.")
    applied, failed = await ai_categorize.apply(db, service, principal, ids=body.ids, all_above=body.all_above)
    return {"applied": applied, "failed": failed}


@router.post("/ai/category-suggestions/{suggestion_id}/discard")
async def discard_category_suggestion(suggestion_id: int, db: DB, principal: ReviewPrincipal):
    try:
        row = await ai_categorize.discard(db, suggestion_id, principal)
    except ai_categorize.ApplyFailedError as exc:
        raise _error(409, "suggestion_not_pending", str(exc)) from exc
    if row is None:
        raise _error(404, "suggestion_not_found", "La sugerencia no existe.")
    return {"id": row.id, "status": row.status}


# --- Subscriptions --------------------------------------------------------------------------------------------------------


@router.get("/ai/subscriptions", dependencies=[READ])
async def list_ai_subscriptions(db: DB, service: Service):
    return {"items": await ai_subscriptions.listing(db, service.settings)}
