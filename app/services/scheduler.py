"""Single-owner cron orchestration; callbacks own their database sessions."""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import get_settings


class SchedulerService:
    def __init__(self, run_cycle, learn_senders, settings=None):
        self.settings = settings or get_settings()
        self.callbacks = {"cycle": run_cycle, "sender_learning": learn_senders}
        self.scheduler = AsyncIOScheduler(timezone=self.settings.timezone)
        for job_id, cron in (
            ("cycle", self.settings.cycle_cron),
            ("sender_learning", self.settings.sender_learning_cron),
        ):
            self.scheduler.add_job(
                self.callbacks[job_id],
                CronTrigger.from_crontab(cron, timezone=self.settings.timezone),
                id=job_id,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=3600,
            )

    def start(self):
        if self.settings.scheduler_enabled and not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def status(self):
        return {
            "enabled": self.settings.scheduler_enabled,
            "running": self.scheduler.running,
            "jobs": [
                {
                    "id": job.id,
                    "next_run": next_run.isoformat() if next_run else None,
                    "trigger": str(job.trigger),
                }
                for job in self.scheduler.get_jobs()
                for next_run in [getattr(job, "next_run_time", None)]
            ],
        }

    async def trigger(self, job_id):
        if job_id not in self.callbacks:
            raise ValueError(f"Unknown job: {job_id}")
        return {"job": job_id, "result": await self.callbacks[job_id]()}
