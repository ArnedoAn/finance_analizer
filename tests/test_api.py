"""Offline API boundary checks for the single-owner contract."""

import pytest

ADMIN_KEY = "test-admin-key-01234567890123456789"
PHONE_KEY = "test-phone-key-01234567890123456789"


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "incorrect"}])
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/inbox", None),
        ("GET", "/api/v1/processing/statistics", None),
        ("GET", "/api/v1/mail/accounts", None),
        ("GET", "/api/v1/auth/check", None),
        ("GET", "/api/v1/reports/summary", None),
        ("POST", "/api/v1/reconcile/run", None),
        ("GET", "/api/v1/senders", None),
        ("POST", "/api/v1/notifications/webhook", {"message": "Compra"}),
        ("POST", "/api/v1/transactions/manual", {"amount": 100, "description": "Cafe"}),
    ],
)
async def test_api_requires_owner_key(client, headers, method, path, body):
    response = await client.request(method, path, headers=headers, json=body)
    assert response.status_code == 401


async def test_liveness_is_public(client):
    response = await client.get("/api/v1/health/live")
    assert response.status_code == 200


async def test_inbox_with_key_uses_isolated_database(client):
    response = await client.get("/api/v1/inbox", headers={"X-API-Key": ADMIN_KEY})
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


async def test_invalid_webhook_is_rejected(client):
    response = await client.post(
        "/api/v1/notifications/webhook", headers={"X-API-Key": ADMIN_KEY}, json={}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("amount", [0, -1])
async def test_manual_amount_must_be_positive(client, amount):
    response = await client.post(
        "/api/v1/transactions/manual",
        headers={"X-API-Key": ADMIN_KEY},
        json={"client_ref": "bad-amount", "amount": amount, "description": "Cafe"},
    )
    assert response.status_code == 422


async def test_phone_auth_check_has_no_processing_side_effects(client, clients):
    response = await client.get("/api/v1/auth/check", headers={"X-API-Key": PHONE_KEY})
    assert response.status_code == 200 and response.json() == {"ok": True}
    clients[0].create_transaction.assert_not_awaited()
    clients[1].analyze.assert_not_awaited()


async def test_webhook_event_retry_returns_same_inbox_id(client):
    from uuid import uuid4
    payload = {"event_id": str(uuid4()), "app": "com.nequi.MobileApp", "message": "Pagaste $12.000"}
    first = await client.post("/api/v1/notifications/webhook", headers={"X-API-Key": PHONE_KEY}, json=payload)
    assert first.status_code == 202
    assert first.json()["accepted"] is True and first.json()["status"] == "queued"
    second = await client.post("/api/v1/notifications/webhook", headers={"X-API-Key": PHONE_KEY}, json=payload)
    assert second.status_code == 200
    assert second.json() == {"accepted": False, "status": "duplicate", "inbox_id": first.json()["inbox_id"]}


@pytest.mark.parametrize("changes", [
    {"date": "not-a-date"}, {"date": "2026-02-30"},
    {"date": "2019-01-01T00:00:00Z"}, {"date": "2099-01-01T00:00:00Z"},
    {"account": "Does not exist"}, {"category": "Imaginary category"},
    {"amount": "50000000.01"}, {"client_ref": ""},
])
async def test_manual_validation_precedes_any_firefly_write(client, clients, service, changes):
    payload = {"client_ref": "invalid", "amount": "12000", "description": "Cafe", "account": "Nequi"}
    payload.update(changes)
    response = await client.post("/api/v1/transactions/manual", headers={"X-API-Key": ADMIN_KEY}, json=payload)
    assert response.status_code == 422
    clients[0].create_transaction.assert_not_awaited()
    assert await service.inbox.count() == 0


async def test_manual_category_casefold_and_client_ref_replay(client, clients):
    payload = {"client_ref": "manual-api", "amount": "12000", "description": "Cafe", "merchant": "SUPERMERCADO CENTRAL", "account": "Nequi", "category": "ALIMENTACIÓN"}
    first = await client.post("/api/v1/transactions/manual", headers={"X-API-Key": ADMIN_KEY}, json=payload)
    second = await client.post("/api/v1/transactions/manual", headers={"X-API-Key": ADMIN_KEY}, json=payload)
    assert first.status_code in {200, 201} and second.status_code in {200, 201}
    assert first.json()["inbox_id"] == second.json()["inbox_id"]
    split = clients[0].create_transaction.call_args.args[0]["transactions"][0]
    assert split["category_name"] == "Alimentación"
    clients[0].create_transaction.assert_awaited_once()


async def test_manual_network_failure_is_durable_502(client, clients, service):
    clients[0].create_transaction.side_effect = OSError("offline")
    response = await client.post("/api/v1/transactions/manual", headers={"X-API-Key": ADMIN_KEY}, json={"client_ref": "offline", "amount": 12000, "description": "Cafe", "account": "Nequi"})
    assert response.status_code == 502
    body = response.json()
    assert body["status"] == "failed" and body["code"] == "upstream_unavailable" and body["inbox_id"]
    row = await service.inbox.get(body["inbox_id"])
    assert row.status == "failed" and row.next_attempt_at is None


async def test_auth_lockout_limits_only_failures_and_success_does_not_reset(client):
    for _ in range(9):
        response = await client.get("/api/v1/auth/check", headers={"X-API-Key": "incorrect"})
        assert response.status_code == 401
    assert (await client.get("/api/v1/inbox", headers={"X-API-Key": ADMIN_KEY})).status_code == 200
    assert (await client.get("/api/v1/auth/check", headers={"X-API-Key": "incorrect"})).status_code == 401
    assert (await client.get("/api/v1/auth/check", headers={"X-API-Key": "incorrect"})).status_code == 429
    assert (await client.get("/api/v1/inbox", headers={"X-API-Key": ADMIN_KEY})).status_code == 200
    assert (await client.get("/api/v1/auth/check", headers={"X-API-Key": PHONE_KEY})).status_code == 200


async def test_unknown_manual_category_returns_suggestions(client):
    response = await client.post(
        "/api/v1/transactions/manual", headers={"X-API-Key": ADMIN_KEY},
        json={"client_ref": "unknown-category", "amount": 12000, "description": "Cafe", "account": "Nequi", "category": "Nonexistent"},
    )
    assert response.status_code == 422
    assert "Alimentación" in response.json()["detail"]


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/v1/inbox", None),
    ("GET", "/api/v1/inbox/1?include_raw=true", None),
    ("POST", "/api/v1/transactions/manual", {"client_ref": "phone", "amount": 12000, "description": "Cafe"}),
    ("POST", "/api/v1/inbox/1/approve", None),
    ("POST", "/api/v1/inbox/1/reject", None),
    ("POST", "/api/v1/inbox/1/void", None),
    ("POST", "/api/v1/inbox/1/reprocess", None),
    ("PATCH", "/api/v1/inbox/1", {"amount": 12000}),
    ("GET", "/api/v1/mail/accounts", None),
    ("POST", "/api/v1/mail/accounts/primary/test", None),
    ("POST", "/api/v1/senders", {"keyword": "example.com"}),
    ("POST", "/api/v1/reconcile/run", None),
])
async def test_phone_key_cannot_access_admin_routes(client, clients, method, path, body):
    # A valid ingest key is authenticated but lacks scope: 403, not 401 (contract section 1).
    response = await client.request(method, path, headers={"X-API-Key": PHONE_KEY}, json=body)
    assert response.status_code == 403
    clients[0].create_transaction.assert_not_awaited()


