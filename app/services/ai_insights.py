"""Entry points of the AI insight functions for the cycle and the scheduler (the rest lives in the ``ai_*`` modules).

* ``after_cycle`` (called by ``Processor.cycle``): categorize/tag one batch, explain new anomalies, judge new recurring
  charges and emit price-increase alerts, and, from 09:00 on, narrate the previous month if it has no report yet.
  Every step is isolated, guarded by ``ai_budget.allow`` and never raises.
* ``run_monthly_narrative`` (scheduler, day 1 at 09:00 Bogotá) and ``run_weekly_subscriptions`` (scheduler, weekly).
"""
from app.core.logging import get_logger
from app.services import ai_anomalies, ai_categorize, ai_narrative, ai_subscriptions, budget_assign

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


async def after_cycle(session, firefly, llm, settings) -> dict:
    results: dict = {}
    await _step(session, "categorize", results, ai_categorize.run_batch(session, firefly, llm, settings))
    # Budgets read the category the step above just wrote, so they run right after it (same batch).
    await _step(session, "budgets", results, budget_assign.run_batch(session, firefly, llm, settings))
    await _step(session, "anomalies", results, ai_anomalies.explain_new(session, llm, settings))
    await _step(session, "subscriptions", results, ai_subscriptions.run(session, llm, settings))
    await _step(session, "narrative", results, ai_narrative.after_cycle(session, firefly, llm, settings))
    return results


async def run_monthly_narrative(session, firefly, llm, settings) -> dict:
    return await ai_narrative.run_previous_month(session, firefly, llm, settings)


async def run_weekly_subscriptions(session, llm, settings) -> dict:
    try:
        return await ai_subscriptions.run(session, llm, settings, max_age=ai_subscriptions.REFRESH)
    except Exception as exc:
        await session.rollback()
        logger.error("weekly_subscriptions_failed", error_type=type(exc).__name__)
        return {"error": type(exc).__name__}
