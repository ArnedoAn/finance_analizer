"""Aggregate the /api/v1 domain routers and expose scheduler job callbacks.

Every route except health/live and health/ready declares a scope from
``app.api.dependencies``; tests enforce that no route is left unauthenticated.
"""
from fastapi import APIRouter

from app.api.dependencies import get_firefly, get_llm
from app.api.routers import (
    alerts,
    assistant,
    catalog,
    devices,
    history,
    inbox,
    ingest,
    ledger,
    own_accounts,
    reports,
    system,
    widgets,
)
from app.db.database import get_db_session
from app.services.processor import Processor

router = APIRouter()
for module in (system, ingest, devices, history, inbox, alerts, assistant, reports, catalog, widgets, ledger, own_accounts):
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
