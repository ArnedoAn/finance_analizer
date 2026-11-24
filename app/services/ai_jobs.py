"""Queue of heavy AI jobs that avoid the peak (OpenCode Go charges twice for deepseek-v4.1-flash on weekdays 01:00-04:00 and
06:00-10:00 UTC) and that the owner can SEE and decide on.

Who goes through here: the automatic batch jobs (categorize, budgets, review suggestions, anomalies, subscriptions, narrative,
trips and events, forgotten subscriptions) and the heavy jobs of the other executors (long agent tasks, month close, coach,
bills extraction, memory batches). Who does NOT: the processing of every incoming message (including its second opinion) and
whatever the owner asks explicitly from the app «now» (search, on-demand narrative, ``run_now`` tasks).

* ``register(runner_key)`` registers ``async def runner(ctx, payload) -> dict`` (``ctx``: ``JobContext``). Register at import time.
* ``enqueue`` creates the job and decides its status from the clock and ``ai_budget`` ``peak_policy``: outside the peak, or with
  ``run``, it is ``queued``; in the peak with ``wait``/``ask`` it is ``waiting_offpeak`` with ``run_after`` = the start of the
  valley (``wait`` is decided by the policy; ``ask`` waits for the owner and is announced by one grouped alert).
* ``run_due`` (scheduler, every minute) runs the ``queued`` jobs and the waiting ones whose valley has come (one at a time per
  kind). ``dispatch`` is ``enqueue`` + run inline when the job is not parked (what the post-cycle steps use).
* The owner decides with ``run_now`` / ``run_all_now`` / ``wait`` / ``cancel`` (``/ai/jobs`` endpoints).

Functions flush but never commit, except ``run_job`` / ``run_due`` / ``dispatch`` which commit around the run.
"""
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select

from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.models import AiJob
from app.services import ai_budget
from app.services import alerts as alerting

logger = get_logger(__name__)

QUEUED, WAITING, RUNNING, DONE, FAILED, CANCELLED = "queued", "waiting_offpeak", "running", "done", "failed", "cancelled"
ACTIVE = (QUEUED, WAITING, RUNNING)
STATUSES = (*ACTIVE, DONE, FAILED, CANCELLED)
# Results that mean «there was nothing to do»: an inline run that ends so leaves no row behind.
NOOP_SKIPS = frozenset({"nothing_to_do", "disabled", "exists", "early", "retry_later", "no_budgets", "concurrent"})
STALE_RUNNING = timedelta(minutes=30)
ALERT_GRACE = timedelta(minutes=1)
KEEP_FINISHED = timedelta(days=30)
ALERT_TYPE = "ai_jobs_waiting"
ALERT_OPTIONS = ["Ejecutar ahora"]
DEFAULT_MODEL = "deepseek-v4.1-flash"
# Share of the monthly allowance (percent) that one call is estimated to cost in the valley, per model: price / monthly limit.
# ESTIMATES: calibrate with the real OpenCode Go prices and limits. In the peak the surcharged models cost PEAK_MULTIPLIER times.
PCT_PER_CALL = {"deepseek-v4.1-flash": 0.02, "mimo-v2.6-flash": 0.04}
SURCHARGED_MODELS = frozenset({"deepseek-v4.1-flash"})
PEAK_MULTIPLIER = 2


@dataclass
class JobContext:
    """What a runner gets: the session (the runner commits its own work) and the clients."""
    session: Any
    firefly: Any
    llm: Any
    settings: Any


Runner = Callable[[JobContext, dict], Awaitable[dict]]
_RUNNERS: dict[str, Runner] = {}


def register(key: str) -> Callable[[Runner], Runner]:
    """Decorator that registers the runner of ``runner_key`` (a later registration of the same key replaces it)."""
    def decorator(runner: Runner) -> Runner:
        _RUNNERS[key] = runner
        return runner
    return decorator


