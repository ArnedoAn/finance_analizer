"""Phase 3: alerts table, anti-noise, generated alert types and the polling endpoints (contract 5, 13, 14)."""
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db.models import Notification
from app.db.models_alerts import Alert
from app.db.models_devices import Device
from app.services import alerts as alerting
from app.services import processor as pipeline

ADMIN_KEY = "test-admin-key-01234567890123456789"
PHONE_KEY = "test-phone-key-01234567890123456789"
HEADERS = {"X-API-Key": ADMIN_KEY}


@pytest.fixture(autouse=True)
def alert_settings(monkeypatch, settings, tmp_path):
    monkeypatch.setattr(alerting, "get_settings", lambda: settings)
    settings.backup_status_file = str(tmp_path / "missing-backup-status.json")


@pytest.fixture
def fake_suggestions(monkeypatch):
    from app.services import suggestions

    async def suggest(session, item, limit=3):
        return ["Alimentación", "Transporte"]

    async def question_for(session, item):
        return {"question": "¿Este cobro es de Alimentación?", "options": ["Alimentación", "Transporte"]}

    monkeypatch.setattr(suggestions, "suggest", suggest)
    monkeypatch.setattr(suggestions, "question_for", question_for)


async def make_item(service, source="email", **changes):
    values = dict(
        source=source, source_ref=str(uuid4()), sender="alertas@nequi.com.co",
        raw_text="Pagaste $12.000 en SUPERMERCADO CENTRAL", subject_or_title="Compra",
        occurred_at=datetime.now(UTC), status="pending",
    )
    if source == "notification":
        values.update(app="com.nequi.MobileApp", sender="com.nequi.MobileApp")
    values.update(changes)
    row = await service.inbox.add(**values)
    await service.session.commit()
    return row


async def rows(db, type=None):
    query = select(Alert).order_by(Alert.id)
    if type:
        query = query.where(Alert.type == type)
    return list((await db.scalars(query)).all())


def fake_clock(monkeypatch, moment):
    monkeypatch.setattr(pipeline, "_now", lambda: moment)


def test_money_is_full_and_spanish_formatted():
    assert alerting.money(Decimal("2500000.00")) == "$2.500.000 COP"
    assert alerting.money(Decimal("1250000.50")) == "$1.250.000,50 COP"
    assert alerting.money(Decimal("10"), "USD") == "$10 USD"


async def test_emit_assigns_increasing_ids_and_serializes_contract_shape(db):
    first = await alerting.emit(db, "digest_daily", title="Uno", body="a", dedupe_key="a")
    second = await alerting.emit(
        db, "pending_review", title="Dos", body="b", item_id=7, amount=Decimal("1500"), currency="COP",
        suggested_categories=["Transporte"], question="¿Taxi?", options=["Sí", "No"], dedupe_key="b",
    )
    assert second.id > first.id
    assert alerting.alert_data(first) == {
        "id": first.id, "type": "digest_daily", "created_at": first.created_at, "title": "Uno", "body": "a",
        "item_id": None, "amount": None, "currency": None, "suggested_categories": [], "question": None,
        "options": [], "acked_at": None,
    }
    data = alerting.alert_data(second)
    assert data["amount"] == "1500.00" and data["suggested_categories"] == ["Transporte"]
    assert data["question"] == "¿Taxi?" and data["options"] == ["Sí", "No"] and data["item_id"] == 7


async def test_anti_noise_defaults_to_type_and_setting_interval(db, settings):
    assert await alerting.emit(db, "phone_silence", title="t", body="b")
    assert await alerting.emit(db, "phone_silence", title="t", body="b") is None
    assert await alerting.emit(db, "backup_failed", title="t", body="b")
    settings.notify_min_interval_minutes = 0
    assert await alerting.emit(db, "phone_silence", title="t", body="b")


async def test_anti_noise_is_per_dedupe_key_and_expires(db):
    async def send(key):
        return await alerting.emit(db, "system_failure", title="t", body="b", dedupe_key=key, min_interval_minutes=60)

    assert await send("failed:1")
    assert await send("failed:1") is None
    assert await send("failed:2")
    await db.execute(update(Notification).where(Notification.key == "alert:failed:1").values(last_sent_at=datetime.now(UTC) - timedelta(minutes=61)))
    assert await send("failed:1")
    assert len(await rows(db)) == 3


