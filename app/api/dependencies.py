"""Application-scoped clients and request-scoped repositories/services."""
import secrets
from collections import OrderedDict, deque
from time import monotonic
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.firefly import FireflyClient
from app.clients.llm import LLMClient
from app.core.config import get_settings
from app.db.database import get_db
from app.services.processor import Processor

_firefly: FireflyClient | None = None
_llm: LLMClient | None = None
_auth_failures: OrderedDict[str, deque[float]] = OrderedDict()


def _check_api_key(request: Request, x_api_key: str | None, *, allow_phone: bool):
    settings = get_settings()
    supplied = x_api_key.encode() if x_api_key is not None else b""
    admin_valid = secrets.compare_digest(supplied, settings.finance_api_key.get_secret_value().encode())
    phone_valid = allow_phone and secrets.compare_digest(supplied, settings.phone_api_key.get_secret_value().encode())
    if admin_valid or phone_valid:
        return
    # Only invalid credentials consume the socket-peer bucket. Valid credentials
    # always pass, even behind a proxy, and never erase an attacker's failures.
    peer = request.client.host if request.client else "unknown"
    now = monotonic()
    failures = _auth_failures.get(peer)
    if failures is None:
        failures = deque()
        _auth_failures[peer] = failures
    while failures and failures[0] <= now - 60:
        failures.popleft()
    _auth_failures.move_to_end(peer)
    if len(_auth_failures) > 10000:
        _auth_failures.popitem(last=False)
    if len(failures) >= 10:
        retry_after = max(1, int(60 - (now - failures[0])) + 1)
        raise HTTPException(429, "Too many authentication failures", headers={"Retry-After": str(retry_after)})
    failures.append(now)
    raise HTTPException(status_code=401, detail="Invalid API key")


async def require_api_key(request: Request, x_api_key: Annotated[str | None, Header()] = None):
    _check_api_key(request, x_api_key, allow_phone=False)


async def require_phone_or_admin_key(request: Request, x_api_key: Annotated[str | None, Header()] = None):
    _check_api_key(request, x_api_key, allow_phone=True)


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