class JobError(Exception):
    """An owner action that cannot be done; ``status``/``code``/``detail`` map to the HTTP error."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


# --- estimates -------------------------------------------------------------------------------------------------------


def _surcharged(model: str | None) -> bool:
    return (model or DEFAULT_MODEL) in SURCHARGED_MODELS


def estimate_pct(calls: int, model: str | None = None) -> float:
    """Percent of the monthly allowance that ``calls`` calls cost in the valley."""
    return round(max(0, calls) * PCT_PER_CALL.get(model or DEFAULT_MODEL, PCT_PER_CALL[DEFAULT_MODEL]), 3)


def public(job: AiJob, now: datetime | None = None) -> dict:
    """Body of one job for ``GET /ai/jobs``: ``estimated_allowance_pct`` is the valley cost, ``estimated_allowance_pct_now`` what
    running it right now would cost (twice in the peak for surcharged models)."""
    base = job.estimated_allowance_pct if job.estimated_allowance_pct is not None else 0.0
    peak = ai_budget.is_peak(now)
    return {
        "id": job.id, "kind": job.kind, "title": job.title, "status": job.status, "created_at": job.created_at,
        "updated_at": job.updated_at, "estimated_calls": job.estimated_calls, "estimated_allowance_pct": base,
        "estimated_allowance_pct_now": round(base * PEAK_MULTIPLIER, 3) if peak and _surcharged(job.model) else base,
        "model": job.model or DEFAULT_MODEL, "run_after": job.run_after, "decided_by": job.decided_by,
        "started_at": job.started_at, "finished_at": job.finished_at, "result": job.result, "error": job.error,
    }


# --- enqueue / decide ------------------------------------------------------------------------------------------------


def _parks(peak_end: datetime | None, mode: str, model: str | None) -> bool:
    """Does a job come due in the peak and have to wait? Not with ``run``, and not for a model without surcharge."""
    return peak_end is not None and mode != "run" and _surcharged(model)


async def active_by_dedupe(session, dedupe_key: str) -> AiJob | None:
    return await session.scalar(select(AiJob).where(AiJob.dedupe_key == dedupe_key, AiJob.status.in_(ACTIVE)).order_by(AiJob.id).limit(1))


async def enqueue(
    session, kind: str, title: str, estimated_calls: int, runner_key: str, payload: dict | None = None, *,
    dedupe_key: str | None = None, model: str | None = None, now: datetime | None = None,
) -> AiJob:
    """Create a job and decide its status from the clock and the owner's ``peak_policy``. Flushes, never commits.

    With ``dedupe_key`` an active job with the same key is returned instead of creating another one. ``model`` is the model the
    runner will call (for the estimate and the surcharge rule); None means the general model, ``deepseek-v4.1-flash``.
    """
    if dedupe_key and (existing := await active_by_dedupe(session, dedupe_key)) is not None:
        return existing
    mode = (await ai_budget.policy(session))["peak_policy"]
    peak_end = ai_budget.peak_ends_at(now)
    job = AiJob(
        kind=kind, title=title[:255], runner_key=runner_key, payload=payload or {}, dedupe_key=dedupe_key,
        estimated_calls=max(0, int(estimated_calls)), estimated_allowance_pct=estimate_pct(estimated_calls, model), model=model,
    )
    if _parks(peak_end, mode, model):
        job.status, job.run_after = WAITING, peak_end
        job.decided_by = "policy" if mode == "wait" else None
    else:
        job.status, job.decided_by = QUEUED, "policy"
    session.add(job)
    await session.flush()
    return job


def due(job: AiJob, peak: bool, mode: str) -> bool:
    """May this job run now? Queued ones always; waiting ones in the valley, or in the peak under ``run`` unless the owner chose to wait."""
    if job.status == QUEUED:
        return True
    if job.status != WAITING:
        return False
    return (not peak) or (mode == "run" and job.decided_by != "owner")


# --- running ---------------------------------------------------------------------------------------------------------


def _json_safe(value) -> dict:
    data = json.loads(json.dumps(value, default=str, ensure_ascii=False))
    return data if isinstance(data, dict) else {"result": data}


def _is_noop(result) -> bool:
    return isinstance(result, dict) and result.get("skipped") in NOOP_SKIPS


async def run_job(ctx: JobContext, job: AiJob, *, discard_noop: bool = False) -> dict:
    """Run ``job`` now (any status except running/final) and store its outcome; returns the runner's result, or
    ``{"error": <class name>}`` when it failed. ``discard_noop`` deletes the row of a run that found nothing to do."""
    session, job_id, kind = ctx.session, job.id, job.kind
    runner = _RUNNERS.get(job.runner_key)
    payload = dict(job.payload or {})
    job.status, job.started_at, job.error = RUNNING, now_utc(), None
    await session.commit()
    result: dict
    error: str | None = None
    try:
        if runner is None:
            raise LookupError("runner_missing")
        result = _json_safe(await runner(ctx, payload))
    except Exception as exc:  # never log the message: it can quote financial data
        await session.rollback()
        error, result = type(exc).__name__, {}
        logger.error("ai_job_failed", job_id=job_id, kind=kind, error_type=error)
    job = await session.get(AiJob, job_id, populate_existing=True)
    if job is None or job.status != RUNNING:
        return result if error is None else {"error": error}
    if error is None and discard_noop and _is_noop(result):
        await session.delete(job)
    else:
        job.status, job.result, job.error, job.finished_at = (FAILED if error else DONE), (result or None), error, now_utc()
    await session.commit()
    return {"error": error} if error else result


async def dispatch(
    ctx: JobContext, kind: str, title: str, runner_key: str, payload: dict | None = None, *, estimated_calls: int = 1,
    precheck: Callable[[], Awaitable[int | None]] | None = None, dedupe_key: str | None = None, model: str | None = None,
) -> dict:
    """The automatic jobs' way in: outside the peak (or with ``run``) the runner runs now and its result comes back; in the peak
    the job is parked (``{"deferred": job_id, "run_after": iso}``). ``precheck`` (peak only, so the valley path costs nothing
    extra) returns the number of calls the work needs or 0 when there is nothing to do, so no empty job is ever parked."""
    session = ctx.session
    mode = (await ai_budget.policy(session))["peak_policy"]
    peak = ai_budget.peak_ends_at() is not None
    if precheck is not None and _parks(ai_budget.peak_ends_at(), mode, model):
        calls = await precheck()
        if not calls:
            return {"skipped": "nothing_to_do"}
        estimated_calls = calls
    job = await enqueue(session, kind, title, estimated_calls, runner_key, payload, dedupe_key=dedupe_key or kind, model=model)
    if job.status == RUNNING:
        await session.commit()
        return {"skipped": "running"}
    if not due(job, peak, mode):
        await session.commit()
        return {"deferred": job.id, "run_after": job.run_after.isoformat() if job.run_after else None}
    return await run_job(ctx, job, discard_noop=True)


async def run_due(ctx: JobContext) -> dict:
    """Scheduler tick: run what is due (one at a time per kind), clean up, and send the grouped alert. Never raises."""
    session = ctx.session
    ran: list[int] = []
    try:
        await reap(session)
        mode = (await ai_budget.policy(session))["peak_policy"]
        peak = ai_budget.is_peak()
        busy = set((await session.scalars(select(AiJob.kind).where(AiJob.status == RUNNING))).all())
        jobs = list((await session.scalars(select(AiJob).where(AiJob.status.in_((QUEUED, WAITING))).order_by(AiJob.id))).all())
        for job in jobs:
            if job.kind in busy or not due(job, peak, mode):
                continue
            busy.add(job.kind)
            await run_job(ctx, job)
            ran.append(job.id)
        alerted = await notify_waiting(session)
        await session.commit()
        return {"ran": ran, "alerted": alerted}
    except Exception as exc:
        await session.rollback()
        logger.error("ai_jobs_tick_failed", error_type=type(exc).__name__)
        return {"ran": ran, "error": type(exc).__name__}


async def reap(session, now: datetime | None = None) -> int:
    """Mark jobs left ``running`` by a crash as failed and purge old finished rows. Does not commit."""
    now = now or now_utc()
    stale = list((await session.scalars(select(AiJob).where(AiJob.status == RUNNING, AiJob.started_at < now - STALE_RUNNING))).all())
    for job in stale:
        job.status, job.error, job.finished_at = FAILED, "interrupted", now
    await session.execute(delete(AiJob).where(AiJob.status.in_((DONE, FAILED, CANCELLED)), AiJob.finished_at < now - KEEP_FINISHED))
    await session.flush()
    return len(stale)


# --- the grouped alert -----------------------------------------------------------------------------------------------


async def notify_waiting(session, now: datetime | None = None) -> bool:
    """With ``peak_policy: ask``: ONE alert per peak window for the jobs that wait for the owner's decision.

    ``«N trabajos de IA esperando la hora valle (ahorro ~X %)»`` with the option «Ejecutar ahora» (the app calls
    ``POST /ai/jobs/run-all-now``). Jobs younger than ``ALERT_GRACE`` are left out so the ones of one cycle are counted together.
    Does not commit.
    """
    now = now or now_utc()
    if (await ai_budget.policy(session))["peak_policy"] != "ask":
        return False
    jobs = list((await session.scalars(
        select(AiJob).where(AiJob.status == WAITING, AiJob.decided_by.is_(None), AiJob.created_at <= now - ALERT_GRACE).order_by(AiJob.id)
    )).all())
    if not jobs:
        return False
    until = min((job.run_after for job in jobs if job.run_after), default=None)
    count = len(jobs)
    savings = f"{sum(job.estimated_allowance_pct or 0.0 for job in jobs):.2f}".replace(".", ",")
    plural = count != 1
    title = f"{count} trabajo{'s' if plural else ''} de IA esperando la hora valle (ahorro ~{savings} %)"
    when = f" hasta las {until.astimezone(ZoneInfo(_timezone())):%H:%M}" if until is not None else ""
    body = (
        f"{count} trabajo{'s' if plural else ''} de IA pesado{'s' if plural else ''} espera{'n' if plural else ''} la hora valle{when} "
        f"(ahorro ~{savings} % del cupo mensual). Puedes ejecutarlos ya, pagando el doble, o dejar que esperen."
    )
    alert = await alerting.emit(
        session, ALERT_TYPE, title=title, body=body, options=ALERT_OPTIONS,
        dedupe_key=f"{ALERT_TYPE}:{until.isoformat() if until else 'none'}", min_interval_minutes=alerting.ONCE_MINUTES,
    )
    return alert is not None


def _timezone() -> str:
    from app.core.config import get_settings

    return get_settings().timezone


# --- the owner's decisions -------------------------------------------------------------------------------------------


async def get(session, job_id: int) -> AiJob:
    job = await session.get(AiJob, job_id, populate_existing=True)
    if job is None:
        raise JobError(404, "job_not_found", "El trabajo no existe.")
    return job


async def listing(session, status: str = "active", limit: int = 100, offset: int = 0) -> tuple[list[AiJob], int]:
    """Jobs of one status (``active`` = queued, waiting and running; ``all``), newest first."""
    query, count = select(AiJob), select(func.count()).select_from(AiJob)
    if status != "all":
        wanted = ACTIVE if status == "active" else (status,)
        query, count = query.where(AiJob.status.in_(wanted)), count.where(AiJob.status.in_(wanted))
    total = await session.scalar(count)
    rows = (await session.scalars(query.order_by(AiJob.id.desc()).limit(limit).offset(offset).execution_options(populate_existing=True))).all()
    return list(rows), total


async def run_now(session, job_id: int) -> AiJob:
    """The owner runs a waiting (or queued) job now, paying the peak price. Flushes; the scheduler or ``kick`` runs it."""
    job = await get(session, job_id)
    if job.status not in (WAITING, QUEUED):
        raise JobError(409, "job_not_actionable", "El trabajo ya no está esperando.")
    job.status, job.decided_by, job.run_after = QUEUED, "owner", None
    await session.flush()
    return job


async def run_all_now(session) -> list[AiJob]:
    jobs = list((await session.scalars(select(AiJob).where(AiJob.status == WAITING).order_by(AiJob.id))).all())
    for job in jobs:
        job.status, job.decided_by, job.run_after = QUEUED, "owner", None
    await session.flush()
    return jobs


async def wait(session, job_id: int) -> AiJob:
    """The owner sends a waiting or queued job back to the valley (only meaningful in the peak)."""
    job = await get(session, job_id)
    if job.status not in (WAITING, QUEUED):
        raise JobError(409, "job_not_actionable", "El trabajo ya no está esperando.")
    peak_end = ai_budget.peak_ends_at()
    if peak_end is None:
        raise JobError(409, "not_peak", "Ahora no es hora pico: el trabajo corre solo.")
    job.status, job.decided_by, job.run_after = WAITING, "owner", peak_end
    await session.flush()
    return job


async def cancel(session, job_id: int) -> AiJob:
    job = await get(session, job_id)
    if job.status == RUNNING:
        raise JobError(409, "job_running", "El trabajo ya está corriendo.")
    if job.status not in (WAITING, QUEUED):
        raise JobError(409, "job_not_actionable", "El trabajo ya terminó.")
    job.status, job.finished_at = CANCELLED, now_utc()
    await session.flush()
    return job