async def test_alerts_endpoint_pages_in_order_and_acks(client, db):
    for number in range(3):
        await alerting.emit(db, "digest_daily", title=f"Resumen {number}", body="b", dedupe_key=f"k{number}")
    await db.commit()
    first = (await client.get("/api/v1/alerts?limit=2", headers=HEADERS)).json()
    ids = [row["id"] for row in first["items"]]
    assert len(ids) == 2 and ids == sorted(ids) and first["last_id"] == ids[-1]
    assert first["items"][0]["title"] == "Resumen 0"
    rest = (await client.get(f"/api/v1/alerts?after_id={first['last_id']}", headers=HEADERS)).json()
    assert [row["title"] for row in rest["items"]] == ["Resumen 2"] and rest["last_id"] > first["last_id"]
    empty = (await client.get(f"/api/v1/alerts?after_id={rest['last_id']}", headers=HEADERS)).json()
    assert empty == {"items": [], "last_id": rest["last_id"]}

    acked = await client.post(f"/api/v1/alerts/{ids[0]}/ack", headers=HEADERS)
    assert acked.status_code == 200 and acked.json()["id"] == ids[0] and acked.json()["acked_at"]
    visible = [row["id"] for row in (await client.get("/api/v1/alerts", headers=HEADERS)).json()["items"]]
    assert ids[0] not in visible and len(visible) == 2
    everything = (await client.get("/api/v1/alerts?include_acked=true", headers=HEADERS)).json()["items"]
    assert [row["id"] for row in everything][0] == ids[0] and len(everything) == 3


async def test_alerts_endpoint_validation_and_scopes(client):
    assert (await client.get("/api/v1/alerts?limit=0", headers=HEADERS)).status_code == 422
    assert (await client.get("/api/v1/alerts?limit=501", headers=HEADERS)).status_code == 422
    assert (await client.post("/api/v1/alerts/999/ack", headers=HEADERS)).status_code == 404
    assert (await client.get("/api/v1/alerts")).status_code == 401
    assert (await client.get("/api/v1/alerts", headers={"X-API-Key": PHONE_KEY})).status_code in {401, 403}


async def test_pending_review_alert_has_suggestions_and_question(service, db, extraction, monkeypatch, fake_suggestions):
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: replace(extraction, kind="review", reason="two transactions"))
    row = await service.process(await make_item(service))
    assert row.status == "needs_review"
    review, = await rows(db, "pending_review")
    assert review.item_id == row.id and review.suggested_categories == ["Alimentación", "Transporte"]
    assert review.amount == Decimal("12000.00") and "$12.000 COP" in review.body
    assert "Pagaste" not in review.body and "Compra" not in review.body
    question, = await rows(db, "question")
    assert question.item_id == row.id and question.question == "¿Este cobro es de Alimentación?"
    assert question.options == ["Alimentación", "Transporte"]


async def test_pending_review_survives_suggestion_failure(service, db, extraction, monkeypatch):
    from app.services import suggestions

    async def broken(*args, **kwargs):
        raise RuntimeError("suggestions down")

    monkeypatch.setattr(suggestions, "suggest", broken)
    monkeypatch.setattr(suggestions, "question_for", broken)
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: replace(extraction, kind="review", reason="two transactions"))
    await service.process(await make_item(service))
    review, = await rows(db, "pending_review")
    assert review.suggested_categories == [] or review.suggested_categories is None
    assert not await rows(db, "question")


async def test_big_amount_alert_above_review_threshold(service, db, extraction, monkeypatch, fake_suggestions):
    monkeypatch.setattr(pipeline, "parse", lambda *a, **kw: replace(extraction, amount=Decimal("2500000.00")))
    row = await service.process(await make_item(service))
    assert row.status == "needs_review"
    big, = await rows(db, "big_amount")
    assert big.item_id == row.id and big.amount == Decimal("2500000.00")
    assert "$2.500.000 COP" in big.body and "$2.000.000 COP" in big.body
    # Re-announcing the same item never repeats it.
    await service._announce(row, "pending")
    assert len(await rows(db, "big_amount")) == 1


async def test_ordinary_created_item_emits_only_new_charge(service, db):
    row = await service.process(await make_item(service))
    assert row.status == "created"
    assert [alert.type for alert in await rows(db)] == ["new_charge"]


async def test_duplicate_item_emits_duplicate_suspect(service, db):
    first = await service.process(await make_item(service))
    second = await service.process(await make_item(service, source="notification"))
    assert first.status == "created" and second.status == "duplicate"
    alert, = await rows(db, "duplicate_suspect")
    assert alert.item_id == second.id and f"#{first.id}" in alert.body and alert.amount == Decimal("12000.00")


