"""Application-scoped clients and request-scoped repositories/services."""
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.firefly import FireflyClient
from app.clients.llm import LLMClient
from app.core.config import get_settings
from app.db.database import get_db
from app.services.processor import Processor

_firefly: FireflyClient | None = None
_llm: LLMClient | None = None


def require_api_key(x_api_key: Annotated[str | None, Header()] = None):
    expected = get_settings().finance_api_key.get_secret_value()
    if x_api_key is None or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid API key")


def get_firefly() -> FireflyClient:
    global _firefly
    if _firefly is None:
        _firefly = FireflyClient()
    return _firefly


def get_llm() -> LLMClient:
    global _llm
    if _llm is None:
        _llm = LLMClient()
    return _llm


def get_processor(db: Annotated[AsyncSession, Depends(get_db)]) -> Processor:
    return Processor(db, get_firefly(), get_llm())


async def cleanup_clients():
    global _firefly, _llm
    if _firefly is not None:
        await _firefly.close()
        _firefly = None
    if _llm is not None:
        await _llm.close()
        _llm = None
