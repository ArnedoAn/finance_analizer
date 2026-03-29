"""
API Dependencies

FastAPI dependency injection for services and clients.
Provides properly initialized services for each request.
"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.deepseek import DeepSeekClient
from app.clients.firefly import FireflyClient
from app.clients.gmail import GmailClient
from app.core.config import get_settings
from app.core.session import (
    DEFAULT_SESSION_ID,
    SESSION_COOKIE_NAME,
    SESSION_HEADER_NAME,
    TELEGRAM_CHAT_ID_HEADER_NAME,
    TELEGRAM_SESSION_HEADER_NAME,
    TELEGRAM_USER_ID_HEADER_NAME,
    USER_ID_HEADER_NAME,
    normalize_session_id,
    resolve_or_create_session_id,
)
from app.db.database import get_db, get_db_session as db_session_context
from app.services.email_processor import EmailProcessorService
from app.services.notification_processor import NotificationProcessorService
from app.services.sync_service import SyncService
from app.services.transaction_service import TransactionService


# Singleton clients (created once, reused)
_gmail_clients: dict[str, GmailClient] = {}
_deepseek_client: DeepSeekClient | None = None
_firefly_clients: dict[str, FireflyClient] = {}


@dataclass(frozen=True)
class RequestSessionContext:
    """Resolved request session metadata."""
    session_id: str
    is_new: bool


def apply_session_cookie(response: Response, session_id: str) -> None:
    """Attach session cookie and session header to the response."""
    settings = get_settings()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=settings.session_cookie_max_age_seconds,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/",
    )
    response.headers[SESSION_HEADER_NAME] = session_id


def get_request_session_context(
    request: Request,
    response: Response,
) -> RequestSessionContext:
    """
    Resolve session from request header/cookie and ensure persistence.

    Priority:
    1. X-Telegram-Session
    2. X-Telegram-User-ID (+ optional X-Telegram-Chat-ID)
    3. X-User-Id
    4. X-Session-ID header
    5. finance_session_id cookie
    6. New generated session id
    """
    header_session_id = request.headers.get(SESSION_HEADER_NAME)
    user_id = request.headers.get(USER_ID_HEADER_NAME)
    telegram_session_id = request.headers.get(TELEGRAM_SESSION_HEADER_NAME)
    telegram_user_id = request.headers.get(TELEGRAM_USER_ID_HEADER_NAME)
    telegram_chat_id = request.headers.get(TELEGRAM_CHAT_ID_HEADER_NAME)
    cookie_session_id = request.cookies.get(SESSION_COOKIE_NAME)

    session_id, is_new = resolve_or_create_session_id(
        header_session_id=header_session_id,
        cookie_session_id=cookie_session_id,
        user_id=user_id,
        telegram_session_id=telegram_session_id,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
    )
    response.headers[SESSION_HEADER_NAME] = session_id
    response.headers[TELEGRAM_SESSION_HEADER_NAME] = session_id

    normalized_cookie = normalize_session_id(cookie_session_id)
    if is_new or normalized_cookie != session_id:
        apply_session_cookie(response, session_id)

    return RequestSessionContext(session_id=session_id, is_new=is_new)


# Type alias for session dependency injection
SessionDep = Annotated[RequestSessionContext, Depends(get_request_session_context)]


def get_session_id(session: SessionDep) -> str:
    """Extract session ID from the request session context."""
    return session.session_id


# Type alias for session_id dependency injection
SessionIdDep = Annotated[str, Depends(get_session_id)]


def get_gmail_client(session_id: str = DEFAULT_SESSION_ID) -> GmailClient:
    """Get a Gmail client singleton scoped by session_id."""
    client = _gmail_clients.get(session_id)
    if client is None:
        client = GmailClient(session_id=session_id)
        _gmail_clients[session_id] = client
    return client


def get_request_gmail_client(session: SessionDep) -> GmailClient:
    """FastAPI dependency that returns the session-scoped Gmail client."""
    return get_gmail_client(session.session_id)


def get_deepseek_client() -> DeepSeekClient:
    """Get DeepSeek client singleton."""
    global _deepseek_client
    if _deepseek_client is None:
        _deepseek_client = DeepSeekClient()
    return _deepseek_client


def get_firefly_client(session_id: str = DEFAULT_SESSION_ID) -> FireflyClient:
    """Get a Firefly client singleton scoped by session_id."""
    client = _firefly_clients.get(session_id)
    if client is None:
        client = FireflyClient(session_id=session_id)
        _firefly_clients[session_id] = client
    return client


def get_request_firefly_client(session: SessionDep) -> FireflyClient:
    """FastAPI dependency that returns the session-scoped Firefly client."""
    return get_firefly_client(session.session_id)


async def cleanup_clients() -> None:
    """Cleanup client connections on shutdown."""
    global _gmail_clients, _deepseek_client, _firefly_clients
    
    if _deepseek_client:
        await _deepseek_client.close()
        _deepseek_client = None
    
    if _firefly_clients:
        for firefly in _firefly_clients.values():
            await firefly.close()
        _firefly_clients.clear()
    
    _gmail_clients.clear()


@dataclass
class Services:
    """Container for all application services."""
    
    session_id: str
    email_processor: EmailProcessorService
    notification_processor: NotificationProcessorService
    sync_service: SyncService
    transaction_service: TransactionService
    gmail: GmailClient
    deepseek: DeepSeekClient
    firefly: FireflyClient


async def get_services(
    db: Annotated[AsyncSession, Depends(get_db)],
    session: SessionDep,
) -> AsyncGenerator[Services, None]:
    """
    Dependency that provides all application services.
    
    Usage:
        @router.post("/process")
        async def process(services: Services = Depends(get_services)):
            ...
    """
    session_id = session.session_id
    gmail = get_gmail_client(session_id)
    deepseek = get_deepseek_client()
    firefly = get_firefly_client(session_id)
    
    sync_service = SyncService(db, firefly, session_id=session_id)
    transaction_service = TransactionService(
        db,
        firefly,
        sync_service,
        session_id=session_id,
    )
    email_processor = EmailProcessorService(
        db,
        gmail,
        deepseek,
        firefly,
        session_id=session_id,
    )
    notification_processor = NotificationProcessorService(
        db,
        deepseek,
        firefly,
        session_id=session_id,
    )
    
    yield Services(
        session_id=session_id,
        email_processor=email_processor,
        notification_processor=notification_processor,
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