async def test_inbox_raw_requires_explicit_opt_in_and_claims_never_escape(client, service):
    from datetime import UTC, datetime

    row = await service.inbox.add(source="email", source_ref="privacy", status="needs_review",
        raw_text="private bank contents", claim_token="internal-secret", claim_until=datetime.now(UTC))
    await service.session.commit()
    headers = {"X-API-Key": ADMIN_KEY}
    listing = (await client.get("/api/v1/inbox", headers=headers)).json()["items"][0]
    detail = (await client.get(f"/api/v1/inbox/{row.id}", headers=headers)).json()
    raw = (await client.get(f"/api/v1/inbox/{row.id}?include_raw=true", headers=headers)).json()
    assert "raw_text" not in listing and "raw_text" not in detail
    assert raw["raw_text"] == "private bank contents"
    for payload in (listing, detail, raw):
        assert "claim_token" not in payload and "claim_until" not in payload


async def test_health_never_logs_into_mailboxes(client, settings, clients, monkeypatch):
    from unittest.mock import AsyncMock

    from app.api.routers import system as routes
    from app.core.config import MailAccount

    settings.mail_accounts = [MailAccount(name="primary", provider="gmail", username="user@gmail.com", password="private")]
    imap = AsyncMock()
    monkeypatch.setattr(routes, "IMAPClient", lambda account: imap)
    monkeypatch.setattr(routes, "get_firefly", lambda: clients[0])
    monkeypatch.setattr(routes, "get_llm", lambda: clients[1])
    response = await client.get("/api/v1/health", headers={"X-API-Key": ADMIN_KEY})
    assert response.status_code == 200
    imap.test.assert_not_awaited()
    imap.poll.assert_not_awaited()


