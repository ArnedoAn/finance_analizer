"""Auto-approval policy endpoints, inbox auto-resolve and e-mail compaction (contract sections 3 and 10)."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api.routers import inbox as inbox_router
from app.clients.imap import MailMessage
from app.core.config import MailAccount
from app.db.models_assistant import Audit
from app.security.sender_trust import AuthResult
from app.services import flags
from app.services import processor as pipeline
from tests.test_devices_v4 import auth, enroll_key

API = "/api/v1"
ADMIN = {"X-API-Key": "test-admin-key-01234567890123456789"}
POLICY = f"{API}/settings/auto-approve"
LONG_MAIL = "\n".join([
    "Estimado cliente: gracias por usar nuestros canales digitales.",
    "Pagaste $12.000 en SUPERMERCADO CENTRAL con tu cuenta Nequi.",
    "Publicidad: abre tu CDT hoy y recibe beneficios exclusivos. " * 10,
    "Aviso legal: " + "informacion confidencial " * 200,
])


async def item(service, source="email", **changes):
    values = dict(
        source=source, source_ref=str(uuid4()), sender="alertas@nequi.com.co",
        raw_text="Pagaste $12.000 en SUPERMERCADO CENTRAL", subject_or_title="Compra",
        occurred_at=datetime.now(UTC), status="pending",
    )
    if source == "notification":
        values.update(app="com.nequi.MobileApp", sender="com.nequi.MobileApp")
    elif source == "sms":
        values.update(sender="85954")
    values.update(changes)
    row = await service.inbox.add(**values)
    await service.session.commit()
    return row


async def set_cap(service, cap, enabled=True):
    await flags.set_policy(service.session, enabled, Decimal(cap))
    await service.session.commit()


@pytest.fixture
def resolver(monkeypatch, service):
    """Runs the background re-evaluation on the test's own session and exposes the router module to await its task."""
    monkeypatch.setattr(inbox_router, "_resolve_task", None)

    async def run(ids):
        await service.reevaluate_ids(ids)

    monkeypatch.setattr(inbox_router, "_run_auto_resolve", run)
    return inbox_router


# --- GET/PUT /settings/auto-approve ---------------------------------------------------------------------------------


async def test_policy_defaults_then_put_round_trip_and_audit(client, db):
    assert (await client.get(POLICY, headers=ADMIN)).json() == {"enabled": True, "max_amount_cop": "2000000.00"}
    saved = await client.put(POLICY, headers=ADMIN, json={"enabled": False, "max_amount_cop": "5000000.00"})
    assert saved.status_code == 200 and saved.json() == {"enabled": False, "max_amount_cop": "5000000.00"}
    assert (await client.get(POLICY, headers=ADMIN)).json() == saved.json()
    number = await client.put(POLICY, headers=ADMIN, json={"enabled": True, "max_amount_cop": 1500000})
    assert number.json() == {"enabled": True, "max_amount_cop": "1500000.00"}
    entries = list((await db.scalars(select(Audit).where(Audit.action == "settings.auto_approve").order_by(Audit.id))).all())
    assert len(entries) == 2 and entries[0].target_type == "settings" and entries[0].actor == "admin"
    assert entries[0].detail == {"enabled": False, "max_amount_cop": "5000000.00", "previous": {"enabled": True, "max_amount_cop": "2000000.00"}}


@pytest.mark.parametrize("body", [
    {}, {"enabled": True}, {"max_amount_cop": "1000"}, {"enabled": "yes", "max_amount_cop": "1000"}, {"enabled": 1, "max_amount_cop": "1000"},
    {"enabled": True, "max_amount_cop": "0"}, {"enabled": True, "max_amount_cop": "-5"}, {"enabled": True, "max_amount_cop": "1000000000.01"},
    {"enabled": True, "max_amount_cop": "abc"}, {"enabled": True, "max_amount_cop": "1000.001"}, {"enabled": True, "max_amount_cop": "1000", "extra": 1},
])
async def test_policy_put_validation_leaves_the_policy_unchanged(client, body):
    response = await client.put(POLICY, headers=ADMIN, json=body)
    assert response.status_code == 422 and response.json()["code"] == "validation_error"
    assert (await client.get(POLICY, headers=ADMIN)).json() == {"enabled": True, "max_amount_cop": "2000000.00"}


