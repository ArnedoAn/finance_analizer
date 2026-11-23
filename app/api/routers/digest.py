"""Owner's digest preference: ``GET/PUT /settings/digest`` (``runtime_flags.digest_policy``, see ``app.services.digest``)."""
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import READ, ReviewPrincipal
from app.api.routers.common import DB
from app.services import audit, digest

router = APIRouter()


class DigestPolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daily: bool
    weekly: bool
    hour: int = Field(ge=0, le=23)
    # Days of silence before ``capture_gap``; omitted keeps the stored value.
    silence_days: int | None = Field(default=None, ge=1, le=30)


@router.get("/settings/digest", dependencies=[READ])
async def get_digest_policy(db: DB):
    return await digest.policy(db)


@router.put("/settings/digest")
async def put_digest_policy(body: DigestPolicyBody, principal: ReviewPrincipal, db: DB):
    previous = await digest.policy(db)
    current = await digest.set_policy(db, daily=body.daily, weekly=body.weekly, hour=body.hour, silence_days=body.silence_days)
    await audit.record(
        db, action="settings.digest", target_type="settings", target_id="digest", principal=principal,
        detail={**current, "previous": previous},
    )
    await db.commit()
    return current
