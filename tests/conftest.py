"""Offline v3 fixtures; TEST_DATABASE_URL opts into a real PostgreSQL backend."""
import base64
import os
from collections.abc import AsyncGenerator
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ["FINANCE_API_KEY"] = "test-admin-key-01234567890123456789"
os.environ["PHONE_API_KEY"] = "test-phone-key-01234567890123456789"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["FIELD_ENCRYPTION_KEY"] = base64.b64encode(os.urandom(32)).decode()

from app.core.config import Settings, get_settings
from app.db.models import Base, OwnAccountRow
from app.parsers import Extraction
from app.services import own_accounts


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    get_settings.cache_clear()
    config = Settings(
        finance_api_key="test-admin-key-01234567890123456789",
        phone_api_key="test-phone-key-01234567890123456789", scheduler_enabled=False,
        database_url=os.getenv("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:"),
        firefly_base_url="", firefly_api_token="", llm_api_key="", mail_accounts=[], own_accounts=[],
        # Pipeline tests exercise SMS/notification auto-creation; the production defaults are 0 (see test_sms_default_requires_review
        # and test_fixes_pipeline).
        manual_default_account="Nequi", sms_auto_approve_max_cop=Decimal("2000000"),
        notification_auto_approve_max_cop=Decimal("2000000"), _env_file=None,
    )
    monkeypatch.setattr("app.api.dependencies.get_settings", lambda: config)
    own_accounts.invalidate()  # the process snapshot must never leak from one test's database into the next
    yield config
    own_accounts.invalidate()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def db(settings) -> AsyncGenerator[AsyncSession, None]:
    url = settings.database_url
    if url.startswith(("postgres://", "postgresql://")):
        url = "postgresql+asyncpg://" + url.split("://", 1)[1]
    postgres = url.startswith("postgresql+")
    schema = "test_" + uuid4().hex
    options = {"connect_args": {"server_settings": {"search_path": schema}}} if postgres else {"poolclass": StaticPool}
    engine = create_async_engine(url, **options)
    try:
        async with engine.begin() as connection:
            if postgres:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            # The runtime source is the table: every test database starts with the same Nequi account the settings used to carry.
            session.add(OwnAccountRow(
                firefly_account="Nequi", bank="nequi", kind="wallet", last4=["1234"], aliases=["nequi"],
                sources=own_accounts.normalize_sources(["app:com.nequi.MobileApp", "sms:85954", "email:nequi.com.co"]),
            ))
            await session.commit()
            own_accounts.invalidate()
            yield session
    finally:
        async with engine.begin() as connection:
            if postgres:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            else:
                await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.fixture
def add_own_account(db):
    """Factory that inserts an own account row (and drops the process snapshot) like the API does after a write."""
    async def add(firefly_account, **fields):
        row = OwnAccountRow(firefly_account=firefly_account, **{"bank": "x", "kind": "wallet", "last4": [], "aliases": [], "sources": [], **fields})
        db.add(row)
        await db.commit()
        own_accounts.invalidate()
        return row

    return add


@pytest.fixture
def extraction():
    return Extraction(
        kind="withdrawal", amount=Decimal("12000.00"), currency="COP",
        merchant="SUPERMERCADO CENTRAL", occurred_at=None, date_only=None,
        account_hint="1234", counterparty_hint=None, template_id="nequi.pagaste", reason=None,
    )


@pytest.fixture
def clients(extraction):
    from app.clients.firefly import FireflyClient
    from app.clients.llm import LLMClient

    firefly = AsyncMock(spec=FireflyClient)
    firefly.check_connection.return_value = True
    firefly.accounts.return_value = [
        {"id": "1", "name": "Nequi", "type": "asset"},
        {"id": "2", "name": "SUPERMERCADO CENTRAL", "type": "expense"},
        {"id": "3", "name": "SALARIO", "type": "revenue"},
        {"id": "4", "name": "Bancolombia", "type": "asset"},
    ]
    firefly.categories.return_value = ["Alimentación", "Transporte"]
    firefly.find_by_external_id.return_value = None
    firefly.create_transaction.return_value = "tx-1"
    firefly.list_auto_transactions.return_value = []
    firefly.create_account.return_value = {"id": "5", "name": "OTRO", "type": "expense"}
    llm = AsyncMock(spec=LLMClient)
    llm.check_connection.return_value = True
    llm.analyze.return_value = replace(extraction, template_id="llm:test")
    return firefly, llm


@pytest.fixture
def service(db, settings, clients, extraction, monkeypatch):
    from app.services import processor as pipeline
    monkeypatch.setattr(pipeline, "parse", lambda *args, **kwargs: extraction)
    return pipeline.Processor(db, *clients, settings=settings)


@pytest_asyncio.fixture
async def client(db, settings, service, monkeypatch) -> AsyncGenerator[AsyncClient, None]:
    from app.api.dependencies import _auth_failures, get_db, get_processor
    from app.api.routers import ingest, system
    from app.api.routers import own_accounts as own_accounts_router
    from app.main import app

    async def process_isolated(item_id):
        await service.process(await service.inbox.get(item_id))

    monkeypatch.setattr(ingest, "process_background", process_isolated)
    monkeypatch.setattr(system, "get_settings", lambda: settings)
    monkeypatch.setattr(own_accounts_router, "get_firefly", lambda: service.firefly)

    async def isolated_db():
        yield db

    _auth_failures.clear()
    ingest._recent_events.clear()
    app.dependency_overrides[get_db] = isolated_db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_processor] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http
    finally:
        app.dependency_overrides.clear()
        _auth_failures.clear()
        ingest._recent_events.clear()