async def test_mail_test_rate_limits_logins_per_account(client, settings, monkeypatch):
    from unittest.mock import AsyncMock

    from app.api.routers import system as routes
    from app.core.config import MailAccount

    settings.mail_accounts = [MailAccount(name=name, provider="gmail", username=name + "@gmail.com", password="private")
        for name in ("primary", "secondary")]
    imap = AsyncMock()
    imap.test.return_value = {"ok": True, "error": None}
    monkeypatch.setattr(routes, "IMAPClient", lambda account: imap)
    headers = {"X-API-Key": ADMIN_KEY}
    assert (await client.post("/api/v1/mail/accounts/primary/test", headers=headers)).status_code == 200
    limited = await client.post("/api/v1/mail/accounts/primary/test", headers=headers)
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"
    imap.test.assert_awaited_once()
    assert (await client.post("/api/v1/mail/accounts/secondary/test", headers=headers)).status_code == 200
    assert imap.test.await_count == 2


async def test_mail_accounts_read_view_hides_login_host_and_secrets(client, db, settings):
    from app.core.config import MailAccount
    from app.db.models import MailAccountState

    settings.mail_accounts = [MailAccount(
        name="primary", provider="gmail", username="owner@gmail.com", password="private-pass",
        authserv_ids=["mx.google.com"],
    )]
    db.add(MailAccountState(
        name="primary", last_uid=77, consecutive_failures=3,
        last_error="login owner@gmail.com failed at imap.gmail.com with private-pass",
    ))
    await db.commit()
    response = await client.get("/api/v1/mail/accounts", headers={"X-API-Key": ADMIN_KEY})
    assert response.status_code == 200
    account, = response.json()["accounts"]
    assert set(account) == {"name", "provider", "last_poll_at", "last_error", "consecutive_failures"}
    assert account["name"] == "primary" and account["provider"] == "gmail" and account["consecutive_failures"] == 3
    for secret in ("owner@gmail.com", "imap.gmail.com", "private-pass", "mx.google.com"):
        assert secret not in response.text
    assert "[redacted]" in account["last_error"]


async def test_quick_502_uses_the_upstream_unavailable_format(client, clients, service):
    clients[0].create_transaction.side_effect = OSError("offline secret-host")
    response = await client.post("/api/v1/transactions/quick", headers={"X-API-Key": ADMIN_KEY}, json={"text": "25k taxi", "client_ref": "quick-502"})
    assert response.status_code == 502
    body = response.json()
    assert set(body) == {"detail", "code", "inbox_id", "status"}
    assert body["code"] == "upstream_unavailable" and body["status"] == "failed"
    assert (await service.inbox.get(body["inbox_id"])).status == "failed"


async def test_statistics_amounts_today_are_two_decimal_strings(client, db):
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.db.models import InboxItem

    now = datetime.now(UTC)
    for ref, amount in (("stat-1", "12000"), ("stat-2", "500.5")):
        db.add(InboxItem(source="sms", source_ref=ref, occurred_at=now, status="created", amount=Decimal(amount), currency="COP", transaction_type="withdrawal"))
    await db.commit()
    body = (await client.get("/api/v1/processing/statistics", headers={"X-API-Key": ADMIN_KEY})).json()
    assert body["amounts_today"] == {"withdrawal": "12500.50"}


async def test_inbox_repository_filters_by_exact_source_ref(db):
    from datetime import UTC, datetime

    from app.db.models import InboxItem
    from app.db.repositories import InboxRepository

    now = datetime.now(UTC)
    for ref in ("mail:abc", "mail:abcd", "sms:1"):
        db.add(InboxItem(source="email", source_ref=ref, occurred_at=now, status="pending"))
    await db.commit()
    repository = InboxRepository(db)
    found = await repository.list(source_ref="mail:abc")
    assert [row.source_ref for row in found] == ["mail:abc"]
    assert await repository.count(source_ref="mail:abc") == 1
    assert await repository.count(source_ref="mail:missing") == 0
    assert await repository.count() == 3
