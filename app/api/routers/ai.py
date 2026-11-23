"""AI usage and the budget policy for automatic AI jobs (Phase AI review; see ``app.services.ai_budget``)."""
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import READ, ReviewPrincipal
from app.api.routers.common import DB
from app.services import ai_budget, audit

router = APIRouter()


class AiBudgetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daily_calls: int = Field(ge=0, le=ai_budget.MAX_DAILY_CALLS)


def _policy_body(policy: dict) -> dict:
    return {
        "daily_calls": policy["daily_calls"],
        "rolling_stop_percent": ai_budget.ROLLING_STOP_PERCENT,
        "weekly_stop_percent": ai_budget.WEEKLY_STOP_PERCENT,
    }


@router.get("/ai/usage", dependencies=[READ])
async def ai_usage(db: DB):
    report = await ai_budget.usage_report(db)
    await db.commit()  # persists a refreshed Go usage
    return report


@router.get("/settings/ai-budget", dependencies=[READ])
async def get_ai_budget(db: DB):
    return _policy_body(await ai_budget.policy(db))


@router.put("/settings/ai-budget")
async def put_ai_budget(body: AiBudgetBody, db: DB, principal: ReviewPrincipal):
    previous = await ai_budget.policy(db)
    current = await ai_budget.set_policy(db, body.daily_calls)
    await audit.record(
        db, action="settings.ai_budget", target_type="settings", target_id="ai_budget", principal=principal,
        detail={"daily_calls": current["daily_calls"], "previous": previous["daily_calls"]},
    )
    await db.commit()
    return _policy_body(current)