@pytest.mark.parametrize("amount", ["0.01", "1000000000", "1000000000.00"])
async def test_policy_put_accepts_the_limits(client, amount):
    response = await client.put(POLICY, headers=ADMIN, json={"enabled": True, "max_amount_cop": amount})
    assert response.status_code == 200 and response.json()["max_amount_cop"] == f"{Decimal(amount):.2f}"


async def test_policy_and_auto_resolve_scopes(client, db):
    reader = auth((await enroll_key(client, ["read"], "reader"))["key"])
    reviewer = auth((await enroll_key(client, ["read", "review"], "reviewer"))["key"])
    ingester = auth((await enroll_key(client, ["ingest"], "ingester"))["key"])
    body = {"enabled": True, "max_amount_cop": "3000000"}
    assert (await client.get(POLICY, headers=reader)).status_code == 200
    assert (await client.get(POLICY, headers=reviewer)).status_code == 200
    assert (await client.get(POLICY, headers=ingester)).status_code == 403
    assert (await client.get(POLICY)).status_code == 401
    assert (await client.put(POLICY, headers=reader, json=body)).status_code == 403
    assert (await client.put(POLICY, headers=ingester, json=body)).status_code == 403
    assert (await client.put(POLICY, json=body)).status_code == 401
    saved = await client.put(POLICY, headers=reviewer, json=body)
    assert saved.status_code == 200 and saved.json() == {"enabled": True, "max_amount_cop": "3000000.00"}
    entry = await db.scalar(select(Audit).where(Audit.action == "settings.auto_approve"))
    assert entry.actor == "reviewer" and entry.device_id is not None
    for headers in (reader, ingester):
        assert (await client.post(f"{API}/inbox/auto-resolve", headers=headers)).status_code == 403
    assert (await client.post(f"{API}/inbox/auto-resolve")).status_code == 401


# --- POST /inbox/auto-resolve ----------------------------------------------------------------------------------------


async def test_auto_resolve_empties_the_review_of_resolvable_items(client, service, clients, extraction, monkeypatch, resolver, db):
    amounts = {"85954": "12001.00", "com.nequi.MobileApp": "12002.00"}
    monkeypatch.setattr(pipeline, "parse", lambda text, subject, sender: replace(extraction, amount=Decimal(amounts.get(sender, "12000.00"))))
    await set_cap(service, "10000")
    waiting = [await service.process(await item(service, source=source)) for source in ("email", "sms", "notification")]
    assert [row.status for row in waiting] == ["needs_review"] * 3
    # Out of reach: mail that failed authentication and the owner's own manual entry.
    unauthenticated = await item(service, status="needs_review", review_reason="sender_auth_failed", raw_text=None)
    manual = await item(
        service, source="manual", sender="owner", status="needs_review", method="manual", review_reason="quick entry over the review threshold",
        amount=Decimal("3000000.00"), currency="COP", merchant="ARRIENDO", asset_account="Nequi", transaction_type="withdrawal",
    )
    assert (await client.put(POLICY, headers=ADMIN, json={"enabled": True, "max_amount_cop": "5000000"})).status_code == 200

    response = await client.post(f"{API}/inbox/auto-resolve", headers=ADMIN)
    assert response.status_code == 202 and response.json() == {"queued": 3}
    await resolver._resolve_task

    assert [(await service.inbox.get(row.id)).status for row in waiting] == ["created"] * 3
    assert clients[0].create_transaction.await_count == 3
    assert (await service.inbox.get(unauthenticated.id)).status == "needs_review"
    assert (await service.inbox.get(manual.id)).status == "needs_review"
    assert (await service.inbox.get(waiting[0].id)).review_reason is None
    audit = await db.scalar(select(Audit).where(Audit.action == "inbox.auto_resolve"))
    assert audit.detail == {"queued": 3}
    # Nothing left to decide: idempotent, no task started.
    again = await client.post(f"{API}/inbox/auto-resolve", headers=ADMIN)
    assert again.status_code == 202 and again.json() == {"queued": 0}


