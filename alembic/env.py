"""Run schema migrations independently of application startup and secrets."""
import asyncio
import os
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.secrets import read_secret
from app.db import models  # noqa: F401
from app.db.database import Base

load_dotenv()


def database_url() -> str:
    """Same source as the app: DATABASE_URL_FILE first, then DATABASE_URL.

    Production never falls back to the local SQLite default: migrating a throwaway
    file while the real database stays on the old schema must fail loudly instead.
    """
    url = read_secret("DATABASE_URL")
    if url:
        return url
    if os.environ.get("APP_ENV") == "production":
        raise RuntimeError("DATABASE_URL or DATABASE_URL_FILE is required in production; refusing to migrate a local SQLite file")
    return "sqlite+aiosqlite:///./data/finance_v3.db"


url = database_url()


def configure(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online():
    if url.startswith("sqlite") and ":memory:" not in url:
        from sqlalchemy.engine import make_url
        database = make_url(url).database
        if database:
            Path(database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(configure)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Baseline adoption requires an online database; run alembic upgrade head")
else:
    asyncio.run(run_online())
