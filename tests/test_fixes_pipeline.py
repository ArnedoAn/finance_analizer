"""Regression tests for the security/correctness review fixes in the ingest pipeline (A1, M1, M3, B6, B8, CR)."""
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.api.routers import ingest
from app.core.config import Settings
from app.core.exceptions import PermanentError
from app.core.timezone import now_utc
from app.db.models import InboxItem
from app.db.models_alerts import Alert
from app.models.schemas import ManualTransaction, WebhookPayload
from app.services import flags, suggestions
from app.services import processor as pipeline

API = "/api/v1"
ADMIN = {"X-API-Key": "test-admin-key-01234567890123456789"}
PHONE = {"X-API-Key": "test-phone-key-01234567890123456789"}
STATUSES = ["pending", "submitting", "created", "needs_review", "failed", "voided", "ignored", "rejected", "duplicate"]


async def item(service, source="notification", ref=None, **changes):
    values = dict(
        source=source, source_ref=ref or str(uuid4()), sender="alertas@nequi.com.co",
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


async def alerts(db, type):
    return list((await db.scalars(select(Alert).where(Alert.type == type).order_by(Alert.id))).all())


def webhook_body(**changes):
    body = {"event_id": str(uuid4()), "type": "notification", "app": "com.nequi.MobileApp", "message": "Pagaste $12.000 en SUPERMERCADO CENTRAL"}
    body.update(changes)
    return body


# --- A1: the auto-approval policy replaced the per-source environment caps -----------------------------

def test_ingest_limit_default_and_the_removed_caps():
    assert Settings.model_fields["ingest_max_events_per_hour"].default == 120
    assert not [name for name in Settings.model_fields if name.endswith("auto_approve_max_cop")]


def test_an_environment_that_still_sets_the_removed_caps_starts_and_ignores_them(monkeypatch):
    for name in ("LLM", "SMS", "NOTIFICATION"):
        monkeypatch.setenv(f"{name}_AUTO_APPROVE_MAX_COP", "5000000")
    config = Settings(finance_api_key="test-admin-key-01234567890123456789", _env_file=None)
    assert config.review_amount_cop == Decimal("2000000")
    assert not [name for name in vars(config) if name.endswith("auto_approve_max_cop")]


async def test_notification_with_known_template_registers_by_default(service, clients):
    row = await service.process(await item(service))
    assert row.method == "parser" and row.status == "created"
    clients[0].create_transaction.assert_awaited_once()


@pytest.mark.parametrize(("cap", "expected"), [("11999", "needs_review"), ("12000", "created")])
async def test_notification_cap_bounds(service, clients, cap, expected):
    await flags.set_policy(service.session, True, Decimal(cap))
    await service.session.commit()
    row = await service.process(await item(service))
    assert row.status == expected
    assert clients[0].create_transaction.await_count == (expected == "created")


async def test_leaked_ingest_key_cannot_create_a_charge_above_the_cap_unreviewed(client, clients, service):
    await flags.set_policy(service.session, True, Decimal("10000"))
    await service.session.commit()
    response = await client.post(f"{API}/notifications/webhook", headers=PHONE, json=webhook_body(message="Compraste $1.900.000 en SUPERMERCADO CENTRAL"))
    assert response.status_code == 202
    row = await service.inbox.get(response.json()["inbox_id"])
    assert row.status == "needs_review" and "amount over auto-approval limit" in row.review_reason
    clients[0].create_transaction.assert_not_awaited()


# --- A1: per-device event limit ---------------------------------------------------------------------

async def test_webhook_over_the_hourly_limit_is_429_with_retry_after_and_a_single_alert(client, service, db, monkeypatch):
    service.settings.ingest_max_events_per_hour = 3
    now = [1000.0]
    monkeypatch.setattr(ingest, "_clock", lambda: now[0])
    for _ in range(3):
        assert (await client.post(f"{API}/notifications/webhook", headers=PHONE, json=webhook_body())).status_code == 202
    now[0] = 1010.0
    limited = await client.post(f"{API}/notifications/webhook", headers=PHONE, json=webhook_body())
    assert limited.status_code == 429 and limited.json()["code"] == "rate_limited"
    assert limited.headers["Retry-After"] == "3590"
    assert (await client.post(f"{API}/notifications/webhook", headers=PHONE, json=webhook_body())).status_code == 429
    assert len((await db.scalars(select(InboxItem))).all()) == 3  # rejected events are not stored
    alert, = await alerts(db, "system_failure")
    assert alert.dedupe_key.startswith("ingest-flood:") and "phone-legacy" in alert.body and "3 eventos" in alert.body


async def test_webhook_limit_is_per_device_and_slides_with_the_window(client, service, monkeypatch):
    service.settings.ingest_max_events_per_hour = 2
    now = [1000.0]
    monkeypatch.setattr(ingest, "_clock", lambda: now[0])
    for _ in range(2):
        assert (await client.post(f"{API}/notifications/webhook", headers=PHONE, json=webhook_body())).status_code == 202
    assert (await client.post(f"{API}/notifications/webhook", headers=PHONE, json=webhook_body())).status_code == 429
    # another principal has its own bucket
    assert (await client.post(f"{API}/notifications/webhook", headers=ADMIN, json=webhook_body())).status_code == 202
    now[0] = 1000.0 + 3600
    assert (await client.post(f"{API}/notifications/webhook", headers=PHONE, json=webhook_body())).status_code == 202


# --- B6: webhook limits and unknown sources ---------------------------------------------------------

@pytest.mark.parametrize(("field", "limit"), [("app", 255), ("sender", 512), ("title", 500), ("message", 20000), ("text", 20000), ("big_text", 20000)])
def test_webhook_payload_field_limits(field, limit):
    base = {"message": "Pagaste $12.000"}
    WebhookPayload(**{**base, field: "x" * limit})
    with pytest.raises(ValidationError):
        WebhookPayload(**{**base, field: "x" * (limit + 1)})


async def test_webhook_oversized_field_is_422_not_a_database_error(client, db):
    response = await client.post(f"{API}/notifications/webhook", headers=PHONE, json=webhook_body(app="a" * 256))
    assert response.status_code == 422 and response.json()["code"] == "validation_error"
    assert (await db.scalars(select(InboxItem))).all() == []


# --- M1: the LLM never sees the unredacted text ------------------------------------------------------

async def test_llm_receives_redacted_text_but_verification_uses_the_original(service, clients, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: None)
    seen = []
    monkeypatch.setattr(pipeline, "verify_llm", lambda text, extraction: seen.append(text) or [])
    raw = "Pagaste $12.000 en SUPERMERCADO CENTRAL con la cuenta 0550012345678"
    await service.process(await item(service, source="sms", raw_text=raw, subject_or_title="Compra cuenta 0550012345678"))
    sent = clients[1].analyze.call_args.args[0]
    assert "0550012345678" not in sent and "[CUENTA-5678]" in sent and "$12.000" in sent
    assert seen == [raw]


# --- M3: retention and stored reasons ---------------------------------------------------------------

@pytest.mark.parametrize("status", STATUSES)
async def test_purge_clears_the_title_after_retention_in_every_status(db, service, status):
    for label, age in (("old", 31), ("fresh", 29)):
        db.add(InboxItem(
            source="email", source_ref=f"{status}-{label}", sender="alertas@nequi.com.co", status=status,
            subject_or_title="Extracto ****1234 - JUAN PEREZ", raw_text="texto", created_at=now_utc() - timedelta(days=age),
        ))
    await db.commit()
    await service._purge()
    kept = {ref: (title, raw) for ref, title, raw in (await db.execute(select(InboxItem.source_ref, InboxItem.subject_or_title, InboxItem.raw_text))).all()}
    assert kept[f"{status}-old"] == ("", None)
    assert kept[f"{status}-fresh"] == ("Extracto ****1234 - JUAN PEREZ", "texto")


@pytest.mark.parametrize("purged", [False, True])
async def test_editing_after_retention_keeps_the_firefly_description(service, clients, purged):
    row = await service.process(await item(service))
    assert row.status == "created"
    if purged:
        row.created_at = now_utc() - timedelta(days=31)
        await service.session.commit()
        await service._purge()
        await service.session.refresh(row)
        assert row.subject_or_title == ""
    await service.review(row, "edit", {"category": "Transporte"})
    sent = clients[0].update_transaction.call_args.args[1]["transactions"][0]
    if purged:
        assert "description" not in sent  # not overwritten with the merchant
    else:
        assert sent["description"] == "Compra"


async def test_llm_review_reason_is_redacted_and_trimmed(service, clients, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: None)
    clients[1].analyze.return_value = replace(extraction, kind="review", reason="Cuenta 0550012345678 " + "x" * 500)
    row = await service.process(await item(service, source="email", sender="alertas@nequi.com.co"))
    assert row.status == "needs_review"
    reason = row.review_reason.split("; ")[0]
    assert "0550012345678" not in row.review_reason and "[CUENTA-5678]" in reason and len(reason) <= 200


async def test_llm_ignore_reason_is_redacted_and_trimmed(service, clients, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: None)
    clients[1].analyze.return_value = replace(extraction, kind="ignore", reason="Cuenta 0550012345678 " + "y" * 500)
    row = await service.process(await item(service, source="email", sender="alertas@nequi.com.co"))
    assert row.status == "ignored"
    assert "0550012345678" not in row.review_reason and len(row.review_reason) <= 200


async def test_permanent_firefly_error_never_echoes_the_response_body(service, clients):
    clients[0].create_transaction.side_effect = PermanentError("Firefly 422: {'errors': {'description': ['Cédula 1234567890 de JUAN PEREZ']}}")
    row = await service.process(await item(service))
    assert row.status == "needs_review"
    assert row.last_error == row.review_reason == "Firefly rejected the request (422)"


@pytest.mark.parametrize(("message", "expected"), [("Merchant is not clean", "Merchant is not clean"), ("x" * 500, "x" * 200)])
async def test_permanent_messages_from_the_processor_stay_readable_and_bounded(service, clients, message, expected):
    clients[0].create_transaction.side_effect = PermanentError(message)
    row = await service.process(await item(service))
    assert row.last_error == row.review_reason == expected


# --- B8(a): the pause holds items that were already extracted ----------------------------------------

def extracted(**changes):
    values = dict(
        method="parser", amount=Decimal("12000.00"), currency="COP", merchant="SUPERMERCADO CENTRAL", asset_account="Nequi",
        transaction_type="withdrawal", original_kind="withdrawal", next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    values.update(changes)
    return values


async def test_pause_holds_extracted_items_that_retry_after_an_outage(service, clients, db):
    row = await item(service, **extracted())
    await flags.mark_suspicious(db, await item(service, status="rejected"))
    await db.commit()
    result = await service.process(row)
    assert result.status == "needs_review" and flags.PAUSED_REASON in result.review_reason
    clients[0].create_transaction.assert_not_awaited()


async def test_pause_does_not_block_explicit_approval_or_manual_entries(service, clients, db):
    await flags.mark_suspicious(db, await item(service, status="rejected"))
    await db.commit()
    approved = await service.process(await item(service, status="needs_review", **extracted()), approved=True)
    assert approved.status == "created"
    manual = await service.manual(ManualTransaction(client_ref="paused-manual", amount=7000, description="Lunch", merchant="SUPERMERCADO CENTRAL", account="Nequi"))
    assert manual.status == "created"


# --- CR: sync, report, new_charge, 502 -----------------------------------------------------------------

async def test_sync_emits_the_missing_account_alert_without_committing(service, db, monkeypatch, add_own_account):
    await add_own_account("Fantasma")
    commits = []
    real_commit = db.commit

    async def spy():
        commits.append(1)
        await real_commit()

    monkeypatch.setattr(db, "commit", spy)
    await service.sync(force=True)
    assert commits == []
    alert, = await alerts(db, "system_failure")
    assert "Fantasma" in alert.body


async def test_report_serializes_money_with_two_decimals(service):
    empty = await service.report("day")
    assert set(empty["totals"].values()) == {"0.00"}
    assert (await service.process(await item(service))).status == "created"
    report = await service.report("day")
    assert report["totals"]["withdrawal"] == "12000.00" and report["totals"]["deposit"] == "0.00"
    values = [*report["totals"].values(), *report["by_category"].values(), *report["by_account"].values(), *(m["amount"] for m in report["top_merchants"])]
    assert values and all(re.fullmatch(r"\d+\.\d{2}", value) for value in values)


async def test_automatic_created_item_emits_new_charge_with_suggested_categories(service, db):
    db.add(InboxItem(
        source="manual", source_ref="history", sender="owner", status="created", method="manual", merchant="SUPERMERCADO CENTRAL",
        category="Alimentación", amount=Decimal("5000.00"), currency="COP", asset_account="Nequi", transaction_type="withdrawal",
        occurred_at=now_utc() - timedelta(days=3),
    ))
    await db.commit()
    row = await service.process(await item(service))
    assert row.status == "created"
    charge, = await alerts(db, "new_charge")
    assert charge.item_id == row.id and charge.amount == Decimal("12000.00") and charge.suggested_categories == ["Alimentación"]
    assert "$12.000 COP" in charge.body and "SUPERMERCADO CENTRAL" in charge.body
    await service._new_charge(row, "pending", False)
    assert len(await alerts(db, "new_charge")) == 1


async def test_no_new_charge_for_manual_entries_or_owner_approvals(service, db):
    manual = await service.manual(ManualTransaction(client_ref="m-1", amount=7000, description="Lunch", merchant="SUPERMERCADO CENTRAL", account="Nequi"))
    assert manual.status == "created"
    await flags.set_policy(db, False, Decimal("2000000"))
    await db.commit()
    held = await service.process(await item(service))
    assert held.status == "needs_review"
    assert (await service.review(held, "approve")).status == "created"
    assert await alerts(db, "new_charge") == []


async def test_new_charge_failure_never_fails_the_operation(service, db, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(suggestions, "suggest", broken)
    row = await service.process(await item(service))
    assert row.status == "created"
    assert await alerts(db, "new_charge") == []


async def test_manual_502_has_detail_code_inbox_id_and_status(client, clients, service):
    clients[0].create_transaction.side_effect = OSError("offline")
    response = await client.post(f"{API}/transactions/manual", headers=ADMIN, json={
        "client_ref": "m-502", "amount": "12000", "description": "Almuerzo", "merchant": "SUPERMERCADO CENTRAL", "account": "Nequi",
    })
    assert response.status_code == 502
    body = response.json()
    assert set(body) == {"detail", "code", "inbox_id", "status"}
    assert body["code"] == "upstream_unavailable" and body["status"] == "failed" and "offline" not in str(body)
    assert (await service.inbox.get(body["inbox_id"])).status == "failed"