async def test_auto_resolve_does_not_call_the_llm_again_when_the_row_has_its_extraction(client, service, clients, monkeypatch, resolver):
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: None)
    await set_cap(service, "10000")
    row = await service.process(await item(service))
    assert row.method == "llm" and row.status == "needs_review"
    clients[1].analyze.assert_awaited_once()
    await client.put(POLICY, headers=ADMIN, json={"enabled": True, "max_amount_cop": "2000000"})
    assert (await client.post(f"{API}/inbox/auto-resolve", headers=ADMIN)).json() == {"queued": 1}
    await resolver._resolve_task
    row = await service.inbox.get(row.id)
    assert row.status == "created" and row.method == "llm" and row.merchant == "SUPERMERCADO CENTRAL"
    assert clients[1].analyze.await_count == 1
    clients[0].create_transaction.assert_awaited_once()


async def test_auto_resolve_extracts_when_the_row_has_no_extraction(client, service, clients, monkeypatch, resolver):
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: None)
    row = await item(service, status="needs_review", review_reason="Mail body unavailable", method=None)
    assert (await client.post(f"{API}/inbox/auto-resolve", headers=ADMIN)).json() == {"queued": 1}
    await resolver._resolve_task
    clients[1].analyze.assert_awaited_once()
    row = await service.inbox.get(row.id)
    assert row.status == "created" and row.review_reason is None and row.amount == Decimal("12000.00")


@pytest.mark.parametrize("changes", [
    {"kind": "review", "reason": "two transactions"}, {"currency": "USD"}, {"amount": None}, {"amount": Decimal("2000001")}, {"merchant": None},
])
async def test_auto_resolve_keeps_hard_reasons_in_review(client, service, clients, extraction, monkeypatch, resolver, changes):
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: replace(extraction, **changes))
    row = await service.process(await item(service))
    assert row.status == "needs_review"
    assert (await client.post(f"{API}/inbox/auto-resolve", headers=ADMIN)).json() == {"queued": 1}
    await resolver._resolve_task
    row = await service.inbox.get(row.id)
    assert row.status == "needs_review" and row.review_reason
    clients[0].create_transaction.assert_not_awaited()


async def test_auto_resolve_keeps_an_incoherent_date_of_a_stored_llm_extraction(client, service, clients, extraction, monkeypatch, resolver):
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: None)
    clients[1].analyze.return_value = replace(extraction, template_id="llm:test", occurred_at=datetime.now(UTC) - timedelta(days=4))
    row = await service.process(await item(service))
    assert row.status == "needs_review" and pipeline.DATE_FUTURE_REASON in row.review_reason
    assert (await client.post(f"{API}/inbox/auto-resolve", headers=ADMIN)).json() == {"queued": 1}
    await resolver._resolve_task
    row = await service.inbox.get(row.id)
    assert row.status == "needs_review" and pipeline.DATE_FUTURE_REASON in row.review_reason
    assert clients[1].analyze.await_count == 1
    clients[0].create_transaction.assert_not_awaited()


