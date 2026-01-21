"""
API Dependencies

FastAPI dependency injection for services and clients.
Provides properly initialized services for each request.
"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.deepseek import DeepSeekClient
from app.clients.firefly import FireflyClient
from app.clients.gmail import GmailClient
from app.db.database import get_db, get_db_session as db_session_context
from app.services.email_processor import EmailProcessorService
from app.services.sync_service import SyncService
from app.services.transaction_service import TransactionService


# Singleton clients (created once, reused)
_gmail_client: GmailClient | None = None
_deepseek_client: DeepSeekClient | None = None
_firefly_client: FireflyClient | None = None


def get_gmail_client() -> GmailClient:
    """Get Gmail client singleton."""
    global _gmail_client
    if _gmail_client is None:
        _gmail_client = GmailClient()
    return _gmail_client


def get_deepseek_client() -> DeepSeekClient:
    """Get DeepSeek client singleton."""
    global _deepseek_client
    if _deepseek_client is None:
        _deepseek_client = DeepSeekClient()
    return _deepseek_client


def get_firefly_client() -> FireflyClient:
    """Get Firefly client singleton."""
    global _firefly_client
    if _firefly_client is None:
        _firefly_client = FireflyClient()
    return _firefly_client


async def cleanup_clients() -> None:
    """Cleanup client connections on shutdown."""
    global _gmail_client, _deepseek_client, _firefly_client
    
    if _deepseek_client:
        await _deepseek_client.close()
        _deepseek_client = None
    
    if _firefly_client:
        await _firefly_client.close()
        _firefly_client = None
    
    _gmail_client = None


@dataclass
class Services:
    """Container for all application services."""
    
    email_processor: EmailProcessorService
    sync_service: SyncService
    transaction_service: TransactionService
    gmail: GmailClient
    deepseek: DeepSeekClient
    firefly: FireflyClient


async def get_services(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AsyncGenerator[Services, None]:
    """
    Dependency that provides all application services.
    
    Usage:
        @router.post("/process")
        async def process(services: Services = Depends(get_services)):
            ...
    """
    gmail = get_gmail_client()
    deepseek = get_deepseek_client()
    firefly = get_firefly_client()
    
    sync_service = SyncService(db, firefly)
    transaction_service = TransactionService(db, firefly, sync_service)
    email_processor = EmailProcessorService(db, gmail, deepseek, firefly)
    
    yield Services(
        email_processor=email_processor,
        sync_service=sync_service,
        transaction_service=transaction_service,
        gmail=gmail,
        deepseek=deepseek,
        firefly=firefly,
    )


# Type alias for dependency injection
ServicesDep = Annotated[Services, Depends(get_services)]


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency that provides a database session for FastAPI endpoints.
    
    Usage:
        @router.get("/items")
        async def get_items(session: AsyncSession = Depends(get_db_session)):
            ...
    """
    async with db_session_context() as session:
        yield session


# Export the context manager for non-FastAPI use
async_session_factory = db_session_context
