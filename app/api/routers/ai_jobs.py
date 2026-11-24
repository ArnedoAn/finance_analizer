"""The queue of heavy AI jobs: what waits for the valley, and the owner's decision for each one (see ``app.services.ai_jobs``).

Reads need ``read``; deciding needs ``review``. Errors are ``{"detail", "code"}``. Every decision is audited.
"""
import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.api.dependencies import READ, ReviewPrincipal, get_firefly, get_llm
from app.api.routers.common import DB
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.database import get_db_session
from app.services import ai_budget, ai_jobs, audit

logger = get_logger(__name__)
router = APIRouter()

_kick_task: asyncio.Task | None = None


async def _run_due_background() -> None:
    """Run the jobs that are due on a session of their own (tests replace this to run on theirs)."""
    async with get_db_session() as db:
        await ai_jobs.run_due(ai_jobs.JobContext(db, get_firefly(), get_llm(), get_settings()))


def kick() -> None:
    """Start the due jobs right now in the background instead of waiting for the next scheduler tick (at most one at a time)."""
    global _kick_task
    if _kick_task is None or _kick_task.done():
        _kick_task = asyncio.create_task(_run_due_background())


def _error(error: ai_jobs.JobError) -> HTTPException:
    return HTTPException(error.status, detail={"detail": error.detail, "code": error.code})


@router.get("/ai/jobs", dependencies=[READ])
async def list_jobs(
    db: DB, status: Annotated[str, Query(pattern="^(active|queued|waiting_offpeak|running|done|failed|cancelled|all)$")] = "active",
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
):
    jobs, total = await ai_jobs.listing(db, status, limit, offset)
    return {"items": [ai_jobs.public(job) for job in jobs], "total": total, "peak": ai_budget.peak_info()}


@router.post("/ai/jobs/run-all-now")
async def run_all_now(db: DB, principal: ReviewPrincipal):
    jobs = await ai_jobs.run_all_now(db)
    await audit.record(
        db, action="ai_job.run_all_now", target_type="ai_job", target_id="all", principal=principal,
        detail={"ids": [job.id for job in jobs]},
    )
    await db.commit()
    if jobs:
        kick()
    return {"queued": len(jobs)}


@router.post("/ai/jobs/{job_id}/run-now")
async def run_now(job_id: int, db: DB, principal: ReviewPrincipal):
    try:
        job = await ai_jobs.run_now(db, job_id)
    except ai_jobs.JobError as exc:
        raise _error(exc) from exc
    await audit.record(db, action="ai_job.run_now", target_type="ai_job", target_id=job.id, principal=principal, detail={"kind": job.kind})
    await db.commit()
    kick()
    return ai_jobs.public(job)


@router.post("/ai/jobs/{job_id}/wait")
async def wait_for_valley(job_id: int, db: DB, principal: ReviewPrincipal):
    try:
        job = await ai_jobs.wait(db, job_id)
    except ai_jobs.JobError as exc:
        raise _error(exc) from exc
    await audit.record(db, action="ai_job.wait", target_type="ai_job", target_id=job.id, principal=principal, detail={"kind": job.kind})
    await db.commit()
    return ai_jobs.public(job)


@router.post("/ai/jobs/{job_id}/cancel")
async def cancel(job_id: int, db: DB, principal: ReviewPrincipal):
    try:
        job = await ai_jobs.cancel(db, job_id)
    except ai_jobs.JobError as exc:
        raise _error(exc) from exc
    await audit.record(db, action="ai_job.cancel", target_type="ai_job", target_id=job.id, principal=principal, detail={"kind": job.kind})
    await db.commit()
    return ai_jobs.public(job)