async def test_auto_resolve_keeps_the_manual_duplicate_and_pause_reasons(service, clients, db):
    from app.models.schemas import ManualTransaction

    await service.manual(ManualTransaction(client_ref="overlap", amount=12000, description="Lunch", merchant="SUPERMERCADO CENTRAL", account="Nequi"))
    await set_cap(service, "10000")
    duplicate = await service.process(await item(service))
    assert duplicate.status == "needs_review" and "over auto-approval limit" in duplicate.review_reason
    await set_cap(service, "2000000")
    await service.reevaluate_ids(await service.reevaluation_ids())
    duplicate = await service.inbox.get(duplicate.id)
    assert duplicate.status == "needs_review" and "possible duplicate manual/automatic transaction" in duplicate.review_reason

    paused = await item(
        service, source="sms", status="needs_review", review_reason="x", method="parser", amount=Decimal("5000.00"), currency="COP",
        merchant="TIENDA", asset_account="Nequi", transaction_type="withdrawal", original_kind="withdrawal",
    )
    await flags.mark_suspicious(db, paused)
    await db.commit()
    await service.reevaluate_ids([paused.id])
    assert flags.PAUSED_REASON in (await service.inbox.get(paused.id)).review_reason
    assert clients[0].create_transaction.await_count == 1  # only the manual entry


async def test_auto_resolve_runs_one_at_a_time(client, service, monkeypatch, resolver):
    await set_cap(service, "10000")
    await service.process(await item(service))
    gate = asyncio.Event()

    async def blocked(ids):
        await gate.wait()

    monkeypatch.setattr(inbox_router, "_run_auto_resolve", blocked)
    assert (await client.post(f"{API}/inbox/auto-resolve", headers=ADMIN)).json() == {"queued": 1}
    busy = await client.post(f"{API}/inbox/auto-resolve", headers=ADMIN)
    assert busy.status_code == 409 and busy.json()["code"] == "auto_resolve_running"
    gate.set()
    await inbox_router._resolve_task
    assert (await client.post(f"{API}/inbox/auto-resolve", headers=ADMIN)).status_code == 202
    await inbox_router._resolve_task


async def test_failed_reevaluation_leaves_the_item_in_review_as_it_was(service, clients, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: None)
    clients[1].analyze.side_effect = RuntimeError("provider down")
    row = await item(service, status="needs_review", review_reason="Mail body unavailable", method=None)
    result = await service.process(row, reevaluate=True)
    assert result.status == "needs_review" and result.review_reason == "Mail body unavailable" and result.method is None
    assert result.id not in {due.id for due in await service.inbox.due()}
    clients[0].create_transaction.assert_not_awaited()


# --- E-mail compaction -----------------------------------------------------------------------------------------------


async def test_long_mail_keeps_its_full_text_in_review_and_an_extract_once_created(service, clients):
    await set_cap(service, "2000000", enabled=False)
    held = await service.process(await item(service, raw_text=LONG_MAIL))
    assert held.status == "needs_review" and held.raw_text == LONG_MAIL
    await set_cap(service, "2000000")
    created = await service.review(held, "approve")
    assert created.status == "created"
    assert created.raw_text == "Compra\nPagaste $12.000 en SUPERMERCADO CENTRAL con tu cuenta Nequi."
    assert len(created.raw_text) <= 600


async def test_mail_created_straight_away_is_compacted_and_encrypted_at_rest(service, db):
    from sqlalchemy import text

    row = await service.process(await item(service, raw_text=LONG_MAIL))
    assert row.status == "created" and len(row.raw_text) <= 600 and "Publicidad" not in row.raw_text and "Aviso legal" not in row.raw_text
    stored = (await db.execute(text("SELECT raw_text FROM inbox_items WHERE id = :id"), {"id": row.id})).scalar_one()
    assert stored != row.raw_text and "SUPERMERCADO" not in stored


@pytest.mark.parametrize("source", ["sms", "notification"])
async def test_sms_and_notifications_keep_their_full_text(service, source):
    body = "Pagaste $12.000 en SUPERMERCADO CENTRAL con tu cuenta Nequi. " + "Texto adicional. " * 80
    row = await service.process(await item(service, source=source, raw_text=body))
    assert row.status == "created" and row.raw_text == body


async def test_llm_summary_replaces_the_extract(service, clients, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: None)
    summary = "Compra de $12.000 en SUPERMERCADO CENTRAL con la cuenta 0550012345678"
    clients[1].analyze.return_value = replace(extraction, template_id="llm:test", summary=summary)
    row = await service.process(await item(service, raw_text=LONG_MAIL))
    assert row.method == "llm" and row.status == "created"
    assert row.raw_text.startswith("Compra de $12.000") and "0550012345678" not in row.raw_text and "[CUENTA-5678]" in row.raw_text