async def test_budget_alerts_fire_once_per_month(service, db, clients):
    clients[0].budget_progress.return_value = [
        {"name": "Mercado", "spent": Decimal("850000.00"), "limit": Decimal("1000000.00")},
        {"name": "Transporte", "spent": Decimal("300000.00"), "limit": Decimal("250000.00")},
        {"name": "Ocio", "spent": Decimal("10.00"), "limit": Decimal("1000000.00")},
        {"name": "Sin tope", "spent": Decimal("500.00"), "limit": Decimal("0.00")},
    ]
    await service.alerts()
    await service.alerts()
    warning, = await rows(db, "budget_80")
    exceeded, = await rows(db, "budget_100")
    assert "Mercado" in warning.title and warning.amount == Decimal("850000.00")
    assert "$850.000 COP" in warning.body and "$1.000.000 COP" in warning.body and "85 %" in warning.body
    assert "Transporte" in exceeded.title and "$300.000 COP" in exceeded.body and "$250.000 COP" in exceeded.body
    start, end = clients[0].budget_progress.call_args.args
    assert start.day == 1 and (end + timedelta(days=1)).day == 1 and start.month == end.month


@pytest.mark.parametrize(("payload", "title"), [
    ({"status": "failed", "detail": "rsync exit 23"}, "Falló la copia de seguridad"),
    ({"status": "ok", "last_success_at": (datetime.now(UTC) - timedelta(days=2)).isoformat()}, "Copia de seguridad atrasada"),
])
async def test_backup_failed_alert_for_failed_and_stale(service, db, settings, tmp_path, payload, title):
    path = tmp_path / "backup-status.json"
    path.write_text(json.dumps({"last_run_at": datetime.now(UTC).isoformat(), **payload}))
    settings.backup_status_file = str(path)
    await service.alerts()
    alert, = await rows(db, "backup_failed")
    assert alert.title == title and "rsync" not in alert.body
    await service.alerts()
    assert len(await rows(db, "backup_failed")) == 1


async def test_healthy_backup_emits_nothing(service, db, settings, tmp_path):
    path = tmp_path / "backup-status.json"
    path.write_text(json.dumps({"status": "ok", "last_success_at": datetime.now(UTC).isoformat()}))
    settings.backup_status_file = str(path)
    await service.alerts()
    assert not await rows(db, "backup_failed")


async def stale_phone_event(service, hours=48):
    return await make_item(service, source="sms", sender="85954", created_at=datetime.now(UTC) - timedelta(hours=hours))


async def test_phone_silence_alert_only_without_recent_phone_events(service, db):
    await stale_phone_event(service)
    await service.alerts()
    alert, = await rows(db, "phone_silence")
    assert "24 h" in alert.body
    await db.execute(update(Notification).values(last_sent_at=datetime.now(UTC) - timedelta(days=3)))
    await make_item(service, source="sms", sender="85954")
    await service.alerts()
    assert len(await rows(db, "phone_silence")) == 1


async def test_phone_silence_without_any_phone_event_does_not_alert(service, db):
    # Same rule as GET /system/capture: nothing to compare against is not "silent".
    await make_item(service, source="email")  # email is not a phone source
    assert await alerting.check_phone_silence(db, service.settings) is None
    await service.alerts()
    assert not await rows(db, "phone_silence")


async def test_emit_takes_advisory_lock_first_only_on_postgres(db, monkeypatch):
    statements = []
    real_execute = db.execute

    async def spy(statement, *args, **kwargs):
        text = str(statement)
        if "pg_advisory_xact_lock" in text:
            statements.append(statement.compile().params)
            return None
        statements.append(text.split()[0])
        return await real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", spy)
    monkeypatch.setattr(alerting, "_is_postgres", lambda session: False)  # same check on SQLite and Postgres runs
    assert await alerting.emit(db, "system_failure", title="t", body="b", dedupe_key="sqlite-1")
    assert not any(isinstance(entry, dict) for entry in statements)

    statements.clear()
    monkeypatch.setattr(alerting, "_is_postgres", lambda session: True)
    assert await alerting.emit(db, "system_failure", title="t", body="b", dedupe_key="pg-1")
    assert list(statements[0].values()) == [alerting.ALERT_ORDER_LOCK]  # first: before the claim and the insert


