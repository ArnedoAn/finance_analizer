"""AI usage and the budget policy for automatic AI jobs (Phase AI review; see ``app.services.ai_budget``)."""
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.dependencies import READ, ReviewPrincipal
from app.api.routers.common import DB
from app.services import ai_budget, audit

router = APIRouter()


class AiBudgetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daily_calls: int | None = Field(default=None, ge=0, le=ai_budget.MAX_DAILY_CALLS)
    peak_policy: Literal["ask", "wait", "run"] | None = None

    @model_validator(mode="after")
    def something_to_change(self):
        if self.daily_calls is None and self.peak_policy is None:
            raise ValueError("Indica daily_calls o peak_policy.")
        return self


def _policy_body(policy: dict) -> dict:
    return {
        "daily_calls": policy["daily_calls"],
        "peak_policy": policy["peak_policy"],
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
    current = await ai_budget.set_policy(db, body.daily_calls, body.peak_policy)
    detail = {"daily_calls": current["daily_calls"], "previous": previous["daily_calls"]}
    if body.peak_policy is not None:
        detail.update({"peak_policy": current["peak_policy"], "previous_peak_policy": previous["peak_policy"]})
    await audit.record(
        db, action="settings.ai_budget", target_type="settings", target_id="ai_budget", principal=principal, detail=detail,
    )
    await db.commit()
    return _policy_body(current)
