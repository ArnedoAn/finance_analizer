"""Aggregate the /api/v1 domain routers and expose scheduler job callbacks.

Every route except health/live and health/ready declares a scope from
``app.api.dependencies``; tests enforce that no route is left unauthenticated.
"""
from fastapi import APIRouter

from app.api.dependencies import get_firefly, get_llm
from app.api.routers import (
    agent as agent_router,
)
from app.api.routers import (
    ai,
    ai_events,
    ai_insights,
    ai_jobs,
    alert_questions,
    alerts,
    assistant,
    budgets,
    cards,
    catalog,
    counterparties,
    debts,
    devices,
    digest,
    errors,
    finance_plan,
    history,
    inbox,
    ingest,
    ledger,
    maintenance,
    memories,
    own_accounts,
    receipts,
    reports,
    simulate,
    snapshots,
    statements,
    system,
    widgets,
)
from app.api.routers import (
    bills as bills_router,
)
from app.api.routers import (
    coach as coach_router,
)
from app.core.config import get_settings
from app.db.database import get_db_session
from app.services import agent, ai_forgotten, coach, fx
from app.services import ai_insights as insights_service
from app.services import ai_jobs as ai_jobs_service
from app.services.processor import Processor

router = APIRouter()
for module in (
    system, ingest, devices, history, inbox, alerts, assistant, reports, catalog, widgets, ledger, own_accounts,
    ai, counterparties, statements, snapshots, digest, errors, ai_insights, maintenance, budgets,
    ai_jobs, ai_events, cards, agent_router, simulate, coach_router, receipts, bills_router, memories, alert_questions,
    debts, finance_plan,
):
    router.include_router(module.router)


async def run_cycle():
    async with get_db_session() as db:
        return await Processor(db, get_firefly(), get_llm()).cycle()


async def learn_senders():
    async with get_db_session() as db:
        return await Processor(db, get_firefly(), get_llm()).learn_senders()


async def run_reconcile():
    async with get_db_session() as db:
        return await Processor(db, get_firefly(), get_llm()).reconcile()


async def run_digest(weekly=False):
    async with get_db_session() as db:
        return await Processor(db, get_firefly(), get_llm()).digest(weekly=weekly)


async def run_weekly_digest():
    return await run_digest(weekly=True)


async def run_digest_tick():
    async with get_db_session() as db:
        return await Processor(db, get_firefly(), get_llm()).digest_tick()


async def run_monthly_narrative():
    async with get_db_session() as db:
        return await insights_service.run_monthly_narrative(db, get_firefly(), get_llm(), get_settings())


async def run_weekly_subscriptions():
    async with get_db_session() as db:
        return await insights_service.run_weekly_subscriptions(db, get_llm(), get_settings())


async def run_ai_jobs_tick():
    """Every minute: run the queued AI jobs and the waiting ones whose valley has come; send the grouped «waiting» alert."""
    async with get_db_session() as db:
        return await ai_jobs_service.run_due(ai_jobs_service.JobContext(db, get_firefly(), get_llm(), get_settings()))


async def run_forgotten_subscriptions():
    async with get_db_session() as db:
        return await ai_forgotten.run_monthly(db, get_llm(), get_settings())


async def run_agent_month_close():
    return await agent.run_month_close(get_firefly(), get_llm(), get_settings())


async def run_weekly_coach():
    return await coach.run_weekly(get_firefly(), get_llm(), get_settings())


async def run_agent_tick():
    return await agent.run_tick(get_firefly(), get_llm(), get_settings())


async def run_fx_trm():
    """Daily: store the latest TRM rates so USD movements convert from the cache (a failure leaves the cache serving)."""
    async with get_db_session() as db:
        return await fx.refresh(db, get_settings())
