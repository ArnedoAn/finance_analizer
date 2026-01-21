"""
Scheduler API Endpoints

Endpoints for managing scheduled tasks and viewing job history.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db_session
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.repositories import SchedulerJobLogRepository
from app.models.schemas import SchedulerJobLogResponse, SchedulerJobStatus
from app.services.scheduler import SchedulerService, describe_cron, get_scheduler

logger = get_logger(__name__)
router = APIRouter(prefix="/scheduler", tags=["Scheduler"])


@router.get(
    "/status",
    response_model=dict[str, Any],
    summary="Get scheduler status",
    description="Get the current status of the scheduler and all jobs.",
)
async def get_scheduler_status() -> dict[str, Any]:
    """Get scheduler status."""
    settings = get_settings()
    scheduler = get_scheduler()
    service = SchedulerService()
    
    return {
        "enabled": settings.scheduler_enabled,
        "running": scheduler.running,
        "jobs": service.get_jobs_status(),
        "config": {
            "processing_cron": settings.scheduler_processing_cron,
            "processing_description": describe_cron(settings.scheduler_processing_cron),
            "learning_cron": settings.scheduler_learning_cron,
            "learning_description": describe_cron(settings.scheduler_learning_cron),
        },
    }


@router.post(
    "/jobs/{job_id}/trigger",
    response_model=dict[str, Any],
    summary="Trigger job manually",
    description="Manually trigger a scheduled job to run immediately.",
)
async def trigger_job(
    job_id: str,
) -> dict[str, Any]:
    """Trigger a job manually."""
    if job_id not in ("email_processing", "sender_learning"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown job: {job_id}. Valid jobs: email_processing, sender_learning",
        )
    
    service = SchedulerService()
    result = await service.trigger_job_now(job_id)
    
    if "error" in result:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result["error"],
        )
    
    return result


@router.get(
    "/logs",
    response_model=list[SchedulerJobLogResponse],
    summary="Get job logs",
    description="Get recent scheduler job execution logs.",
)
async def get_job_logs(
    limit: int = 50,
    job_type: str | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    """Get recent job logs."""
    repo = SchedulerJobLogRepository(session)
    logs = await repo.get_recent(limit=limit, job_type=job_type)
    
    return [
        {
            "id": log.id,
            "job_name": log.job_name,
            "job_type": log.job_type,
            "status": log.status,
            "started_at": log.started_at,
            "completed_at": log.completed_at,
            "emails_processed": log.emails_processed,
            "transactions_created": log.transactions_created,
            "senders_learned": log.senders_learned,
            "error_message": log.error_message,
        }
        for log in logs
    ]


@router.get(
    "/logs/last/{job_name}",
    response_model=SchedulerJobLogResponse | None,
    summary="Get last job run",
    description="Get the last execution log for a specific job.",
)
async def get_last_job_run(
    job_name: str,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any] | None:
    """Get last run of a specific job."""
    repo = SchedulerJobLogRepository(session)
    log = await repo.get_last_run(job_name)
    
    if not log:
        return None
    
    return {
        "id": log.id,
        "job_name": log.job_name,
        "job_type": log.job_type,
        "status": log.status,
        "started_at": log.started_at,
        "completed_at": log.completed_at,
        "emails_processed": log.emails_processed,
        "transactions_created": log.transactions_created,
        "senders_learned": log.senders_learned,
        "error_message": log.error_message,
    }


@router.post(
    "/start",
    summary="Start scheduler",
    description="Start the scheduler if it's not already running.",
)
async def start_scheduler() -> dict[str, str]:
    """Start the scheduler."""
    settings = get_settings()
    
    if not settings.scheduler_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Scheduler is disabled in configuration",
        )
    
    service = SchedulerService()
    service.start()
    
    return {"status": "started"}


@router.post(
    "/stop",
    summary="Stop scheduler",
    description="Stop the scheduler if it's running.",
)
async def stop_scheduler() -> dict[str, str]:
    """Stop the scheduler."""
    service = SchedulerService()
    service.stop()
    
    return {"status": "stopped"}
