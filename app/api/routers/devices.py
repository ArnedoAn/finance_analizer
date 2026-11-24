"""Device enrollment and per-device keys (contract sections 1 and 14)."""
import secrets
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select, update

from app.api.dependencies import ADMIN, SCOPES, Principal, _record_failure, hash_secret
from app.api.routers.common import DB
from app.core.config import get_settings
from app.core.timezone import now_utc
from app.db.models_devices import Device, EnrollCode, EnrollQrView
from app.services import enroll_qr

router = APIRouter()

# `admin` never leaves the server, so it can be neither requested nor granted by enrollment.
EnrollScope = Literal["ingest", "read", "review"]


def _enroll_code_gate() -> None:
    """Marker: /devices/enroll is authenticated by its one-time code, not by an API key."""


_enroll_code_gate.required_scope = "enroll_code"
ENROLL_CODE = Depends(_enroll_code_gate)


class EnrollTokenRequest(BaseModel):
    scopes: list[EnrollScope] = Field(min_length=1)
    ttl_minutes: int = Field(default=10, ge=1, le=10)


class EnrollQrRequest(BaseModel):
    ttl_minutes: int = Field(default=10, ge=1, le=10)
    # Only a label for the audit log: the app asks the owner for the device name itself.
    name_hint: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None


class EnrollRequest(BaseModel):
    code: str = Field(min_length=1, max_length=200)
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    scopes: list[EnrollScope] = Field(min_length=1)


def _canonical(scopes) -> list[str]:
    return [scope for scope in SCOPES if scope in set(scopes)]


def _device_data(device: Device) -> dict:
    return {
        "id": device.id, "name": device.name, "scopes": list(device.scopes), "created_at": device.created_at,
        "last_used_at": device.last_used_at, "revoked_at": device.revoked_at, "expires_at": device.expires_at,
    }


def _stage_enroll_code(db, scopes: list[str], expires_at, code: str | None = None) -> str:
    """Add one single-use enrollment code (hash only) to the session; the caller commits. Random unless ``code`` is given."""
    code = code or secrets.token_urlsafe(16)
    db.add(EnrollCode(code_hash=hash_secret(code), scopes=scopes, expires_at=expires_at))
    return code


@router.post("/devices/enroll-token", dependencies=[ADMIN])
async def create_enroll_token(body: EnrollTokenRequest, db: DB):
    scopes = _canonical(body.scopes)
    expires_at = now_utc() + timedelta(minutes=body.ttl_minutes)
    code = _stage_enroll_code(db, scopes, expires_at)
    await db.commit()
    return {"code": code, "scopes": scopes, "expires_at": expires_at}


def _public_base(request: Request) -> str:
    return get_settings().public_api_base_url or str(request.base_url).rstrip("/")


@router.post("/devices/enroll-qr", status_code=201)
async def create_enroll_qr(request: Request, db: DB, principal: Annotated[Principal, ADMIN], body: EnrollQrRequest | None = None):
    from app.services import audit

    body = body or EnrollQrRequest()
    base = _public_base(request)
    expires_at = now_utc() + timedelta(minutes=body.ttl_minutes)
    view_token = secrets.token_urlsafe(32)  # 43 characters, 256 bits
    ingest, interactive = enroll_qr.derive_codes(view_token)
    _stage_enroll_code(db, enroll_qr.INGEST_SCOPES, expires_at, ingest)
    _stage_enroll_code(db, enroll_qr.INTERACTIVE_SCOPES, expires_at, interactive)
    view = EnrollQrView(token_hash=hash_secret(view_token), expires_at=expires_at)
    db.add(view)
    await db.flush()
    # Neither the codes nor the view token go to the audit log.
    await audit.record(
        db, action="device.enroll_qr", target_type="enroll_qr_view", target_id=view.id,
        detail={"ttl_minutes": body.ttl_minutes, "name_hint": body.name_hint, "expires_at": expires_at}, principal=principal,
    )
    await db.commit()
    return {"view_url": f"{base}/api/v1/enroll/view/{view_token}", "expires_at": expires_at}


