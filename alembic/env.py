"""Run schema migrations independently of application startup and secrets."""
import asyncio
import os
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import models  # noqa: F401
from app.db.database import Base

load_dotenv()
url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./data/finance_v3.db")


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