async def test_llm_summary_is_not_used_for_phone_sources(service, clients, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: None)
    clients[1].analyze.return_value = replace(extraction, template_id="llm:test", summary="Resumen")
    row = await service.process(await item(service, source="sms"))
    assert row.status == "created" and row.raw_text == "Pagaste $12.000 en SUPERMERCADO CENTRAL"


async def test_rejected_duplicate_ignored_and_voided_mail_are_compacted(service, clients, extraction, monkeypatch):
    await set_cap(service, "2000000", enabled=False)
    held = await service.process(await item(service, raw_text=LONG_MAIL))
    assert held.raw_text == LONG_MAIL
    rejected = await service.review(held, "reject")
    assert rejected.status == "rejected" and len(rejected.raw_text) <= 600 and rejected.raw_text.startswith("Compra")
    await set_cap(service, "2000000")

    await service.process(await item(service, source="sms"))
    duplicate = await service.process(await item(service, raw_text=LONG_MAIL))
    assert duplicate.status == "duplicate" and len(duplicate.raw_text) <= 600

    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: replace(extraction, kind="ignore", amount=None, merchant=None))
    ignored = await service.process(await item(service, raw_text=LONG_MAIL))
    assert ignored.status == "ignored" and len(ignored.raw_text) <= 600 and ignored.raw_text.startswith("Compra")

    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: extraction)
    created = await service.process(await item(service, raw_text=LONG_MAIL, occurred_at=datetime.now(UTC) + timedelta(hours=3)))
    assert created.status == "created"
    extract = created.raw_text
    voided = await service.review(created, "void")
    assert voided.status == "voided" and voided.raw_text == extract


async def test_extract_keeps_the_lines_with_amount_merchant_account_and_date(service):
    body = "\n".join([
        "Hola Maria, gracias por tu compra.", "Valor: $45.000,00", "Comercio: TIENDA X", "Cuenta terminada en 1234",
        "Fecha: 05/11/2026 10:15", "Publicidad: pide domicilio con 20% de descuento.",
    ])
    row = await item(
        service, status="created", method="parser", raw_text=body, subject_or_title="Aviso de compra", amount=Decimal("45000.00"), currency="COP",
        merchant="TIENDA X", asset_account="Nequi", transaction_type="withdrawal", occurred_at=datetime(2026, 11, 5, 15, 0, tzinfo=UTC),
    )
    await service.compact(row)
    assert row.raw_text.split("\n") == [
        "Aviso de compra", "Valor: $45.000,00", "Comercio: TIENDA X", "Cuenta terminada en 1234", "Fecha: 05/11/2026 10:15",
    ]


async def test_poll_mail_compacts_a_duplicate_message_but_not_the_pending_one(service, monkeypatch):
    service.settings.mail_accounts = [
        MailAccount(name=name, provider="gmail", username=name + "@gmail.com", password="test") for name in ("primary", "secondary")
    ]
    message = MailMessage(
        uid=1, message_id="bank-message@nequi.com.co", from_address="alertas@nequi.com.co", subject="Compra",
        occurred_at=datetime.now(UTC), body=LONG_MAIL, auth=AuthResult(dkim="pass", domain="nequi.com.co", trusted=True),
    )
    imap = AsyncMock()
    imap.poll.return_value = (42, 1, [message])
    monkeypatch.setattr(pipeline, "IMAPClient", lambda account: imap)
    await service.poll_mail()
    first = await service.inbox.by_ref("mail:primary:42:1")
    second = await service.inbox.by_ref("mail:secondary:42:1")
    assert first.status == "pending" and first.raw_text == LONG_MAIL[:4000]
    assert second.status == "duplicate" and len(second.raw_text) <= 600 and second.raw_text.startswith("Compra")
