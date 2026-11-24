"""What-if simulator: ``POST /simulate`` (review). See ``app.services.simulator``."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import ReviewPrincipal
from app.api.routers.common import DB, Service
from app.services import simulator

router = APIRouter()


class SimulateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=simulator.MAX_QUESTION_CHARS)


@router.post("/simulate")
async def simulate(body: SimulateBody, db: DB, service: Service, principal: ReviewPrincipal):
    try:
        result = await simulator.simulate(db, service.firefly, service.llm, service.settings, body.question)
    except simulator.SimulationError as exc:
        await db.commit()  # keeps the usage record of the calls already made
        raise HTTPException(exc.status, detail={"detail": exc.detail, "code": exc.code}) from exc
    await db.commit()  # persists the usage record
    return result
