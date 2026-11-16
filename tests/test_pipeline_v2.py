"""Service contracts, using the shared in-memory AsyncSession fixture."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.models.schemas import ManualTransaction, TransactionAnalysis, WebhookPayload
from app.services import processor as pipeline
from app.services.processor import Processor
from app.services.scheduler import SchedulerService


@pytest.fixture
def clients():
    firefly = AsyncMock()
    firefly.accounts.return_value = [
        {"id": "1", "name": "Efectivo", "type": "asset"},
        {"id": "2", "name": "SUPERMERCADO CENTRAL", "type": "expense"},
        {"id": "3", "name": "SALARIO", "type": "revenue"},
    ]
    firefly.categories.return_value = ["Alimentación"]
    firefly.create_transaction.return_value = "tx-1"
    firefly.create_account.return_value = {"id": "4", "name": "OTRO", "type": "expense"}
    llm = AsyncMock()
    llm.analyze.return_value = TransactionAnalysis(
        amount=Decimal("12000"), merchant="SUPERMERCADO CENTRAL", description="Compra"
    )
    return firefly, llm


@pytest.fixture
def service(db, settings, clients, monkeypatch):
    settings.firefly_default_asset_account = "Efectivo"
    settings.manual_default_account = "Efectivo"
    monkeypatch.setattr(pipeline, "should_ignore", lambda *args: False)
    monkeypatch.setattr(pipeline, "parse_transaction", lambda *args: TransactionAnalysis(
        amount=Decimal("12000"), merchant="SUPERMERCADO CENTRAL", description="Compra"
    ))
    return Processor(db, *clients, settings=settings)


async def item(service, source="email", ref="mail:1:1", occurred_at=None, **kwargs):
    row = await service.inbox.add(
        source=source, source_ref=ref, sender="banco@example.com", raw_text="Compra $12.000",
        subject_or_title="Compra", occurred_at=occurred_at or datetime(2026, 11, 16, 15, tzinfo=UTC),
        status="pending", **kwargs,
    )
    await service.session.commit()
    return row


async def test_unknown_notifications_never_store_body(service):
    row, accepted = await service.ingest_notification(WebhookPayload(
        app="unknown.app", message="private message", timestamp="1794841200000",
    ))
    assert not accepted
    assert row.status == "ignored"
    assert row.raw_text is None
    assert row.occurred_at == datetime.fromtimestamp(1794841200, UTC)


async def test_known_notification_is_durable_and_reference_deduplicated(service):
    await service.senders.add("com.bank")
    payload = WebhookPayload(app="com.bank", message="short", big_text="long", timestamp="1794841200000")
    row, accepted = await service.ingest_notification(payload)
    again, second = await service.ingest_notification(payload)
    assert accepted and not second
    assert row.id == again.id
    assert row.raw_text == "long"
    assert row.status == "pending"
    assert (await service.inbox.get(row.id)) is not None


async def test_sms_recognizes_sender_not_app(service):
    await service.senders.add("Bancolombia")
    row, accepted = await service.ingest_notification(WebhookPayload(
        type="sms", sender="Bancolombia", app="sms.reader", message="Compra", date="2026-11-16 10:00",
    ))
    assert accepted and row.source == "sms"
    assert row.occurred_at.hour == 15


async def test_rules_precede_parser_and_llm(service, clients, monkeypatch):
    monkeypatch.setattr(pipeline, "should_ignore", lambda *args: True)
    parser = AsyncMock()
    monkeypatch.setattr(pipeline, "parse_transaction", parser)
    row = await service.process(await item(service))
    assert (row.status, row.method) == ("ignored", "rule-ignore")
    parser.assert_not_called()
    clients[1].analyze.assert_not_called()
    clients[0].create_transaction.assert_not_called()


async def test_parser_firefly_payload_and_idempotent_processing(service, clients):
    row = await service.process(await item(service))
    assert row.status == "created" and row.method == "parser"
    await service.process(row)
    clients[0].create_transaction.assert_awaited_once()
    clients[1].analyze.assert_not_called()
    payload = clients[0].create_transaction.call_args.args[0]
    assert payload["apply_rules"] is True
    split = payload["transactions"][0]
    assert split["external_id"] == "fa:mail:1:1"
    assert split["tags"] == ["auto", "email"]
    assert split["date"] == "2026-11-16T10:00:00-05:00"
    assert split["source_id"] == "1" and split["destination_id"] == "2"


async def test_llm_receives_existing_categories_accounts_cycle(service, clients, monkeypatch):
    monkeypatch.setattr(pipeline, "parse_transaction", lambda *args: None)
    row = await service.process(await item(service), cycle_id="stable-cycle")
    assert row.method == "llm"
    assert clients[1].analyze.call_args.kwargs == {
        "categories": ["Alimentación"], "accounts": ["Efectivo"], "cycle_id": "stable-cycle",
    }


async def test_cross_channel_dedup_but_not_same_channel(service, clients):
    first = await service.process(await item(service))
    same = await service.process(await item(service, ref="mail:1:2"))
    phone = await service.process(await item(service, source="notification", ref="phone:1"))
    sms = await service.process(await item(service, source="sms", ref="sms:1"))
    assert first.status == same.status == "created"
    assert phone.status == sms.status == "duplicate"
    assert phone.duplicate_of_id in {first.id, same.id}
    assert clients[0].create_transaction.await_count == 2


async def test_phone_family_does_not_deduplicate_itself(service, clients):
    first = await service.process(await item(service, source="notification", ref="phone:1"))
    second = await service.process(await item(service, source="sms", ref="sms:1"))
    assert first.status == second.status == "created"
    assert clients[0].create_transaction.await_count == 2


async def test_dedup_respects_window(service):
    await service.process(await item(service))
    row = await service.process(await item(
        service, source="sms", ref="sms:later",
        occurred_at=datetime(2026, 11, 16, 18, tzinfo=UTC),
    ))
    assert row.status == "created"


async def test_retries_backoff_and_terminal_failure(service, clients):
    clients[0].create_transaction.side_effect = RuntimeError("offline")
    row = await service.process(await item(service))
    assert row.status == "pending" and row.attempts == 1
    assert row.last_error == "offline"
    assert 290 < (row.next_attempt_at - datetime.now(UTC)).total_seconds() <= 300
    await service.process(row)
    assert row.attempts == 1
    row.attempts = service.settings.max_attempts - 1
    row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await service.inbox.save()
    row = await service.process(row)
    assert row.status == "failed" and row.next_attempt_at is None


async def test_dry_run_does_not_create_or_mutate(service, clients):
    row = await item(service)
    preview = await service.process(row, dry_run=True)
    assert preview["method"] == "parser" and preview["amount"] == Decimal("12000")
    assert row.status == "pending" and row.method is None and row.attempts == 0
    clients[0].create_transaction.assert_not_called()
    clients[0].create_account.assert_not_called()


async def test_no_asset_creation_when_missing(service, clients):
    service.settings.firefly_default_asset_account = "Missing"
    row = await service.process(await item(service))
    assert row.status == "pending" and "Missing" in row.last_error
    clients[0].create_account.assert_not_called()
    clients[0].create_transaction.assert_not_called()


async def test_account_map_overrides_llm_asset(service, clients, monkeypatch):
    service.settings.account_map = {"banco@example": "Efectivo"}
    monkeypatch.setattr(pipeline, "parse_transaction", lambda *args: TransactionAnalysis(
        amount=12000, merchant="SUPERMERCADO CENTRAL", asset_account="Invented", category="Invented",
    ))
    row = await service.process(await item(service))
    assert row.asset_account == "Efectivo" and row.category is None


async def test_cache_reused_and_force_refresh(service, clients):
    await service.accounts()
    await service.categories()
    await service.accounts()
    clients[0].accounts.assert_awaited_once()
    clients[0].categories.assert_awaited_once()
    await service.sync(force=True)
    assert clients[0].accounts.await_count == 2


async def test_date_only_preserves_occurrence_time(service, monkeypatch):
    monkeypatch.setattr(pipeline, "parse_transaction", lambda *args: TransactionAnalysis(
        amount=12000, merchant="SUPERMERCADO CENTRAL", date="2026-11-16",
    ))
    row = await service.process(await item(service))
    assert row.occurred_at == datetime(2026, 11, 16, 15, tzinfo=UTC)


async def test_manual_deposit_uses_revenue_and_is_durable(service, clients):
    row = await service.manual(ManualTransaction(
        amount=2500000, description="Nómina", merchant="SALARIO", type="deposit",
        date="2026-11-16T10:00:00-05:00",
    ))
    assert row.status == "created" and row.method == "manual"
    split = clients[0].create_transaction.call_args.args[0]["transactions"][0]
    assert split["source_id"] == "3" and split["destination_id"] == "1"
    assert split["external_id"].startswith("fa:manual:")


async def test_poll_stores_before_processing_and_advances_cursor(service, monkeypatch):
    service.settings.mail_accounts = [type("Account", (), {"name": "primary"})()]
    client = AsyncMock()
    client.poll.return_value = ("v1", 3, [
        {"uid": 1, "sender": "bank@example.com", "subject": "Compra", "body": "body", "occurred_at": datetime.now(UTC)},
        {"uid": 3, "sender": "private@example.com", "subject": "Personal", "body": "private body", "occurred_at": datetime.now(UTC)},
    ])
    monkeypatch.setattr(pipeline, "IMAPClient", lambda account: client)
    await service.senders.add("bank@example")
    result = await service.poll_mail()
    assert result["queued"] == result["ignored"] == 1
    assert (await service.inbox.by_ref("primary:v1:1")).status == "pending"
    assert (await service.inbox.by_ref("primary:v1:3")).raw_text is None
    state = await service.mail_states.get("primary")
    assert state.last_uid == 3 and state.uidvalidity == "v1"
    assert state.last_error is None and state.last_poll_at.tzinfo is not None


async def test_sender_learning_rejects_hallucinated_keywords(service, clients, monkeypatch):
    service.settings.mail_accounts = [type("Account", (), {"name": "primary"})()]
    client = AsyncMock()
    client.poll.return_value = ("v1", 1, [{"sender": "alerts@newbank.co"}])
    monkeypatch.setattr(pipeline, "IMAPClient", lambda account: client)
    clients[1].learn_senders.return_value = ["newbank.co", "invented.co", "newbank.co"]
    result = await service.learn_senders()
    assert result["senders_learned"] == 1
    assert await service.senders.matches("alerts@newbank.co")
    assert not await service.senders.matches("invented.co")


async def test_scheduler_contract(settings):
    cycle, learn = AsyncMock(return_value={"processed": 1}), AsyncMock(return_value={"senders_learned": 1})
    scheduler = SchedulerService(cycle, learn, settings)
    assert {job["id"] for job in scheduler.status()["jobs"]} == {"cycle", "sender_learning"}
    assert await scheduler.trigger("cycle") == {"job": "cycle", "result": {"processed": 1}}
    with pytest.raises(ValueError):
        await scheduler.trigger("obsolete_job")


async def test_merchant_prefix_uses_existing_expense(service, clients, monkeypatch):
    monkeypatch.setattr(pipeline, "parse_transaction", lambda *args: TransactionAnalysis(
        amount=12000, merchant="PAYU*SUPERMERCADO CEN",
    ))
    row = await service.process(await item(service))
    assert row.status == "created" and row.merchant == "SUPERMERCADO CEN"
    clients[0].create_account.assert_not_called()
    assert clients[0].create_transaction.call_args.args[0]["transactions"][0]["destination_id"] == "2"


async def test_explicit_message_time_is_local_with_offset(service, clients, monkeypatch):
    monkeypatch.setattr(pipeline, "parse_transaction", lambda *args: TransactionAnalysis(
        amount=12000, merchant="SUPERMERCADO CENTRAL", date="2026-11-15T22:45:00",
    ))
    row = await service.process(await item(service))
    assert row.occurred_at == datetime(2026, 11, 16, 3, 45, tzinfo=UTC)
    assert clients[0].create_transaction.call_args.args[0]["transactions"][0]["date"] == "2026-11-15T22:45:00-05:00"


async def test_processor_uses_validated_analysis_currency(service, clients, monkeypatch):
    monkeypatch.setattr(pipeline, "parse_transaction", lambda *args: None)
    cop = await item(service)
    cop.raw_text += " Información: consulta nuestras tarifas USD"
    await service.inbox.save()
    cop = await service.process(cop)
    assert cop.currency == "COP"
    clients[1].analyze.return_value = TransactionAnalysis(amount=12000, currency="USD", merchant="SUPERMERCADO CENTRAL")
    usd = await service.process(await item(service, ref="mail:usd"))
    assert usd.currency == "USD"


async def test_expired_cache_refreshes(service, clients, monkeypatch):
    await service.sync()
    cached_at = pipeline._CACHES[clients[0]]["at"]
    monkeypatch.setattr(pipeline, "monotonic", lambda: cached_at + service.settings.firefly_cache_ttl_minutes * 60 + 1)
    await service.categories()
    assert clients[0].categories.await_count == 2


async def test_mail_errors_keep_cursor_for_retry(service, monkeypatch):
    service.settings.mail_accounts = [type("Account", (), {"name": "primary"})()]
    state = await service.mail_states.get("primary")
    state.uidvalidity, state.last_uid = "v1", 99
    await service.mail_states.save()
    client = AsyncMock()
    client.poll.side_effect = OSError("mail offline")
    monkeypatch.setattr(pipeline, "IMAPClient", lambda account: client)
    result = await service.poll_mail()
    assert result["errors"] == {"primary": "mail offline"}
    state = await service.mail_states.get("primary")
    assert state.last_uid == 99 and state.uidvalidity == "v1"
    assert state.last_error == "mail offline"


async def test_llm_failure_is_durable_then_next_cycle_creates(service, clients, monkeypatch):
    monkeypatch.setattr(pipeline, "parse_transaction", lambda *args: None)
    clients[1].analyze.side_effect = [RuntimeError("LLM offline"), TransactionAnalysis(amount=12000, merchant="SUPERMERCADO CENTRAL")]
    row = await service.process(await item(service))
    assert row.status == "pending" and row.attempts == 1
    assert row.next_attempt_at > datetime.now(UTC)
    clients[0].create_transaction.assert_not_called()
    row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await service.inbox.save()
    await service.session.commit()
    result = await service.cycle()
    assert result["by_status"] == {"created": 1}
    assert (await service.inbox.get(row.id)).status == "created"
    assert clients[1].analyze.await_count == 2


@pytest.mark.parametrize("subject", ["Compra rechazada", "Tu fecha de pago ya está cerca Lulo Bank"])
async def test_real_lulo_ignore_never_calls_llm(db, settings, clients, subject):
    actual = Processor(db, *clients, settings=settings)
    row = await actual.inbox.add(source="email", source_ref=subject, sender="notificaciones@lulobank.com", subject_or_title=subject, raw_text="Monto $354.011,56", occurred_at=datetime.now(UTC))
    await db.commit()
    row = await actual.process(row)
    assert (row.status, row.method) == ("ignored", "rule-ignore")
    clients[1].analyze.assert_not_called()
    clients[0].create_transaction.assert_not_called()


async def test_real_parser_and_local_twenty_hour_date(db, settings, clients):
    actual = Processor(db, *clients, settings=settings)
    row = await actual.inbox.add(source="notification", source_ref="local20", sender="Bancolombia", subject_or_title="Compra", raw_text="Compraste $12.000 en SUPERMERCADO CENTRAL con tu T.Deb *1234", occurred_at=datetime(2026, 11, 17, 1, tzinfo=UTC))
    await db.commit()
    row = await actual.process(row)
    assert row.status == "created" and row.method == "parser"
    assert clients[0].create_transaction.call_args.args[0]["transactions"][0]["date"] == "2026-11-16T20:00:00-05:00"
    clients[1].analyze.assert_not_called()


async def test_lulo_footer_does_not_ignore_or_change_cop(db, settings, clients):
    actual = Processor(db, *clients, settings=settings)
    body = "Compra por $20.000 en DLO*Didi con tu tarjeta débito 7556. " + "Detalles de tu compra. " * 20
    body += "Nunca te pediremos tu clave dinámica ni códigos de seguridad. Conoce nuestras promociones y descuentos. Tarifas USD."
    row = await actual.inbox.add(source="email", source_ref="lulo-footer", sender="Notificaciones Lulo bank <notificaciones@lulobank.com>", subject_or_title="Compra realizada", raw_text=body, occurred_at=datetime.now(UTC))
    await db.commit()
    row = await actual.process(row)
    assert (row.status, row.method, row.currency) == ("created", "parser", "COP")
    assert row.merchant == "DIDI"
    clients[1].analyze.assert_not_called()


async def test_short_otp_notification_is_ignored(db, settings, clients):
    actual = Processor(db, *clients, settings=settings)
    row = await actual.inbox.add(source="notification", source_ref="otp", sender="Lulo", subject_or_title="Seguridad", raw_text="Tu código de verificación es 123456", occurred_at=datetime.now(UTC))
    await db.commit()
    row = await actual.process(row)
    assert (row.status, row.method) == ("ignored", "rule-ignore")
    clients[1].analyze.assert_not_called()


async def test_dry_cycle_previews_duplicate_without_persistence(service, clients):
    existing = await service.process(await item(service))
    queued = await item(service, source="notification", ref="preview-phone")
    result = await service.cycle(dry_run=True)
    assert result["mail"] == {"skipped": True}
    assert result["previews"] == [{"inbox_id": queued.id, "method": "parser", "amount": Decimal("12000"), "currency": "COP", "merchant": "SUPERMERCADO CENTRAL", "category": None, "asset_account": "Efectivo", "transaction_type": "withdrawal", "date": "2026-11-16T10:00:00-05:00", "duplicate_of": existing.id, "status": "duplicate"}]
    unchanged = await service.inbox.get(queued.id)
    assert unchanged.status == "pending" and unchanged.amount is None and unchanged.method is None
    assert clients[0].create_transaction.await_count == 1


async def test_llm_dry_cycle_exposes_paid_extraction(service, clients, monkeypatch):
    monkeypatch.setattr(pipeline, "parse_transaction", lambda *args: None)
    row = await item(service)
    result = await service.cycle(dry_run=True)
    assert result["previews"][0]["method"] == "llm"
    assert result["previews"][0]["amount"] == Decimal("12000")
    clients[1].analyze.assert_awaited_once()
    clients[0].create_transaction.assert_not_called()
    assert (await service.inbox.get(row.id)).method is None


async def test_dry_run_endpoint_includes_previews(client, service, clients):
    from app.api.dependencies import get_processor
    from app.main import app

    row = await item(service)
    app.dependency_overrides[get_processor] = lambda: service
    response = await client.post("/api/v1/processing/run?dry_run=true", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    assert response.json()["previews"][0]["inbox_id"] == row.id
    assert response.json()["previews"][0]["method"] == "parser"
    assert (await service.inbox.get(row.id)).status == "pending"
    clients[0].create_transaction.assert_not_called()
