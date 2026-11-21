"""Application-scoped clients, request-scoped services and scope-based API-key auth.

Every protected route declares exactly one scope through ``require_scope``. Keys
are per-device (contract section 1): only their SHA-256 hash is stored and the
``Principal`` interface is the same for the server-side admin key and devices.
"""
import hashlib
import secrets
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.firefly import FireflyClient
from app.clients.llm import LLMClient
from app.core.config import get_settings
from app.core.timezone import now_utc
from app.db.database import get_db
from app.db.models_devices import Device
from app.services.processor import Processor

SCOPES = ("ingest", "read", "review", "admin")
LEGACY_PHONE_DEVICE = "phone-legacy"
LAST_USED_INTERVAL = timedelta(minutes=1)

_firefly: FireflyClient | None = None
_llm: LLMClient | None = None
_auth_failures: OrderedDict[str, deque[float]] = OrderedDict()


@dataclass(frozen=True)
class Principal:
    """Authenticated caller. ``device_id`` is None only for the server-side admin key."""

    device_id: int | None
    name: str
    scopes: frozenset[str]

    def allows(self, scope: str) -> bool:
        # admin implies everything; review implies read; ingest is implied by nothing else.
        return "admin" in self.scopes or scope in self.scopes or (scope == "read" and "review" in self.scopes)


def _record_failure(request: Request, detail: str = "Invalid API key", code: str = "invalid_key"):
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
    raise HTTPException(status_code=401, detail={"detail": detail, "code": code})


def hash_secret(value: str) -> str:
    """SHA-256 hex digest; the only form in which device keys and enrollment codes are stored."""
    return hashlib.sha256(value.encode()).hexdigest()


async def _find_device(db: AsyncSession, key_hash: str) -> Device | None:
    return (await db.execute(select(Device).where(Device.key_hash == key_hash))).scalar_one_or_none()


async def _adopt_legacy_phone_key(db: AsyncSession, key_hash: str) -> Device | None:
    db.add(Device(name=LEGACY_PHONE_DEVICE, key_hash=key_hash, scopes=["ingest"]))
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent first use already adopted the key; use that row.
        await db.rollback()
    return await _find_device(db, key_hash)


async def _touch_last_used(db: AsyncSession, device: Device, now: datetime) -> None:
    # Write at most once per interval per device; the conditional UPDATE also
    # keeps concurrent requests from each writing.
    if device.last_used_at is not None and now - device.last_used_at < LAST_USED_INTERVAL:
        return
    await db.execute(
        update(Device)
        .where(Device.id == device.id, or_(Device.last_used_at.is_(None), Device.last_used_at < now - LAST_USED_INTERVAL))
        .values(last_used_at=now)
    )
    await db.commit()


async def _device_principal(db: AsyncSession, supplied: str) -> Principal | None:
    settings = get_settings()
    key_hash = hash_secret(supplied)
    device = await _find_device(db, key_hash)
    if device is None and settings.phone_api_key is not None and key_hash == hash_secret(settings.phone_api_key.get_secret_value()):
        device = await _adopt_legacy_phone_key(db, key_hash)
    now = now_utc()
    if device is None or device.revoked_at is not None or (device.expires_at is not None and device.expires_at <= now):
        return None
    await _touch_last_used(db, device, now)
    return Principal(device.id, device.name, frozenset(device.scopes))


async def authenticate(request: Request, x_api_key: str | None, db: AsyncSession) -> Principal:
    """Resolve the X-API-Key to a Principal once per request, or raise 401/429.

    FINANCE_API_KEY is the server-side admin (``device_id`` None, no database
    access). Every other key is looked up by its SHA-256 hash in ``devices``;
    revoked or expired devices are rejected like unknown keys.

    Legacy shared ingest key: when PHONE_API_KEY is set, its first valid use
    registers it as an ``ingest`` device named ``phone-legacy`` (hash only) and
    from then on it is just another device. To retire it, enroll the phone with
    a per-device key, revoke ``phone-legacy`` (DELETE /devices/{id}) and remove
    PHONE_API_KEY from the environment. Removing only the variable is not
    enough: the registered device keeps working until revoked.
    """
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    principal = None
    if x_api_key:
        if secrets.compare_digest(x_api_key.encode(), get_settings().finance_api_key.get_secret_value().encode()):
            principal = Principal(None, "admin", frozenset({"admin"}))
        else:
            principal = await _device_principal(db, x_api_key)
    if principal is None:
        _record_failure(request)
    request.state.principal = principal
    return principal


def require_scope(scope: str | None):
    """Dependency factory: authenticate, then require ``scope`` (None = any valid key)."""
    if scope is not None and scope not in SCOPES:
        raise ValueError(f"Unknown scope {scope}")

    async def dependency(
        request: Request,
        db: Annotated[AsyncSession, Depends(get_db)],
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> Principal:
        principal = await authenticate(request, x_api_key, db)
        if scope is not None and not principal.allows(scope):
            raise HTTPException(403, "Insufficient scope")
        return principal

    dependency.required_scope = scope
    return dependency


ANY_KEY = Depends(require_scope(None))
INGEST = Depends(require_scope("ingest"))
READ = Depends(require_scope("read"))
REVIEW = Depends(require_scope("review"))
ADMIN = Depends(require_scope("admin"))
ReadPrincipal = Annotated[Principal, READ]
ReviewPrincipal = Annotated[Principal, REVIEW]


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
