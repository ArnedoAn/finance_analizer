"""History import: sender rules by exact address, mailbox range search, SMS dedupe with live capture."""
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.clients.imap import MailHeader, MailMessage
from app.db.repositories import KnownSenderRepository, MailStateRepository
from app.security.sender_trust import AuthResult, evaluate_email
from tests.test_devices_v4 import API, auth, enroll_key
from tests.test_imap_v3 import FakeIMAP, mail  # noqa: F401  (fixture)

PHONE = {"X-API-Key": "test-phone-key-01234567890123456789"}


def test_address_rule_trusts_only_that_sender():
    rule = [SimpleNamespace(keyword="alertas@bank.test", status="active", require_auth=True)]
    signed = AuthResult(trusted=True, domain="bank.test")
    assert evaluate_email(MailHeader(1, from_address="Alertas@Bank.test", auth=signed), rule).reason == "approved_sender"
    assert evaluate_email(MailHeader(1, from_address="promo@bank.test", auth=signed), rule).reason == "unknown_sender"
    assert evaluate_email(MailHeader(1, from_address="alertas@bank.test"), rule).reason == "authentication_failed"


@pytest.mark.parametrize("keyword", ["someone@gmail.com", "gmail.com", "bad@", "no-domain", "a b@bank.test"])
async def test_sender_repository_rejects_generic_or_invalid(db, keyword):
    with pytest.raises(ValueError):
        await KnownSenderRepository(db).add(keyword)


async def test_history_search_reads_gmail_all_mail_by_sender_and_range(mail):  # noqa: F811
    client, connection = mail
    selected = []
    connection.list = lambda: ("OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\All \\HasNoChildren) "/" "[Gmail]/Todos"'])
    connection.select = lambda folder, readonly: (selected.append(folder), ("OK", []))[1]
    validity, messages = await client.history(date(2026, 11, 1), date(2026, 11, 1), ["alertas@bank.test", "bank2.test"], lambda h: True)
    assert validity == 123 and len(messages) == 2
    assert selected == ['"[Gmail]/Todos"']
    search = next(c for c in connection.commands if c[0] == "search")
    assert search[2] == 'SINCE 31-Oct-2026 BEFORE 03-Nov-2026 OR FROM "alertas@bank.test" FROM "bank2.test"'


async def test_history_without_senders_never_connects(mail):  # noqa: F811
    client, connection = mail
    assert await client.history(date(2026, 11, 1), date(2026, 11, 2), [], lambda h: True) == (0, [])
    assert connection.commands == []


def _message(uid, sender="alertas@bank.test", day=5, trusted=True, message_id=None):
    return MailMessage(uid, message_id or f"m{uid}@bank.test", sender, "", "Compra aprobada",
                       datetime(2026, 11, day, 15, tzinfo=UTC), AuthResult(trusted=trusted, domain="bank.test" if trusted else None),
                       body="Compraste $50.000 en TIENDA")


async def test_history_mail_counts_imports_once_and_keeps_the_live_cursor(service, db, monkeypatch):
    service.settings.mail_accounts = [SimpleNamespace(name="primary")]
    await KnownSenderRepository(db).add("alertas@bank.test")
    await db.commit()
    asked = []
    messages = [_message(1), _message(2, sender="promo@bank.test"), _message(3, trusted=False), _message(4, day=20)]

    class FakeClient:
        def __init__(self, account):
            pass

        async def history(self, since, until, senders, want_body):
            asked.append(senders)
            return 77, messages

    monkeypatch.setattr("app.services.processor.IMAPClient", FakeClient)
    state = await MailStateRepository(db).get("primary")
    state.last_uid, state.uidvalidity = 500, "9"
    await db.commit()

    preview, ids = await service.history_mail(date(2026, 11, 1), date(2026, 11, 10), dry_run=True)
    assert ids == []
    assert {k: v for k, v in preview["counts"].items() if v} == {"found": 3, "untrusted": 1, "auth_failed": 1, "new": 1, "parser": 1}
    assert asked[0] == ["alertas@bank.test", "nequi.com.co"]

    result, ids = await service.history_mail(date(2026, 11, 1), date(2026, 11, 10), dry_run=False)
    assert result["counts"]["new"] == 1 and len(ids) == 1
    item = await service.inbox.get(ids[0])
    assert (item.status, item.source, item.message_id) == ("pending", "email", "m1@bank.test")

    again, ids = await service.history_mail(date(2026, 11, 1), date(2026, 11, 10), dry_run=False)
    assert (again["counts"]["known"], again["counts"]["new"], ids) == (1, 0, [])
    state = await MailStateRepository(db).get("primary")
    assert (state.last_uid, state.uidvalidity) == (500, "9")


async def test_sms_history_never_doubles_a_live_sms(client, monkeypatch):
    from app.api.routers import history

    processed = []

    async def record(ids):
        processed.append(ids)

    monkeypatch.setattr(history, "_process_sms", record)
    stamp = int((datetime.now(UTC) - timedelta(days=3)).timestamp() * 1000)
    live = await client.post(f"{API}/notifications/webhook", headers=PHONE,
                             json={"type": "sms", "sender": "85954", "message": "Pagaste $12.000 en TIENDA", "post_time": stamp})
    assert live.status_code == 202, live.text
    review = auth((await enroll_key(client, ["review"]))["key"])
    batch = [{"sender": "85954", "body": "Pagaste $12.000 en TIENDA", "date": stamp},
             {"sender": "85954", "body": "Recibiste $40.000", "date": stamp + 60000},
             {"sender": "12345", "body": "Promo", "date": stamp}]

    preview = (await client.post(f"{API}/history/sms", headers=review, json={"dry_run": True, "items": batch})).json()
    assert (preview["counts"]["known"], preview["counts"]["new"], preview["counts"]["untrusted"], preview["queued"]) == (1, 1, 1, 0)
    imported = (await client.post(f"{API}/history/sms", headers=review, json={"dry_run": False, "items": batch})).json()
    assert imported["queued"] == 1 and len(processed) == 1
    again = (await client.post(f"{API}/history/sms", headers=review, json={"dry_run": False, "items": batch})).json()
    assert (again["counts"]["known"], again["queued"]) == (2, 0)


async def test_sms_import_has_its_own_hourly_limit(client, monkeypatch):
    from app.api.routers import history

    monkeypatch.setattr(history, "SMS_PREVIEW_MAX_PER_HOUR", 3)
    monkeypatch.setattr(history, "_sms_windows", {})
    review = auth((await enroll_key(client, ["review"]))["key"])
    items = [{"sender": "12345", "body": "x", "date": 1_800_000_000_000}] * 2
    assert (await client.post(f"{API}/history/sms", headers=review, json={"dry_run": True, "items": items})).status_code == 200
    limited = await client.post(f"{API}/history/sms", headers=review, json={"dry_run": True, "items": items})
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) > 0


