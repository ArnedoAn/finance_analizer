"""
Test configuration and fixtures.
"""

import asyncio
from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.db.database import Base
from app.main import app


# Test database URL
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Create test database session."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    
    async with session_factory() as session:
        yield session
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    
    await engine.dispose()


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Create test HTTP client."""
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def sample_email_data() -> dict[str, Any]:
    """Sample email data for testing."""
    return {
        "message_id": "<test-123@gmail.com>",
        "internal_id": "abc123",
        "thread_id": "thread123",
        "subject": "Confirmación de pago - Amazon",
        "sender": "noreply@amazon.com",
        "recipient": "user@gmail.com",
        "date": "2024-01-15T10:30:00Z",
        "body_text": """
        Hola,
        
        Tu pago de $45.99 USD ha sido procesado exitosamente.
        
        Detalles de la compra:
        - Producto: Echo Dot
        - Monto: $45.99 USD
        - Fecha: 15 de enero de 2024
        - Método: Tarjeta VISA ****1234
        
        Gracias por tu compra.
        
        Amazon
        """,
        "body_html": "",
        "snippet": "Tu pago de $45.99 USD ha sido procesado",
        "labels": ["INBOX"],
    }


@pytest.fixture
def sample_analysis_data() -> dict[str, Any]:
    """Sample analysis data for testing."""
    return {
        "amount": "45.99",
        "currency": "USD",
        "date": "2024-01-15",
        "description": "Compra Echo Dot en Amazon",
        "merchant": "Amazon",
        "suggested_category": "Compras",
        "suggested_account_name": "Tarjeta VISA",
        "transaction_type": "withdrawal",
        "confidence_score": 0.95,
    }
