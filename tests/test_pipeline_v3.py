"""V3 durability, deduplication and review regressions (no external network)."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.config import OwnAccount
from app.core.exceptions import PermanentError
from app.db.models import InboxItem
from app.models.schemas import ManualTransaction, WebhookPayload
from app.services import processor as pipeline


async def item(service, source="email", ref=None, **changes):
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


async def test_external_id_lookup_precedes_first_create(service, clients):
    row = await service.process(await item(service, ref="mail:first"))
    assert row.status == "created"
    clients[0].find_by_external_id.assert_awaited_once_with("fa:mail:first")
    split = clients[0].create_transaction.call_args.args[0]["transactions"][0]
    assert split["external_id"] == "fa:mail:first"
    assert split["source_id"] == "1" and split["destination_id"] == "2"
    assert "auto" in split["tags"]
    await service.process(row)
    clients[0].create_transaction.assert_awaited_once()


async def test_timeout_after_create_adopts_without_second_post(service, clients):
    firefly = clients[0]
    remote = {}

    async def lookup(external_id):
        return remote.get(external_id)

    async def committed_then_timeout(payload):
        # Observe the committed submission state from another DB session.
        from sqlalchemy.ext.asyncio import AsyncSession
        async with AsyncSession(service.session.bind) as observer:
            durable = await observer.scalar(select(InboxItem).where(InboxItem.source_ref == "timeout"))
            assert durable.status == "submitting"
        remote[payload["transactions"][0]["external_id"]] = "remote-42"
        raise TimeoutError("response lost after Firefly committed")

    firefly.find_by_external_id.side_effect = lookup
    firefly.create_transaction.side_effect = committed_then_timeout
    row = await service.process(await item(service, ref="timeout"))
    assert row.status in {"pending", "submitting"}
    row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    row.claim_until = None
    await service.session.commit()
    row = await service.process(row)
    assert row.status == "created" and row.firefly_transaction_id == "remote-42"
    assert firefly.find_by_external_id.await_count == 2
    firefly.create_transaction.assert_awaited_once()


async def test_recover_submitting_adopts_existing_transaction(service, clients):
    clients[0].find_by_external_id.return_value = "existing-7"
    row = await service.process(await item(service, status="submitting"))
    assert row.status == "created" and row.firefly_transaction_id == "existing-7"
    clients[0].create_transaction.assert_not_awaited()


@pytest.mark.parametrize("sources", [("notification", "sms"), ("email", "notification"), ("sms", "email")])
async def test_distinct_channels_deduplicate(service, clients, sources):
    first = await service.process(await item(service, source=sources[0]))
    second = await service.process(await item(service, source=sources[1]))
    assert first.status == "created" and second.status == "duplicate"
    assert second.duplicate_of_id == first.id
    clients[0].create_transaction.assert_awaited_once()


@pytest.mark.parametrize("source", ["email", "notification", "sms"])
async def test_same_source_never_fingerprint_deduplicates(service, clients, source):
    first = await service.process(await item(service, source=source))
    second = await service.process(await item(service, source=source))
    assert first.status == second.status == "created"
    assert clients[0].create_transaction.await_count == 2


async def test_cross_channel_window_is_bounded(service):
    await service.process(await item(service, occurred_at=datetime.now(UTC) - timedelta(hours=2)))
    assert (await service.process(await item(service, source="sms"))).status == "created"


async def test_timestamp_free_phone_events_have_unique_references(service):
    payload = WebhookPayload(app="com.nequi.MobileApp", message="Pagaste $12.000")
    first, accepted = await service.ingest_notification(payload)
    second, accepted_again = await service.ingest_notification(payload)
    assert accepted and accepted_again
    assert first.id != second.id and first.source_ref != second.source_ref


async def test_event_id_survives_payload_and_timestamp_changes(service):
    event_id = uuid4()
    first, accepted = await service.ingest_notification(WebhookPayload(
        event_id=event_id, app="com.nequi.MobileApp", message="Pagaste $12.000", post_time=int(datetime.now(UTC).timestamp() * 1000),
    ))
    second, accepted_again = await service.ingest_notification(WebhookPayload(
        event_id=event_id, app="com.nequi.MobileApp", message="Updated text", post_time=1,
    ))
    assert accepted and not accepted_again
    assert first.id == second.id and first.source_ref == f"evt:{event_id}"


@pytest.mark.parametrize("post_time", [1, 4102444800000])
async def test_unreasonable_phone_timestamp_requires_review(service, post_time):
    before = datetime.now(UTC)
    row, _ = await service.ingest_notification(WebhookPayload(
        event_id=uuid4(), app="com.nequi.MobileApp", message="Pagaste $12.000", post_time=post_time,
    ))
    row = await service.process(row)
    assert row.status == "needs_review" and row.review_reason
    assert before - timedelta(seconds=1) <= row.occurred_at <= datetime.now(UTC)


async def test_raw_text_is_retained_even_for_unknown_sources(service):
    row, accepted = await service.ingest_notification(WebhookPayload(app="unknown.app", message="private" * 1000))
    assert not accepted and row.status == "ignored"
    assert row.raw_text == ("private" * 1000)[:4000]


async def test_outage_retries_without_attempt_limit_then_fails_after_72_hours(service, clients):
    clients[0].create_transaction.side_effect = OSError("offline")
    row = await item(service, attempts=100, created_at=datetime.now(UTC) - timedelta(hours=71))
    row = await service.process(row)
    assert row.status in {"pending", "submitting"} and row.next_attempt_at
    assert row.attempts > 100
    row.created_at = datetime.now(UTC) - timedelta(hours=73)
    row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    row.claim_until = None
    await service.session.commit()
    row = await service.process(row)
    assert row.status == "failed" and row.next_attempt_at is None


async def test_permanent_firefly_error_needs_review_without_retry(service, clients):
    clients[0].create_transaction.side_effect = PermanentError("invalid account")
    row = await service.process(await item(service))
    assert row.status == "needs_review" and row.review_reason
    assert row.next_attempt_at is None


@pytest.mark.parametrize("changes", [
    {"kind": "review", "reason": "two transactions"},
    {"merchant": None}, {"currency": "USD"}, {"amount": Decimal("2000001")},
    {"occurred_at": datetime.now(UTC) - timedelta(days=4)},
    {"occurred_at": datetime.now(UTC) + timedelta(days=1)},
])
async def test_uncertain_extractions_never_create_silently(service, clients, extraction, monkeypatch, changes):
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: replace(extraction, **changes))
    row = await service.process(await item(service))
    assert row.status == "needs_review" and row.review_reason
    clients[0].create_transaction.assert_not_awaited()


async def test_unknown_source_account_needs_review(service, clients):
    row = await service.process(await item(service, sender="alerts@unmapped.bank"))
    assert row.status == "needs_review" and row.review_reason
    clients[0].create_transaction.assert_not_awaited()


async def test_llm_contract_and_verification(service, clients, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline, "verify_llm", lambda *a: ["amount not present"])
    row = await service.process(await item(service), cycle_id="cycle-42")
    assert row.status == "needs_review" and row.review_reason
    args = clients[1].analyze.call_args
    assert args.args[0] and args.kwargs == {"cycle_id": "cycle-42"}
    clients[0].create_transaction.assert_not_awaited()


async def test_manual_client_ref_is_idempotent(service, clients):
    payload = ManualTransaction(client_ref="phone-manual-1", amount=12000, description="Lunch", merchant="SUPERMERCADO CENTRAL", account="Nequi")
    first = await service.manual(payload)
    second = await service.manual(payload)
    assert first.id == second.id and first.status == "created"
    clients[0].create_transaction.assert_awaited_once()


async def test_manual_auto_collision_is_review_not_silent_duplicate(service, clients):
    await service.manual(ManualTransaction(client_ref="manual-overlap", amount=12000, description="Lunch", merchant="SUPERMERCADO CENTRAL", account="Nequi"))
    row = await service.process(await item(service))
    assert row.status == "needs_review" and row.review_reason
    clients[0].create_transaction.assert_awaited_once()


async def test_manual_failure_never_enters_automatic_retry(service, clients):
    clients[0].create_transaction.side_effect = OSError("offline")
    row = await service.manual(ManualTransaction(client_ref="failed-manual", amount=12000, description="Lunch", account="Nequi"))
    assert row.status == "failed" and row.next_attempt_at is None
    assert row.id not in {due.id for due in await service.inbox.due()}


async def test_transfer_pairs_opposite_leg_without_second_create(service, clients, extraction, monkeypatch):
    service.settings.own_accounts.append(OwnAccount(
        firefly_account="Bancolombia", bank="bancolombia", kind="savings", last4=["5678"],
        aliases=["bancolombia"], sources=["sms:87400"],
    ))
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: replace(extraction, kind="transfer", counterparty_hint="5678"))
    outgoing = await service.process(await item(service))
    assert outgoing.status == "created" and outgoing.transaction_type == "transfer"
    split = clients[0].create_transaction.call_args.args[0]["transactions"][0]
    assert split["source_id"] == "1" and split["destination_id"] == "4"
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: replace(extraction, kind="deposit", account_hint="5678", counterparty_hint="1234"))
    incoming = await service.process(await item(service, source="sms", sender="87400"))
    assert incoming.status == "duplicate" and incoming.duplicate_of_id == outgoing.id
    clients[0].create_transaction.assert_awaited_once()


async def test_reconciliation_deleted_transaction_no_longer_deduplicates(service, clients):
    original = await service.process(await item(service))
    clients[0].find_by_external_id.return_value = None
    await service.reconcile()
    assert (await service.inbox.get(original.id)).status == "voided"
    replacement = await service.process(await item(service, source="sms"))
    assert replacement.status == "created"
    assert clients[0].create_transaction.await_count == 2


async def test_email_message_id_deduplicates_across_imap_accounts(service, monkeypatch):
    from app.clients.imap import MailMessage
    from app.core.config import MailAccount
    from app.security.sender_trust import AuthResult

    service.settings.mail_accounts = [
        MailAccount(name=name, provider="gmail", username=name + "@gmail.com", password="test")
        for name in ("primary", "secondary")
    ]
    message = MailMessage(
        uid=1, message_id="bank-message@nequi.com.co", from_address="alertas@nequi.com.co",
        subject="Compra", occurred_at=datetime.now(UTC), body="Pagaste $12.000",
        auth=AuthResult(dkim="pass", domain="nequi.com.co", trusted=True),
    )
    imap = AsyncMock()
    imap.poll.return_value = (42, 1, [message])
    monkeypatch.setattr(pipeline, "IMAPClient", lambda account: imap)
    result = await service.poll_mail()
    assert result["queued"] == 1 and result["duplicates"] == 1 and not result["errors"]
    first = await service.inbox.by_ref("mail:primary:42:1")
    second = await service.inbox.by_ref("mail:secondary:42:1")
    assert first.status == "pending" and second.status == "duplicate"
    assert second.duplicate_of_id == first.id
    assert first.message_id == second.message_id == message.message_id
    for name in ("primary", "secondary"):
        state = await service.mail_states.get(name)
        assert state.last_uid == 1 and str(state.uidvalidity) == "42"


async def test_notification_throttle_is_persistent_and_key_scoped(db, settings, monkeypatch):
    import httpx
    from pydantic import SecretStr

    from app.db.models import Notification
    from app.services.notifications import Notifier

    settings.notify_url = "https://notify.invalid/notify"
    settings.notify_api_key = SecretStr("notify-test-key")
    post = AsyncMock(return_value=httpx.Response(200, request=httpx.Request("POST", settings.notify_url)))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert await Notifier(db, settings).send("failed:1", "x" * 5000, silent=True)
    # A new instance must honor the DB throttle, not an in-memory cache.
    assert not await Notifier(db, settings).send("failed:1", "again")
    assert await Notifier(db, settings).send("failed:2", "separate alert")
    assert post.await_count == 2
    payload = post.call_args_list[0].kwargs
    assert payload["headers"] == {"x-api-key": "notify-test-key"}
    assert payload["json"] == {"message": "x" * 4000, "silent": True}
    record = await db.get(Notification, "failed:1")
    record.last_sent_at = datetime.now(UTC) - timedelta(hours=6, seconds=1)
    await db.commit()
    assert await Notifier(db, settings).send("failed:1", "after six hours")


async def test_batch_notification_uses_fifteen_minute_throttle(service, monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(service.notifier, "send", send)
    await service.notify_batch({"created": 1, "needs_review": 2})
    assert send.call_args.args[0] == "batch"
    assert send.call_args.args[2] == 15


async def test_failed_outage_sends_immediate_alert(service, clients, monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(service.notifier, "send", send)
    clients[0].create_transaction.side_effect = OSError("offline")
    row = await service.process(await item(service, created_at=datetime.now(UTC) - timedelta(hours=73)))
    assert row.status == "failed"
    assert send.call_args.args[0] == f"failed:{row.id}"


async def test_ambiguous_transfer_requires_review(service, clients, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: replace(extraction, kind="transfer", counterparty_hint="unknown bank"))
    row = await service.process(await item(service))
    assert row.status == "needs_review" and row.review_reason
    clients[0].create_transaction.assert_not_awaited()


async def test_committed_claim_prevents_second_worker_submission(service, clients):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.repositories import InboxRepository

    row = await item(service)
    assert await service.inbox.claim(row.id) is not None
    async with AsyncSession(service.session.bind, expire_on_commit=False) as second:
        assert await InboxRepository(second).claim(row.id) is None
    clients[0].create_transaction.assert_not_awaited()


async def test_postgres_claim_skips_row_locked_by_another_worker(service):
    from sqlalchemy.ext.asyncio import AsyncSession

    if service.session.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row-lock semantics require TEST_DATABASE_URL")
    row = await item(service)
    async with AsyncSession(service.session.bind, expire_on_commit=False) as worker:
        async with worker.begin():
            await worker.scalar(select(InboxItem).where(InboxItem.id == row.id).with_for_update())
            assert await service.inbox.claim(row.id) is None
    assert await service.inbox.claim(row.id) is not None


async def test_configured_asset_missing_in_firefly_requires_review(service, clients):
    clients[0].accounts.return_value = [a for a in clients[0].accounts.return_value if a["name"] != "Nequi"]
    row = await service.process(await item(service))
    assert row.status == "needs_review" and row.review_reason
    clients[0].create_transaction.assert_not_awaited()
    clients[0].create_account.assert_not_awaited()


async def test_manual_default_merchant_is_fixed_not_free_text(service, clients):
    row = await service.manual(ManualTransaction(
        client_ref="manual-default-merchant", amount=12000,
        description="Lunch with a different description each time", account="Nequi",
    ))
    assert row.status == "created" and row.merchant == "GASTO MANUAL"
    clients[0].create_account.assert_awaited_once_with("GASTO MANUAL", "expense")


@pytest.mark.parametrize("source", ["email", "notification", "sms"])
async def test_llm_extractions_require_review_by_default(service, clients, monkeypatch, source):
    monkeypatch.setattr(pipeline, "parse", lambda *args: None)
    assert service.settings.llm_auto_approve_max_cop == 0
    row = await service.process(await item(service, source=source))
    assert row.method == "llm" and row.status == "needs_review" and row.review_reason
    clients[1].analyze.assert_awaited_once()
    clients[0].create_transaction.assert_not_awaited()


@pytest.mark.parametrize("threshold,expected", [(0, "needs_review"), (11999, "needs_review"), (12000, "created"), (12001, "created")])
@pytest.mark.parametrize("source", ["email", "notification", "sms"])
async def test_explicit_llm_threshold_bounds_verified_extractions(service, clients, monkeypatch, source, threshold, expected):
    monkeypatch.setattr(pipeline, "parse", lambda *args: None)
    service.settings.llm_auto_approve_max_cop = Decimal(threshold)
    row = await service.process(await item(service, source=source))
    assert row.method == "llm" and row.status == expected
    assert clients[0].create_transaction.await_count == (expected == "created")


async def test_llm_threshold_never_overrides_verification(service, clients, monkeypatch):
    service.settings.llm_auto_approve_max_cop = Decimal("1000000")
    monkeypatch.setattr(pipeline, "parse", lambda *args: None)
    monkeypatch.setattr(pipeline, "verify_llm", lambda *args: ["merchant absent"])
    row = await service.process(await item(service))
    assert row.status == "needs_review" and "merchant absent" in row.review_reason
    clients[0].create_transaction.assert_not_awaited()


@pytest.mark.parametrize("amount,expected", [("2000000", "created"), ("2000001", "needs_review")])
async def test_sms_parser_default_two_million_limit(service, clients, extraction, monkeypatch, amount, expected):
    assert service.settings.sms_auto_approve_max_cop == Decimal("2000000")
    monkeypatch.setattr(pipeline, "parse", lambda *args: replace(extraction, amount=Decimal(amount)))
    row = await service.process(await item(service, source="sms"))
    assert row.method == "parser" and row.status == expected
    assert clients[0].create_transaction.await_count == (expected == "created")


@pytest.mark.parametrize("source,expected", [("sms", "needs_review"), ("email", "created"), ("notification", "created")])
async def test_lower_sms_parser_threshold_is_source_scoped(service, clients, source, expected):
    service.settings.sms_auto_approve_max_cop = Decimal("10000")
    row = await service.process(await item(service, source=source))
    assert row.status == expected
    assert clients[0].create_transaction.await_count == (expected == "created")


async def test_sms_llm_never_uses_permissive_sms_parser_threshold(service, clients, monkeypatch):
    service.settings.sms_auto_approve_max_cop = Decimal("2000000")
    service.settings.llm_auto_approve_max_cop = Decimal("10000")
    monkeypatch.setattr(pipeline, "parse", lambda *args: None)
    row = await service.process(await item(service, source="sms"))
    assert row.method == "llm" and row.status == "needs_review"
    clients[0].create_transaction.assert_not_awaited()


@pytest.mark.parametrize("source", ["sms", "notification"])
async def test_sensitive_phone_code_is_not_retained_or_sent_to_llm(service, clients, source, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", lambda *args: None)
    payload = WebhookPayload(type=source, app="com.nequi.MobileApp", sender="85954",
        title="Código de seguridad 123456", message="Tu código de seguridad es 123456")
    row, accepted = await service.ingest_notification(payload)
    assert not accepted and row.status == "ignored"
    assert row.raw_text is None and row.subject_or_title == ""
    await service.process(row)
    clients[1].analyze.assert_not_awaited()
    clients[0].create_transaction.assert_not_awaited()


@pytest.mark.parametrize("sensitive", [False, True])
async def test_mail_auth_failure_and_sensitive_code_privacy(service, clients, monkeypatch, sensitive):
    from app.clients.imap import MailMessage
    from app.core.config import MailAccount
    from app.security.sender_trust import AuthResult

    service.settings.mail_accounts = [MailAccount(name="primary", provider="gmail", username="user@gmail.com", password="secret")]
    message = MailMessage(uid=1, message_id="security@nequi.com.co", from_address="alertas@nequi.com.co",
        subject="Código de seguridad 123456" if sensitive else "Compra",
        occurred_at=datetime.now(UTC), body="Tu código de seguridad es 123456" if sensitive else None,
        auth=AuthResult(dkim="pass" if sensitive else "fail", domain="nequi.com.co", trusted=sensitive))
    imap = AsyncMock()
    imap.poll.return_value = (42, 1, [message])
    monkeypatch.setattr(pipeline, "IMAPClient", lambda account: imap)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(service.notifier, "send", send)
    await service.poll_mail()
    row = await service.inbox.by_ref("mail:primary:42:1")
    assert row.raw_text is None
    if sensitive:
        assert row.status == "ignored" and row.subject_or_title == ""
    else:
        assert row.status == "needs_review" and row.review_reason
        send.assert_awaited()
        assert "123456" not in str(send.call_args)
    clients[1].analyze.assert_not_awaited()
    clients[0].create_transaction.assert_not_awaited()


async def test_sensitive_pending_item_is_scrubbed_before_extraction(service, clients, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", lambda *args: None)
    row = await item(service, source="sms", raw_text="Tu código de seguridad es 123456",
        subject_or_title="Código de seguridad 123456")
    row = await service.process(row)
    assert row.status == "ignored"
    assert row.raw_text is None and row.subject_or_title == ""
    clients[1].analyze.assert_not_awaited()
    clients[0].create_transaction.assert_not_awaited()