async def test_mail_history_rejects_future_and_ingest_keys(client):
    review = auth((await enroll_key(client, ["review"]))["key"])
    tomorrow = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
    future = await client.post(f"{API}/history/mail", headers=review, json={"since": "2026-01-01", "until": tomorrow})
    assert future.status_code == 422
    assert (await client.post(f"{API}/history/mail", headers=PHONE, json={"since": "2026-01-01", "until": "2026-01-02"})).status_code == 403
    assert (await client.post(f"{API}/history/sms", headers=PHONE, json={"items": [{"sender": "1", "body": "x", "date": 1}]})).status_code == 403


async def test_owner_manages_sender_addresses_from_the_phone(client):
    review = auth((await enroll_key(client, ["review"]))["key"])
    added = await client.post(f"{API}/senders", headers=review, json={"keyword": "AlertasYNotificaciones@Bancolombia.com.co"})
    assert added.status_code == 200, added.text
    assert added.json()["keyword"] == "alertasynotificaciones@bancolombia.com.co"
    assert (await client.post(f"{API}/senders", headers=review, json={"keyword": "yo@gmail.com"})).status_code == 422
    assert (await client.post(f"{API}/senders", headers=PHONE, json={"keyword": "bank.test"})).status_code == 403
    listed = (await client.get(f"{API}/senders", headers=review)).json()["senders"]
    assert [s["keyword"] for s in listed] == ["alertasynotificaciones@bancolombia.com.co"]
    assert (await client.delete(f"{API}/senders/{listed[0]['id']}", headers=review)).json() == {"deleted": True}


async def test_old_movements_reach_the_inbox_without_alerts(service, monkeypatch):
    announced = []

    async def item_event(session, settings, item, old_status):
        announced.append(item.id)

    monkeypatch.setattr("app.services.processor.alerting.item_event", item_event)
    old, _ = await service._insert("sms:old", source="sms", sender="85954", raw_text="Pagaste $12.000", status="pending",
                                   occurred_at=datetime.now(UTC) - timedelta(days=30))
    fresh, _ = await service._insert("sms:fresh", source="sms", sender="85954", raw_text="Pagaste $12.000", status="pending",
                                     occurred_at=datetime.now(UTC))
    await service.process_ids([old.id, fresh.id])
    assert announced == [fresh.id]
