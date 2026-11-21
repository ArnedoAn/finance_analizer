"""Per-device keys, one-time enrollment codes and scope enforcement (contract sections 1 and 14)."""
import hashlib
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, text, update

from app.core.timezone import now_utc
from app.db.models_devices import Device, EnrollCode

ADMIN_KEY = "test-admin-key-01234567890123456789"
PHONE_KEY = "test-phone-key-01234567890123456789"
ADMIN = {"X-API-Key": ADMIN_KEY}
API = "/api/v1"


def sha256(value):
    return hashlib.sha256(value.encode()).hexdigest()


async def new_code(client, scopes):
    response = await client.post(f"{API}/devices/enroll-token", headers=ADMIN, json={"scopes": scopes})
    assert response.status_code == 200, response.text
    return response.json()["code"]


async def enroll(client, scopes, name="phone"):
    return await client.post(f"{API}/devices/enroll", json={"code": await new_code(client, scopes), "name": name, "scopes": scopes})


async def enroll_key(client, scopes, name="phone"):
    response = await enroll(client, scopes, name)
    assert response.status_code == 201, response.text
    return response.json()


def auth(key):
    return {"X-API-Key": key}


async def device_row(db, device_id):
    row = await db.get(Device, device_id)
    await db.refresh(row)
    return row


async def test_enroll_token_shape_and_expiry(client):
    response = await client.post(f"{API}/devices/enroll-token", headers=ADMIN, json={"scopes": ["read", "review"]})
    body = response.json()
    assert response.status_code == 200 and set(body) == {"code", "scopes", "expires_at"}
    assert body["scopes"] == ["read", "review"] and len(body["code"]) >= 20
    remaining = datetime.fromisoformat(body["expires_at"]) - now_utc()
    assert timedelta(minutes=9) < remaining <= timedelta(minutes=10)


async def test_enroll_returns_key_once_and_stores_only_hashes(client, db):
    code = await new_code(client, ["ingest"])
    response = await client.post(f"{API}/devices/enroll", json={"code": code, "name": "  Pixel  ", "scopes": ["ingest"]})
    body = response.json()
    assert response.status_code == 201
    assert set(body) == {"device_id", "name", "scopes", "key", "created_at"}
    assert body["name"] == "Pixel" and body["scopes"] == ["ingest"]
    key = body["key"]
    assert key.startswith("cofre_") and len(key) >= 6 + 43
    device = await device_row(db, body["device_id"])
    assert device.key_hash == sha256(key)
    stored = str(list((await db.execute(text("SELECT * FROM devices"))).all())) + str(list((await db.execute(text("SELECT * FROM enroll_codes"))).all()))
    assert key not in stored and code not in stored
    assert sha256(code) in stored
    listing = (await client.get(f"{API}/devices", headers=ADMIN)).json()
    assert key not in str(listing) and "key_hash" not in str(listing)


async def test_enroll_code_is_single_use(client):
    code = await new_code(client, ["read"])
    payload = {"code": code, "name": "tablet", "scopes": ["read"]}
    first = await client.post(f"{API}/devices/enroll", json=payload)
    second = await client.post(f"{API}/devices/enroll", json=payload)
    assert first.status_code == 201
    assert second.status_code == 401 and second.json()["code"] == "invalid_code"
    assert (await client.get(f"{API}/devices", headers=ADMIN)).json()["total"] == 1


async def test_expired_unknown_and_used_codes_are_401(client, db):
    code = await new_code(client, ["read"])
    await db.execute(update(EnrollCode).values(expires_at=now_utc() - timedelta(seconds=1)))
    await db.commit()
    for candidate in (code, "does-not-exist"):
        response = await client.post(f"{API}/devices/enroll", json={"code": candidate, "name": "x", "scopes": ["read"]})
        assert response.status_code == 401 and response.json() == {"detail": "Invalid enrollment code", "code": "invalid_code"}
    assert (await client.get(f"{API}/devices", headers=ADMIN)).json()["total"] == 0


async def test_atomic_claim_rejects_code_already_marked_used(client, db):
    code = await new_code(client, ["read"])
    await db.execute(update(EnrollCode).values(used_at=now_utc()))
    await db.commit()
    response = await client.post(f"{API}/devices/enroll", json={"code": code, "name": "x", "scopes": ["read"]})
    assert response.status_code == 401
    assert (await db.execute(select(Device))).first() is None


async def test_enroll_code_failures_count_toward_ip_limit(client):
    payload = {"code": "wrong", "name": "x", "scopes": ["read"]}
    for _ in range(10):
        assert (await client.post(f"{API}/devices/enroll", json=payload)).status_code == 401
    limited = await client.post(f"{API}/devices/enroll", json=payload)
    assert limited.status_code == 429 and limited.json()["code"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) >= 1


