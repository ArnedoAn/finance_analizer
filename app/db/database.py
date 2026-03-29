"""
Database Configuration and Session Management

Provides async SQLAlchemy engine, session factory, and database utilities.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""
    pass


# Global engine and session factory
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _rebuild_legacy_cache_tables_sqlite(sync_conn: object) -> None:
    """
    Rebuild legacy cache tables to remove global unique constraints.

    Older SQLite schemas used global UNIQUE columns (name/tag/keyword/firefly_id).
    This rebuild keeps data and enables per-session uniqueness.
    """
    sync_conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS account_cache_new (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            session_id VARCHAR(64) NOT NULL DEFAULT 'default',
            firefly_id VARCHAR(100) NOT NULL,
            name VARCHAR(255) NOT NULL,
            account_type VARCHAR(50) NOT NULL,
            currency_code VARCHAR(3) NOT NULL DEFAULT 'USD',
            active BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """
    )
    sync_conn.exec_driver_sql(
        """
        INSERT INTO account_cache_new (
            id, session_id, firefly_id, name, account_type, currency_code, active, created_at, updated_at
        )
        SELECT
            id,
            COALESCE(session_id, 'default'),
            firefly_id,
            name,
            account_type,
            currency_code,
            active,
            created_at,
            updated_at
        FROM account_cache
        """
    )
    sync_conn.exec_driver_sql("DROP TABLE account_cache")
    sync_conn.exec_driver_sql("ALTER TABLE account_cache_new RENAME TO account_cache")

    sync_conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS category_cache_new (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            session_id VARCHAR(64) NOT NULL DEFAULT 'default',
            firefly_id VARCHAR(100) NOT NULL,
            name VARCHAR(255) NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """
    )
    sync_conn.exec_driver_sql(
        """
        INSERT INTO category_cache_new (id, session_id, firefly_id, name, created_at, updated_at)
        SELECT
            id,
            COALESCE(session_id, 'default'),
            firefly_id,
            name,
            created_at,
            updated_at
        FROM category_cache
        """
    )
    sync_conn.exec_driver_sql("DROP TABLE category_cache")
    sync_conn.exec_driver_sql("ALTER TABLE category_cache_new RENAME TO category_cache")

    sync_conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS tag_cache_new (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            session_id VARCHAR(64) NOT NULL DEFAULT 'default',
            firefly_id VARCHAR(100) NOT NULL,
            tag VARCHAR(255) NOT NULL,
            created_at DATETIME NOT NULL
        )
        """
    )
    sync_conn.exec_driver_sql(
        """
        INSERT INTO tag_cache_new (id, session_id, firefly_id, tag, created_at)
        SELECT
            id,
            COALESCE(session_id, 'default'),
            firefly_id,
            tag,
            created_at
        FROM tag_cache
        """
    )
    sync_conn.exec_driver_sql("DROP TABLE tag_cache")
    sync_conn.exec_driver_sql("ALTER TABLE tag_cache_new RENAME TO tag_cache")

    sync_conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS known_senders_new (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            session_id VARCHAR(64) NOT NULL DEFAULT 'default',
            keyword VARCHAR(100) NOT NULL,
            sender_name VARCHAR(255) NOT NULL,
            sender_type VARCHAR(50) NOT NULL DEFAULT 'bank',
            is_active BOOLEAN NOT NULL DEFAULT 1,
            is_auto_learned BOOLEAN NOT NULL DEFAULT 0,
            confidence_score INTEGER NOT NULL DEFAULT 100,
            emails_matched INTEGER NOT NULL DEFAULT 0,
            last_matched_at DATETIME,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """
    )
    sync_conn.exec_driver_sql(
        """
        INSERT INTO known_senders_new (
            id, session_id, keyword, sender_name, sender_type, is_active, is_auto_learned,
            confidence_score, emails_matched, last_matched_at, created_at, updated_at
        )
        SELECT
            id,
            COALESCE(session_id, 'default'),
            keyword,
            sender_name,
            sender_type,
            is_active,
            is_auto_learned,
            confidence_score,
            emails_matched,
            last_matched_at,
            created_at,
            updated_at
        FROM known_senders
        """
    )
    sync_conn.exec_driver_sql("DROP TABLE known_senders")
    sync_conn.exec_driver_sql("ALTER TABLE known_senders_new RENAME TO known_senders")


def _ensure_session_columns(sync_conn: object) -> None:
    """
    Backfill session columns/indexes for existing databases.

    This keeps older SQLite/PostgreSQL databases compatible when
    new session-aware fields are introduced without Alembic migrations.
    """
    inspector = inspect(sync_conn)
    table_names = set(inspector.get_table_names())
    dialect_name = sync_conn.dialect.name

    session_tables = (
        "processed_emails",
        "audit_logs",
        "processed_notifications",
        "transaction_fingerprints",
        "scheduler_job_logs",
        "account_cache",
        "category_cache",
        "tag_cache",
        "known_senders",
    )

    for table_name in session_tables:
        if table_name not in table_names:
            continue
        existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        if "session_id" in existing_columns:
            continue

        if dialect_name == "sqlite":
            sync_conn.exec_driver_sql(
                f"ALTER TABLE {table_name} "
                "ADD COLUMN session_id VARCHAR(64) NOT NULL DEFAULT 'default'"
            )
        else:
            sync_conn.exec_driver_sql(
                f"ALTER TABLE {table_name} "
                "ADD COLUMN IF NOT EXISTS session_id VARCHAR(64) NOT NULL DEFAULT 'default'"
            )

    # Legacy SQLite tables used global unique constraints; rebuild for session scope.
    if (
        dialect_name == "sqlite"
        and {"account_cache", "category_cache", "tag_cache", "known_senders"}.issubset(table_names)
    ):
        account_indexes = inspector.get_indexes("account_cache")
        has_legacy_index = any(
            idx.get("name") == "ix_account_cache_name_type"
            for idx in account_indexes
        )
        if has_legacy_index:
            _rebuild_legacy_cache_tables_sqlite(sync_conn)

    # Replace pre-session unique index with session-scoped one if needed.
    if "processed_emails" in table_names:
        sync_conn.exec_driver_sql("DROP INDEX IF EXISTS ix_processed_emails_unique")
        sync_conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_processed_emails_session_unique "
            "ON processed_emails (session_id, message_id, internal_id)"
        )

    # Ensure session-aware indexes exist.
    if "audit_logs" in table_names:
        sync_conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_audit_logs_session_email "
            "ON audit_logs (session_id, email_message_id, email_internal_id)"
        )
        sync_conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_audit_logs_session_date "
            "ON audit_logs (session_id, email_date)"
        )
        sync_conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_audit_logs_session_status_date "
            "ON audit_logs (session_id, status, created_at)"
        )

    if "processed_notifications" in table_names:
        sync_conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_processed_notifications_session_hash "
            "ON processed_notifications (session_id, notification_hash)"
        )

    if "transaction_fingerprints" in table_names:
        sync_conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_fingerprint_session_hash_date "
            "ON transaction_fingerprints (session_id, fingerprint_hash, transaction_date)"
        )

    if "scheduler_job_logs" in table_names:
        sync_conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_scheduler_job_logs_session_job_date "
            "ON scheduler_job_logs (session_id, job_name, started_at)"
        )

    # Session-aware cache indexes and constraints.
    if "account_cache" in table_names:
        # Drop legacy global uniqueness (single-tenant schema).
        if dialect_name == "postgresql":
            sync_conn.exec_driver_sql(
                "ALTER TABLE account_cache DROP CONSTRAINT IF EXISTS account_cache_firefly_id_key"
            )
        sync_conn.exec_driver_sql("DROP INDEX IF EXISTS ix_account_cache_name_type")
        sync_conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_account_cache_session_firefly "
            "ON account_cache (session_id, firefly_id)"
        )
        sync_conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_account_cache_session_name_type "
            "ON account_cache (session_id, name, account_type)"
        )

    if "category_cache" in table_names:
        if dialect_name == "postgresql":
            sync_conn.exec_driver_sql(
                "ALTER TABLE category_cache DROP CONSTRAINT IF EXISTS category_cache_firefly_id_key"
            )
            sync_conn.exec_driver_sql(
                "ALTER TABLE category_cache DROP CONSTRAINT IF EXISTS category_cache_name_key"
            )
        sync_conn.exec_driver_sql("DROP INDEX IF EXISTS ix_category_cache_name")
        sync_conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_category_cache_session_firefly "
            "ON category_cache (session_id, firefly_id)"
        )
        sync_conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_category_cache_session_name "
            "ON category_cache (session_id, name)"
        )

    if "tag_cache" in table_names:
        if dialect_name == "postgresql":
            sync_conn.exec_driver_sql(
                "ALTER TABLE tag_cache DROP CONSTRAINT IF EXISTS tag_cache_firefly_id_key"
            )
            sync_conn.exec_driver_sql(
                "ALTER TABLE tag_cache DROP CONSTRAINT IF EXISTS tag_cache_tag_key"
            )
        sync_conn.exec_driver_sql("DROP INDEX IF EXISTS ix_tag_cache_tag")
        sync_conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_tag_cache_session_firefly "
            "ON tag_cache (session_id, firefly_id)"
        )
        sync_conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_tag_cache_session_tag "
            "ON tag_cache (session_id, tag)"
        )

    if "known_senders" in table_names:
        # Merge duplicates from per-session era before restoring global uniqueness.
        sync_conn.exec_driver_sql(
            """
            DELETE FROM known_senders
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM known_senders
                GROUP BY keyword
            )
            """
        )
        if dialect_name == "postgresql":
            sync_conn.exec_driver_sql(
                "ALTER TABLE known_senders DROP CONSTRAINT IF EXISTS known_senders_keyword_key"
            )
            sync_conn.exec_driver_sql(
                "DROP INDEX IF EXISTS ix_known_senders_session_keyword"
            )
        sync_conn.exec_driver_sql("DROP INDEX IF EXISTS ix_known_senders_active")
        sync_conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_known_senders_active "
            "ON known_senders (is_active, keyword)"
        )
        sync_conn.exec_driver_sql("DROP INDEX IF EXISTS ix_known_senders_session_keyword")
        sync_conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_known_senders_keyword "
            "ON known_senders (keyword)"
        )


def _get_engine() -> AsyncEngine:
    """Get or create the database engine."""
    global _engine
    
    if _engine is None:
        settings = get_settings()
        
        # Ensure data directory exists for SQLite
        if "sqlite" in settings.database_url:
            db_path = settings.database_url.split("///")[-1]
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.debug,
            future=True,
            # SQLite-specific settings
            connect_args={"check_same_thread": False}
            if "sqlite" in settings.database_url
            else {},
        )
        logger.info("database_engine_created", url=settings.database_url.split("@")[-1])
    
    return _engine


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the session factory."""
    global _session_factory
    
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=_get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
    
    return _session_factory


async def init_db() -> None:
    """
    Initialize the database by creating all tables.
    
    Should be called on application startup.
    """
    engine = _get_engine()
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_session_columns)
    
    logger.info("database_initialized")


async def close_db() -> None:
    """
    Close database connections.
    
    Should be called on application shutdown.
    """
    global _engine, _session_factory
    
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("database_connections_closed")


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency that provides a database session.
    
    Usage in FastAPI:
        @app.get("/items")
        async def get_items(db: AsyncSession = Depends(get_db)):
            ...
    """
    session_factory = _get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager for getting a database session.
    
    Usage:
        async with get_db_session() as session:
            result = await session.execute(query)
    """
    session_factory = _get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
