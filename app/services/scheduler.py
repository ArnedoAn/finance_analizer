"""
Scheduler Service

Manages CRON-based scheduled tasks using APScheduler.
Handles automatic email processing and sender learning jobs.
"""

import asyncio
from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.timezone import get_app_timezone

logger = get_logger(__name__)

# Global scheduler instance
_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """Get the global scheduler instance with timezone configured."""
    global _scheduler
    if _scheduler is None:
        app_tz = get_app_timezone()
        _scheduler = AsyncIOScheduler(timezone=app_tz)
    return _scheduler


class SchedulerService:
    """
    Service for managing scheduled jobs.
    
    Uses APScheduler for CRON-based task scheduling.
    """
    
    def __init__(self) -> None:
        self.settings = get_settings()
        self.scheduler = get_scheduler()
        self._jobs_registered = False
    
    def setup_jobs(self) -> None:
        """Register all scheduled jobs."""
        if self._jobs_registered:
            return
        
        if not self.settings.scheduler_enabled:
            logger.info("scheduler_disabled")
            return
        
        # Email processing job
        self.scheduler.add_job(
            self._run_email_processing,
            CronTrigger.from_crontab(self.settings.scheduler_processing_cron),
            id="email_processing",
            name="Process Financial Emails",
            replace_existing=True,
            misfire_grace_time=300,  # 5 minutes grace period
        )
        logger.info(
            "scheduler_job_registered",
            job="email_processing",
            cron=self.settings.scheduler_processing_cron,
        )
        
        # Sender learning job
        self.scheduler.add_job(
            self._run_sender_learning,
            CronTrigger.from_crontab(self.settings.scheduler_learning_cron),
            id="sender_learning",
            name="Learn Financial Senders",
            replace_existing=True,
            misfire_grace_time=3600,  # 1 hour grace period
        )
        logger.info(
            "scheduler_job_registered",
            job="sender_learning",
            cron=self.settings.scheduler_learning_cron,
        )
        
        self._jobs_registered = True
    
    def start(self) -> None:
        """Start the scheduler."""
        if not self.settings.scheduler_enabled:
            return
        
        if not self.scheduler.running:
            self.setup_jobs()
            self.scheduler.start()
            logger.info("scheduler_started")
    
    def stop(self) -> None:
        """Stop the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("scheduler_stopped")
    
    def get_jobs_status(self) -> list[dict[str, Any]]:
        """Get status of all scheduled jobs."""
        jobs = []
        for job in self.scheduler.get_jobs():
            next_run = job.next_run_time
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": next_run.isoformat() if next_run else None,
                "trigger": str(job.trigger),
            })
        return jobs
    
    async def trigger_job_now(self, job_id: str) -> dict[str, Any]:
        """Manually trigger a job immediately."""
        if job_id == "email_processing":
            result = await self._run_email_processing()
            return {"job": job_id, "result": result}
        elif job_id == "sender_learning":
            result = await self._run_sender_learning()
            return {"job": job_id, "result": result}
        else:
            return {"error": f"Unknown job: {job_id}"}
    
    async def _run_email_processing(self) -> dict[str, Any]:
        """Execute email processing job."""
        from app.api.dependencies import (
            get_deepseek_client,
            get_firefly_client,
            get_gmail_client,
        )
        from app.db.database import get_db_session
        from app.db.repositories import SchedulerJobLogRepository
        from app.services.email_processor import EmailProcessorService
        from app.models.schemas import BatchProcessRequest
        
        logger.info("scheduler_email_processing_starting")
        
        async with get_db_session() as session:
            job_log_repo = SchedulerJobLogRepository(session)
            job_log = await job_log_repo.create(
                job_name="email_processing",
                job_type="processing",
            )
            await session.commit()
            
            try:
                gmail = get_gmail_client()
                deepseek = get_deepseek_client()
                firefly = get_firefly_client()
                
                processor = EmailProcessorService(
                    session=session,
                    gmail_client=gmail,
                    deepseek_client=deepseek,
                    firefly_client=firefly,
                )
                
                # Use known senders for filtering
                result = await processor.process_batch(
                    BatchProcessRequest(
                        max_emails=self.settings.gmail_max_results,
                        dry_run=self.settings.dry_run,
                        use_known_senders=True,  # Filter by known senders
                    )
                )
                
                await job_log_repo.complete(
                    job_log.id,
                    emails_processed=result.total_emails,
                    transactions_created=result.created,
                    details={"skipped": result.skipped, "failed": result.failed},
                )
                await session.commit()
                
                logger.info(
                    "scheduler_email_processing_completed",
                    processed=result.total_emails,
                    created=result.created,
                )
                
                return {
                    "status": "completed",
                    "emails_processed": result.total_emails,
                    "transactions_created": result.created,
                }
                
            except Exception as e:
                logger.error("scheduler_email_processing_failed", error=str(e))
                await job_log_repo.fail(job_log.id, str(e))
                await session.commit()
                return {"status": "failed", "error": str(e)}
    
    async def _run_sender_learning(self) -> dict[str, Any]:
        """Execute sender learning job."""
        from app.api.dependencies import get_deepseek_client, get_gmail_client
        from app.db.database import get_db_session
        from app.services.sender_learning import SenderLearningService
        
        logger.info("scheduler_sender_learning_starting")
        
        async with get_db_session() as session:
            try:
                gmail = get_gmail_client()
                deepseek = get_deepseek_client()
                
                learning_service = SenderLearningService(
                    session=session,
                    gmail_client=gmail,
                    deepseek_client=deepseek,
                )
                
                result = await learning_service.learn_from_recent_emails(
                    email_count=self.settings.scheduler_learning_email_count,
                    days_back=15,  # Look back 15 days
                )
                
                logger.info(
                    "scheduler_sender_learning_completed",
                    emails_analyzed=result["emails_analyzed"],
                    senders_learned=result["senders_learned"],
                )
                
                return {
                    "status": "completed",
                    **result,
                }
                
            except Exception as e:
                logger.error("scheduler_sender_learning_failed", error=str(e))
                return {"status": "failed", "error": str(e)}


# Helper to parse CRON expressions for documentation
def describe_cron(cron_expression: str) -> str:
    """Get human-readable description of a CRON expression."""
    parts = cron_expression.split()
    if len(parts) != 5:
        return cron_expression
    
    minute, hour, day, month, weekday = parts
    
    descriptions = []
    
    # Common patterns
    if cron_expression == "0 */6 * * *":
        return "Every 6 hours"
    if cron_expression == "0 0 */5 * *":
        return "Every 5 days at midnight"
    if cron_expression == "0 0 * * *":
        return "Daily at midnight"
    if cron_expression == "0 0 1,15 * *":
        return "On the 1st and 15th of each month at midnight"
    if cron_expression == "0 0 * * 0":
        return "Every Sunday at midnight"
    
    # Fallback
    return f"CRON: {cron_expression}"
