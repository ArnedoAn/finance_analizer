"""Cron orchestration with a dedicated, session-scoped PostgreSQL leader lock."""
import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

from app.core import error_log
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.database import _get_engine

logger = get_logger(__name__)
LEADER_LOCK = 736283194
DIGEST_TICK_CRON = "0 * * * *"
MONTHLY_NARRATIVE_CRON = "0 9 1 * *"  # day 1, 09:00 in the settings timezone
WEEKLY_SUBSCRIPTIONS_CRON = "30 9 * * 1"  # Mondays 09:30


class SchedulerService:
    def __init__(self, run_cycle, learn_senders, run_reconcile, run_digest, run_weekly_digest, run_digest_tick,
                 run_monthly_narrative, run_weekly_subscriptions, settings=None, extra_jobs=None):
        """``extra_jobs`` is ``{job_id: (callback, cron)}``: jobs of the other modules (AI job queue tick, forgotten
        subscriptions, month close, coach...) scheduled in the settings timezone like the rest."""
        self.settings = settings or get_settings()
        self.extra_jobs = dict(extra_jobs or {})
        self.callbacks = {
            "cycle": run_cycle, "sender_learning": learn_senders,
            "reconcile": run_reconcile, "digest": run_digest,
            "weekly_digest": run_weekly_digest, "digest_tick": run_digest_tick,
            "monthly_narrative": run_monthly_narrative, "weekly_subscriptions": run_weekly_subscriptions,
            **{job_id: callback for job_id, (callback, _) in self.extra_jobs.items()},
        }
        self.scheduler = AsyncIOScheduler(timezone=self.settings.timezone)
        self._connection = None
        self._leader = False
        self._closing = False
        self._election_lock = asyncio.Lock()
        self._active: set[asyncio.Task] = set()
        for job_id, cron in (
            ("cycle", self.settings.cycle_cron),
            ("sender_learning", self.settings.sender_learning_cron),
            ("reconcile", self.settings.reconcile_cron),
            # Hourly: the owner's digest policy (``digest_policy``: hour, daily, weekly) decides what is due.
            # ``digest`` and ``weekly_digest`` are manual jobs (POST /scheduler/jobs/{id}/trigger), never scheduled.
            ("digest_tick", DIGEST_TICK_CRON),
            ("monthly_narrative", MONTHLY_NARRATIVE_CRON),
            ("weekly_subscriptions", WEEKLY_SUBSCRIPTIONS_CRON),
            *((job_id, cron) for job_id, (_, cron) in self.extra_jobs.items()),
        ):
            self.scheduler.add_job(
                self._run, CronTrigger.from_crontab(cron, timezone=self.settings.timezone),
                args=[job_id], id=job_id, coalesce=True, max_instances=1,
                misfire_grace_time=3600,
            )
        self.scheduler.add_job(self._elect, "interval", seconds=30, id="leader_election", max_instances=1)

    async def _elect(self):
        async with self._election_lock:
            if self._closing:
                return
            engine = _get_engine()
            if engine.dialect.name != "postgresql":
                self._leader = True
                return
            try:
                if self._connection is not None:
                    # A reconnected session no longer owns the advisory lock.
                    if self._connection.invalidated:
                        raise ConnectionError("Scheduler leader session invalidated")
                    owns_lock = await self._connection.scalar(text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                        "AND pid = pg_backend_pid() AND classid = 0 AND objid = :key AND granted)"
                    ), {"key": LEADER_LOCK})
                    if not owns_lock:
                        raise ConnectionError("Scheduler leadership lost")
                    await self._connection.commit()
                    return
                connection = await engine.connect()
                try:
                    acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LEADER_LOCK})
                    await connection.commit()
                except BaseException:
                    await connection.close()
                    raise
                if acquired:
                    self._connection = connection
                    self._leader = True
                else:
                    await connection.close()
                    self._leader = False
            except Exception:
                self._leader = False
                if self._connection is not None:
                    await self._connection.close()
                    self._connection = None
                for task in tuple(self._active):
                    task.cancel()
                logger.warning("scheduler_leader_connection_lost")

    async def _run(self, job_id):
        await self._elect()
        if not self._leader:
            return {"skipped": "not_scheduler_leader"}
        task = asyncio.current_task()
        self._active.add(task)
        try:
            return await self.callbacks[job_id]()
        except Exception as exc:
            error_log.record("cycle", f"job {job_id}", exc=exc)
            raise
        finally:
            self._active.discard(task)

    async def start(self):
        if self.settings.scheduler_enabled and not self.scheduler.running:
            await self._elect()
            self.scheduler.start()

    async def shutdown(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        tasks = tuple(self._active)
        self._closing = True
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._election_lock:
            if self._connection is not None:
                try:
                    await self._connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LEADER_LOCK})
                    await self._connection.commit()
                finally:
                    await self._connection.close()
                    self._connection = None
            self._leader = False

    def status(self):
        return {
            "enabled": self.settings.scheduler_enabled,
            "running": self.scheduler.running, "leader": self._leader,
            "jobs": [
                {"id": job.id, "next_run": next_run.isoformat() if next_run else None, "trigger": str(job.trigger)}
                for job in self.scheduler.get_jobs() if job.id in self.callbacks
                for next_run in [getattr(job, "next_run_time", None)]
            ],
        }

    async def trigger(self, job_id):
        if job_id not in self.callbacks:
            raise ValueError(f"Unknown job: {job_id}")
        return {"job": job_id, "result": await self._run(job_id)}
