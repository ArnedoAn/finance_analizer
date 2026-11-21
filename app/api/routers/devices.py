"""Device enrollment and per-device keys (contract sections 1 and 14)."""
import secrets
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select, update

from app.api.dependencies import ADMIN, SCOPES, Principal, _record_failure, hash_secret
from app.api.routers.common import DB
from app.core.timezone import now_utc
from app.db.models_devices import Device, EnrollCode

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


@router.post("/devices/enroll-token", dependencies=[ADMIN])
async def create_enroll_token(body: EnrollTokenRequest, db: DB):
    scopes = _canonical(body.scopes)
    code = secrets.token_urlsafe(16)
    expires_at = now_utc() + timedelta(minutes=body.ttl_minutes)
    db.add(EnrollCode(code_hash=hash_secret(code), scopes=scopes, expires_at=expires_at))
    await db.commit()
    return {"code": code, "scopes": scopes, "expires_at": expires_at}


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