async def test_admin_scope_cannot_be_enrolled(client, db):
    token = await client.post(f"{API}/devices/enroll-token", headers=ADMIN, json={"scopes": ["admin"]})
    assert token.status_code == 422 and token.json()["code"] == "validation_error"
    code = await new_code(client, ["read"])
    escalate = await client.post(f"{API}/devices/enroll", json={"code": code, "name": "x", "scopes": ["admin"]})
    assert escalate.status_code == 422 and escalate.json()["code"] == "validation_error"
    wider = await client.post(f"{API}/devices/enroll", json={"code": code, "name": "x", "scopes": ["read", "review"]})
    assert wider.status_code == 422
    # Rejected requests do not burn the code.
    assert (await client.post(f"{API}/devices/enroll", json={"code": code, "name": "x", "scopes": ["read"]})).status_code == 201
    assert all("admin" not in device.scopes for device in (await db.execute(select(Device))).scalars())


async def test_ingest_key_only_reaches_webhook_and_auth_check(client):
    key = (await enroll_key(client, ["ingest"]))["key"]
    assert (await client.get(f"{API}/auth/check", headers=auth(key))).status_code == 200
    payload = {"event_id": str(uuid4()), "app": "com.nequi.MobileApp", "message": "Pagaste $12.000"}
    assert (await client.post(f"{API}/notifications/webhook", headers=auth(key), json=payload)).status_code == 202
    denied = await client.get(f"{API}/inbox", headers=auth(key))
    assert denied.status_code == 403 and denied.json()["code"] == "insufficient_scope"
    assert (await client.get(f"{API}/devices", headers=auth(key))).status_code == 403
    assert (await client.post(f"{API}/devices/enroll-token", headers=auth(key), json={"scopes": ["read"]})).status_code == 403


async def test_read_cannot_approve_but_review_can(client, service):
    item = await service.inbox.add(
        source="app", source_ref="scope-approve", status="needs_review", sender="com.nequi.MobileApp",
        subject_or_title="Pagaste", raw_text="Pagaste $12.000", amount=Decimal("12000.00"), currency="COP",
        merchant="SUPERMERCADO CENTRAL", asset_account="Nequi", transaction_type="withdrawal",
    )
    await service.session.commit()
    read_key = (await enroll_key(client, ["read"], "reader"))["key"]
    review_key = (await enroll_key(client, ["read", "review"], "reviewer"))["key"]
    assert (await client.get(f"{API}/inbox", headers=auth(read_key))).status_code == 200
    denied = await client.post(f"{API}/inbox/{item.id}/approve", headers=auth(read_key))
    assert denied.status_code == 403 and denied.json()["code"] == "insufficient_scope"
    assert (await client.get(f"{API}/inbox", headers=auth(review_key))).status_code == 200
    approved = await client.post(f"{API}/inbox/{item.id}/approve", headers=auth(review_key))
    assert approved.status_code == 200, approved.text


async def test_revoking_a_device_cuts_access(client, db):
    created = await enroll_key(client, ["ingest"])
    key, device_id = created["key"], created["device_id"]
    assert (await client.get(f"{API}/auth/check", headers=auth(key))).status_code == 200
    revoked = await client.delete(f"{API}/devices/{device_id}", headers=ADMIN)
    assert revoked.status_code == 200 and revoked.json() == {"revoked": True}
    denied = await client.get(f"{API}/auth/check", headers=auth(key))
    assert denied.status_code == 401 and denied.json()["code"] == "invalid_key"
    item = (await client.get(f"{API}/devices", headers=ADMIN)).json()["items"][0]
    assert item["id"] == device_id and item["revoked_at"] is not None
    first_revocation = (await device_row(db, device_id)).revoked_at
    assert (await client.delete(f"{API}/devices/{device_id}", headers=ADMIN)).json() == {"revoked": True}
    assert (await device_row(db, device_id)).revoked_at == first_revocation
    missing = await client.delete(f"{API}/devices/9999", headers=ADMIN)
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"


async def test_expired_device_key_is_401(client, db):
    created = await enroll_key(client, ["ingest"])
    await db.execute(update(Device).values(expires_at=now_utc() - timedelta(seconds=1)))
    await db.commit()
    assert (await client.get(f"{API}/auth/check", headers=auth(created["key"]))).status_code == 401


