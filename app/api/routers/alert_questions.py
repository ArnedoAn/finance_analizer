"""Answering question alerts and the policy that limits them (see ``app.services.alert_questions``)."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import READ, ReviewPrincipal
from app.api.routers.common import DB, Service
from app.services import alert_questions, alerts, audit, own_accounts

router = APIRouter()


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: str = Field(min_length=1, max_length=40)


class QuestionPolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daily_limit: int = Field(ge=0, le=alert_questions.MAX_DAILY_LIMIT)
    shared_accounts: list[str] = Field(default_factory=list, max_length=20)


async def _policy_body(db, service) -> dict:
    return {**await alert_questions.policy(db), "asked_today": await alert_questions.asked_today(db, service.settings)}


@router.post("/alerts/{alert_id}/answer")
async def answer_alert(alert_id: int, body: AnswerBody, db: DB, service: Service, principal: ReviewPrincipal):
    try:
        alert, result = await alert_questions.answer(db, service, principal, alert_id, body.choice)
    except alert_questions.QuestionError as exc:
        raise HTTPException(exc.status, detail={"detail": exc.detail, "code": exc.code}) from exc
    data = alerts.alert_data(alert)
    await alert_questions.attach(db, [data])
    return {"alert": data, "choice": body.choice, "result": result}


@router.get("/settings/questions", dependencies=[READ])
async def get_question_policy(db: DB, service: Service):
    return await _policy_body(db, service)


@router.put("/settings/questions")
async def put_question_policy(body: QuestionPolicyBody, db: DB, service: Service, principal: ReviewPrincipal):
    known = {account.firefly_account.casefold(): account.firefly_account for account in await own_accounts.load(db)}
    chosen = []
    for name in body.shared_accounts:
        canonical = known.get(name.strip().casefold())
        if canonical is None:
            raise HTTPException(422, detail={"detail": f"La cuenta propia «{name}» no existe.", "code": "unknown_account"})
        if canonical not in chosen:
            chosen.append(canonical)
    previous = await alert_questions.policy(db)
    await alert_questions.set_policy(db, daily_limit=body.daily_limit, shared_accounts=chosen)
    await audit.record(db, action="settings.questions", target_type="settings", target_id="alert_questions", principal=principal,
                       detail={"daily_limit": body.daily_limit, "shared_accounts": chosen, "previous": previous})
    await db.commit()
    return await _policy_body(db, service)