def _enroll_view_gate() -> None:
    """Marker: /enroll/view/{token} is public on purpose; the unguessable token (and the live one-time codes behind it) is the credential."""


_enroll_view_gate.required_scope = "enroll_view"
ENROLL_VIEW = Depends(_enroll_view_gate)


@router.get("/enroll/view/{token}", response_class=HTMLResponse, dependencies=[ENROLL_VIEW])
async def view_enroll_qr(token: str, request: Request, db: DB):
    now = now_utc()
    token = token[:enroll_qr.MAX_TOKEN_LENGTH]
    ingest, interactive = enroll_qr.derive_codes(token)
    hashes = [hash_secret(ingest), hash_secret(interactive)]
    view = (await db.execute(select(EnrollQrView).where(EnrollQrView.token_hash == hash_secret(token)))).scalar_one_or_none()
    rows = {row.code_hash: row for row in (await db.execute(select(EnrollCode).where(EnrollCode.code_hash.in_(hashes)))).scalars()}
    first, second = (rows.get(code_hash) for code_hash in hashes)
    live = (
        view is not None and view.expires_at > now and first is not None and second is not None
        and all(row.used_at is None and row.expires_at > now for row in (first, second))
    )
    # The page is rendered whether or not the link is alive, so an unknown, expired and used link cost the same.
    expires_at = min(view.expires_at, first.expires_at, second.expires_at) if live else now
    page = enroll_qr.render_page(enroll_qr.build_link(_public_base(request), ingest, interactive), expires_at, now)
    if live:
        return HTMLResponse(page, headers=enroll_qr.SECURITY_HEADERS)
    try:
        _record_failure(request, "Invalid enrollment view", "invalid_view")
    except HTTPException as exc:
        if exc.status_code == 429:
            raise
    return HTMLResponse(enroll_qr.render_invalid_page(), status_code=404, headers=enroll_qr.SECURITY_HEADERS)


@router.post("/devices/enroll", status_code=201, dependencies=[ENROLL_CODE])
async def enroll_device(body: EnrollRequest, request: Request, db: DB):
    from app.services import audit

    now = now_utc()
    row = (await db.execute(select(EnrollCode).where(EnrollCode.code_hash == hash_secret(body.code)))).scalar_one_or_none()
    if row is None or row.used_at is not None or row.expires_at <= now:
        _record_failure(request, "Invalid enrollment code", "invalid_code")
    scopes = _canonical(body.scopes)
    if not set(scopes) <= set(row.scopes):
        raise HTTPException(422, "Requested scopes exceed the enrollment code scopes")
    # Single atomic claim: of two simultaneous requests only one updates the row.
    claimed = await db.execute(
        update(EnrollCode)
        .where(EnrollCode.id == row.id, EnrollCode.used_at.is_(None), EnrollCode.expires_at > now)
        .values(used_at=now)
    )
    if claimed.rowcount != 1:
        _record_failure(request, "Invalid enrollment code", "invalid_code")
    key = "cofre_" + secrets.token_urlsafe(32)
    device = Device(name=body.name, key_hash=hash_secret(key), scopes=scopes)
    db.add(device)
    await db.flush()
    await audit.record(db, action="device.enroll", target_type="device", target_id=device.id, detail={"name": device.name, "scopes": scopes})
    await db.commit()
    return {"device_id": device.id, "name": device.name, "scopes": scopes, "key": key, "created_at": device.created_at}


@router.get("/devices", dependencies=[ADMIN])
async def list_devices(db: DB):
    devices = (await db.execute(select(Device).order_by(Device.id))).scalars().all()
    return {"items": [_device_data(device) for device in devices], "total": len(devices)}


@router.delete("/devices/{device_id}")
async def revoke_device(device_id: int, db: DB, principal: Annotated[Principal, ADMIN]):
    from app.services import audit

    device = await db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "Device not found")
    if device.revoked_at is None:
        device.revoked_at = now_utc()
        await audit.record(db, action="device.revoke", target_type="device", target_id=device.id, detail={"name": device.name}, principal=principal)
        await db.commit()
    return {"revoked": True}