async def test_last_used_at_updates_at_most_once_per_minute(client, db):
    created = await enroll_key(client, ["ingest"])
    headers = auth(created["key"])
    assert (await device_row(db, created["device_id"])).last_used_at is None
    assert (await client.get(f"{API}/auth/check", headers=headers)).status_code == 200
    first = (await device_row(db, created["device_id"])).last_used_at
    assert first is not None
    assert (await client.get(f"{API}/auth/check", headers=headers)).status_code == 200
    assert (await device_row(db, created["device_id"])).last_used_at == first
    stale = now_utc() - timedelta(minutes=2)
    await db.execute(update(Device).values(last_used_at=stale))
    await db.commit()
    assert (await client.get(f"{API}/auth/check", headers=headers)).status_code == 200
    assert (await device_row(db, created["device_id"])).last_used_at > stale + timedelta(minutes=1)


async def test_device_list_shape_and_admin_only(client):
    await enroll_key(client, ["read"], "reader")
    body = (await client.get(f"{API}/devices", headers=ADMIN)).json()
    assert body["total"] == 1
    assert set(body["items"][0]) == {"id", "name", "scopes", "created_at", "last_used_at", "revoked_at", "expires_at"}
    assert body["items"][0]["name"] == "reader" and body["items"][0]["scopes"] == ["read"]
    assert (await client.get(f"{API}/devices")).status_code == 401


async def test_legacy_phone_key_is_adopted_as_ingest_device(client, db):
    assert (await db.execute(select(Device))).first() is None
    assert (await client.get(f"{API}/auth/check", headers=auth(PHONE_KEY))).status_code == 200
    assert (await client.get(f"{API}/auth/check", headers=auth(PHONE_KEY))).status_code == 200
    devices = (await client.get(f"{API}/devices", headers=ADMIN)).json()["items"]
    assert [(d["name"], d["scopes"]) for d in devices] == [("phone-legacy", ["ingest"])]
    row = (await db.execute(select(Device))).scalar_one()
    assert row.key_hash == sha256(PHONE_KEY)
    assert PHONE_KEY not in str(list((await db.execute(text("SELECT * FROM devices"))).all()))
    assert (await client.get(f"{API}/inbox", headers=auth(PHONE_KEY))).status_code == 403
    await client.delete(f"{API}/devices/{row.id}", headers=ADMIN)
    assert (await client.get(f"{API}/auth/check", headers=auth(PHONE_KEY))).status_code == 401


async def test_phone_key_is_optional(client, settings):
    settings.phone_api_key = None
    assert (await client.get(f"{API}/auth/check", headers=auth(PHONE_KEY))).status_code == 401
    assert (await client.get(f"{API}/auth/check", headers=ADMIN)).status_code == 200


async def test_admin_key_is_server_side_principal_without_device(client, db):
    assert (await client.get(f"{API}/auth/check", headers=ADMIN)).status_code == 200
    assert (await db.execute(select(Device))).first() is None


async def test_errors_use_detail_and_code(client):
    missing = await client.get(f"{API}/inbox")
    assert missing.status_code == 401 and missing.json() == {"detail": "Invalid API key", "code": "invalid_key"}
    invalid = await client.post(f"{API}/devices/enroll", json={"name": "x"})
    assert invalid.status_code == 422 and invalid.json()["code"] == "validation_error"
    assert isinstance(invalid.json()["detail"], str)
    unknown = await client.get(f"{API}/no-such-route", headers=ADMIN)
    assert unknown.status_code == 404 and unknown.json()["code"] == "not_found"


async def test_enroll_and_revoke_are_audited_without_secrets(client, monkeypatch):
    calls = []

    async def record(session, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("app.services.audit.record", record)
    created = await enroll_key(client, ["ingest"])
    await client.delete(f"{API}/devices/{created['device_id']}", headers=ADMIN)
    assert [call["action"] for call in calls] == ["device.enroll", "device.revoke"]
    assert all(call["target_type"] == "device" and call["target_id"] == created["device_id"] for call in calls)
    assert created["key"] not in str(calls)
    assert calls[1]["principal"].name == "admin"


def test_enroll_route_declares_its_code_scope():
    from app.main import app
    from tests.test_route_scopes import api_routes, route_scopes

    scopes = {
        (route.path, tuple(sorted(route.methods))): route_scopes(route)
        for route in api_routes(app) if route.path.startswith("/api/v1/devices")
    }
    assert scopes[("/api/v1/devices/enroll", ("POST",))] == ["enroll_code"]
    assert scopes[("/api/v1/devices/enroll-token", ("POST",))] == ["admin"]
    assert scopes[("/api/v1/devices", ("GET",))] == ["admin"]
    assert scopes[("/api/v1/devices/{device_id}", ("DELETE",))] == ["admin"]
