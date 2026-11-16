"""Isolated v2 fixtures: no local .env, network, scheduler, or persistent DB."""

import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ["FINANCE_API_KEY"] = "test-key"
os.environ["SCHEDULER_ENABLED"] = "false"

from app.core.config import Settings, get_settings  # noqa: E402
from app.db.models import Base  # noqa: E402


@pytest.fixture(autouse=True)
def settings():
    """Never load developer secrets from .env or retain cached test settings."""
    get_settings.cache_clear()
    config = Settings(
        finance_api_key="test-key",
        database_url="sqlite+aiosqlite:///:memory:",
        scheduler_enabled=False,
        firefly_base_url="",
        firefly_api_token="",
        llm_api_key="",
        mail_accounts=[],
        _env_file=None,
    )
    yield config
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(db, settings) -> AsyncGenerator[AsyncClient, None]:
    from app.api.dependencies import get_db
    from app.main import app

    async def isolated_db():
        yield db

    app.dependency_overrides[get_db] = isolated_db
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        # ASGITransport deliberately does not start lifespan/external clients.
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield http
    finally:
        app.dependency_overrides.clear()
