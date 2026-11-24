"""Entry points of the AI insight functions for the cycle and the scheduler (the rest lives in the ``ai_*`` modules).

* ``after_cycle`` (called by ``Processor.cycle``): categorize/tag one batch, explain new anomalies, judge new recurring
  charges and emit price-increase alerts, and, from 09:00 on, narrate the previous month if it has no report yet; then the
  trip/event detection. Every step is isolated, guarded by ``ai_budget.allow`` and never raises.
* ``run_monthly_narrative`` (scheduler, day 1 at 09:00 Bogotá) and ``run_weekly_subscriptions`` (scheduler, weekly).

Each step is a HEAVY AUTOMATIC job, so it goes through ``ai_jobs.dispatch``: outside the peak it runs right here and its result
comes back as before; in the peak it is parked in the queue (``{"deferred": job_id, ...}``) until the valley or until the owner
runs it (see ``app.services.ai_jobs``). A step with nothing to do never leaves a job behind.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.services import (
    ai_anomalies,
    ai_categorize,
    ai_events,
    ai_features,
    ai_jobs,
    ai_narrative,
    ai_subscriptions,
    budget_assign,
)

logger = get_logger(__name__)


async def _step(session, name: str, results: dict, work) -> None:
    try:
        results[name] = await work
    except Exception as exc:
        try:
            await session.rollback()
        except Exception:
            pass
        logger.error("ai_insights_step_failed", step=name, error_type=type(exc).__name__)
        results[name] = {"error": type(exc).__name__}


# --- runners (registered for the queue; they run the same work the steps always did) -----------------------------------------


@ai_jobs.register("categorize_batch")
async def _categorize(ctx, payload):
    return await ai_categorize.run_batch(ctx.session, ctx.firefly, ctx.llm, ctx.settings)


@ai_jobs.register("budget_assign")
async def _budgets(ctx, payload):
    # Budgets read the category the categorize step wrote, so the two keep their order (see ``after_cycle``).
    return await budget_assign.run_batch(ctx.session, ctx.firefly, ctx.llm, ctx.settings)


@ai_jobs.register("anomalies")
async def _anomalies(ctx, payload):
    return await ai_anomalies.explain_new(ctx.session, ctx.llm, ctx.settings)


@ai_jobs.register("subscriptions")
async def _subscriptions(ctx, payload):
    max_age = ai_subscriptions.REFRESH if payload.get("weekly") else None
    return await ai_subscriptions.run(ctx.session, ctx.llm, ctx.settings, max_age=max_age)


@ai_jobs.register("narrative")
async def _narrative(ctx, payload):
    return await ai_narrative.after_cycle(ctx.session, ctx.firefly, ctx.llm, ctx.settings)


@ai_jobs.register("monthly_narrative")
async def _monthly_narrative(ctx, payload):
    return await ai_narrative.run_previous_month(ctx.session, ctx.firefly, ctx.llm, ctx.settings)


# --- prechecks (used only when a job would be parked: how many calls does the work need, 0 for none) -------------------------


async def _categorize_calls(session) -> int:
    state = await ai_features.get(session)
    features = state["features"]
    if not (features["categorize"] or features["tags"]):
        return 0
    return 1 if await ai_categorize.eligible(session, features) else 0


async def _budget_calls(session, settings) -> int:
    ids = await budget_assign.pending_ids(session, ZoneInfo(settings.timezone))
    return len(ids)


async def _narrative_calls(session, settings) -> int:
    if datetime.now(ZoneInfo(settings.timezone)).hour < ai_narrative.MORNING_HOUR:
        return 0
    return await ai_narrative.pending(session, settings)


async def after_cycle(session, firefly, llm, settings) -> dict:
    ctx = ai_jobs.JobContext(session, firefly, llm, settings)
    results: dict = {}
    await _step(session, "categorize", results, ai_jobs.dispatch(
        ctx, "categorize_batch", "Categorizar y etiquetar movimientos", "categorize_batch", dedupe_key="categorize_batch",
        precheck=lambda: _categorize_calls(session),
    ))
    # Budgets read the category the step above just wrote, so they run right after it (same batch).
    await _step(session, "budgets", results, ai_jobs.dispatch(
        ctx, "budget_assign", "Asignar presupuestos a los gastos nuevos", "budget_assign", dedupe_key="budget_assign",
        precheck=lambda: _budget_calls(session, settings),
    ))
    await _step(session, "anomalies", results, ai_jobs.dispatch(
        ctx, "anomalies", "Explicar movimientos inusuales", "anomalies", dedupe_key="anomalies",
        precheck=lambda: ai_anomalies.pending(session, settings),
    ))
    await _step(session, "subscriptions", results, ai_jobs.dispatch(
        ctx, "subscriptions", "Revisar cobros recurrentes", "subscriptions", dedupe_key="subscriptions",
        precheck=lambda: ai_subscriptions.pending(session, settings),
    ))
    await _step(session, "narrative", results, ai_jobs.dispatch(
        ctx, "narrative", "Narrar el informe del mes anterior", "narrative", dedupe_key="narrative",
        precheck=lambda: _narrative_calls(session, settings),
    ))
    await _step(session, "events", results, ai_events.after_cycle(session, firefly, llm, settings))
    return results


async def run_monthly_narrative(session, firefly, llm, settings) -> dict:
    return await ai_jobs.dispatch(
        ai_jobs.JobContext(session, firefly, llm, settings), "narrative", "Narrar el informe del mes anterior", "monthly_narrative",
        dedupe_key="narrative", precheck=lambda: ai_narrative.pending(session, settings),
    )


async def run_weekly_subscriptions(session, llm, settings) -> dict:
    return await ai_jobs.dispatch(
        ai_jobs.JobContext(session, None, llm, settings), "subscriptions", "Renovar el veredicto de las suscripciones",
        "subscriptions", {"weekly": True}, dedupe_key="subscriptions_weekly",
        precheck=lambda: ai_subscriptions.pending(session, settings, ai_subscriptions.REFRESH),
    )