async def test_key_expiring_alerts_only_for_active_keys_expiring_within_seven_days(service, db, settings):
    now = datetime.now(UTC)
    db.add_all([
        Device(name="Pixel", key_hash="a" * 64, scopes=["read"], expires_at=now + timedelta(days=3)),
        Device(name="Lejano", key_hash="b" * 64, scopes=["read"], expires_at=now + timedelta(days=30)),
        Device(name="Vencido", key_hash="c" * 64, scopes=["read"], expires_at=now - timedelta(days=1)),
        Device(name="Revocado", key_hash="d" * 64, scopes=["read"], expires_at=now + timedelta(days=2), revoked_at=now),
        Device(name="Sin caducidad", key_hash="e" * 64, scopes=["read"]),
    ])
    await db.commit()
    await service.alerts()
    alert, = await rows(db, "key_expiring")
    assert "Pixel" in alert.body


@pytest.mark.parametrize(("moment", "title"), [
    (datetime(2026, 11, 30, 18, tzinfo=UTC), "Hoy cierra noviembre de 2026"),
    (datetime(2026, 12, 1, 15, tzinfo=UTC), "Cierre de noviembre de 2026"),
])
async def test_month_close_on_last_day_and_first_of_next_month(service, db, monkeypatch, moment, title):
    fake_clock(monkeypatch, moment)
    await service.alerts()
    alert, = await rows(db, "month_close")
    assert alert.title == title and "Ingresos $0 COP" in alert.body


async def test_no_month_close_mid_month(service, db, monkeypatch):
    fake_clock(monkeypatch, datetime(2026, 11, 15, 15, tzinfo=UTC))
    await service.alerts()
    assert not await rows(db, "month_close")


@pytest.mark.parametrize(("weekly", "kind"), [(False, "digest_daily"), (True, "digest_weekly")])
async def test_digest_emits_summary_alert(service, db, weekly, kind):
    await service.digest(weekly=weekly)
    await service.digest(weekly=weekly)
    alert, = await rows(db, kind)
    assert "{" not in alert.body and "COP" in alert.body


async def test_digest_summarizes_with_full_amounts(db):
    report = {"start": "2026-11-20", "totals": {"withdrawal": "125000.00", "deposit": "3000000.00"},
              "by_category": {"Mercado": "100000.00", "Ocio": "25000.00"}, "top_merchants": [{"merchant": "EXITO", "amount": "100000.00"}]}
    previous = {"start": "2026-11-19", "totals": {"withdrawal": "80000.00", "deposit": "0"}, "by_category": {}, "top_merchants": []}
    stats = {"pending": 1, "needs_review": 2, "failed": 0}
    alert = await alerting.digest(db, report, previous, stats, weekly=False)
    assert alert.type == "digest_daily" and alert.title == "Resumen del 20/11/2026"
    assert "$125.000 COP" in alert.body and "$3.000.000 COP" in alert.body and "$80.000 COP" in alert.body
    assert "EXITO" in alert.body and "Pendientes 1, por revisar 2, fallidos 0" in alert.body
    assert (await alerting.digest(db, report, previous, stats, weekly=False)) is None


async def test_missing_own_account_emits_system_failure(service, db, add_own_account):
    await add_own_account("Fantasma")
    await service.sync(force=True)
    await service.sync(force=True)
    alert, = await rows(db, "system_failure")
    assert "Fantasma" in alert.body and alert.dedupe_key == "missing-account:Fantasma"


async def test_reconcile_difference_emits_system_failure(service, db):
    row = await service.process(await make_item(service))
    assert row.status == "created"
    await service.reconcile()
    alert, = await rows(db, "system_failure")
    assert "1 movimientos" in alert.body


async def test_periodic_checks_run_external_checks_in_order_and_survive_failures(service, db, monkeypatch):
    from app.services import anomalies, reports

    calls = []

    async def failing(session, settings, firefly):
        calls.append("reports")
        raise RuntimeError("reports exploded")

    async def recording(session, settings, firefly):
        calls.append("anomalies")

    monkeypatch.setattr(reports, "alert_check", failing)
    monkeypatch.setattr(anomalies, "alert_check", recording)
    await stale_phone_event(service)
    await service.alerts()
    assert calls == ["reports", "anomalies"]
    assert await rows(db, "phone_silence")


async def test_budget_failure_does_not_abort_the_other_checks(service, db, clients):
    await stale_phone_event(service)
    clients[0].budget_progress.side_effect = RuntimeError("firefly down")
    await service.alerts()
    assert await rows(db, "phone_silence") and not await rows(db, "budget_80")
