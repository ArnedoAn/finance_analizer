"""Instant dashboard snapshots, digest policy and grouping, capture watch and the persistent server error log."""
import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.clients.firefly import FireflyClient
from app.clients.imap import IMAPClient
from app.clients.llm import LLMClient
from app.core import error_log
from app.core.config import MailAccount, Settings
from app.core.exceptions import FireflyError, PermanentError
from app.core.timezone import now_utc
from app.db.models import DashboardSnapshot, InboxItem, MailAccountState, ServerError
from app.db.models_alerts import Alert
from app.db.models_assistant import Audit
from app.services import capture, digest, flags, reports, snapshots
from app.services import processor as pipeline
from tests.test_devices_v4 import auth, enroll_key

API = "/api/v1"
ADMIN = {"X-API-Key": "test-admin-key-01234567890123456789"}
PHONE = {"X-API-Key": "test-phone-key-01234567890123456789"}
NOW = datetime(2026, 11, 10, 17, 0, tzinfo=UTC)  # 12:00 in Bogota


def split(kind, when, amount, category=None, dest="TIENDA", source="Nequi", currency="COP"):
    return {
        "type": kind, "date": when, "amount": amount, "category_name": category, "destination_name": dest,
        "source_name": source, "currency_code": currency,
    }


SPLITS = [
    split("withdrawal", "2026-11-02T12:00:00-05:00", "100000.00", "Mercado", "EXITO"),
    split("withdrawal", "2026-11-02T18:00:00-05:00", "50000.00", "Mercado", "EXITO"),
    split("withdrawal", "2026-11-03T09:00:00-05:00", "30000.00", None, ""),
    split("deposit", "2026-11-01T10:00:00-05:00", "1000000.00", None, "Nequi", "EMPRESA"),
    split("transfer", "2026-11-04T10:00:00-05:00", "5000.00"),
    split("withdrawal", "2026-11-05T10:00:00-05:00", "20.00", "Ocio", "STEAM", currency="USD"),
    split("withdrawal", "2026-11-11T10:00:00-05:00", "9999.00", "Futuro"),  # after "today"
    split("withdrawal", "2026-10-31T23:30:00-05:00", "7000.00", "Octubre"),  # still October in Bogota
    split("withdrawal", "2026-11-01T02:00:00+00:00", "3000.00", "Octubre"),  # 21:00 on October 31 in Bogota
]


@pytest.fixture
def firefly(clients):
    mock = clients[0]
    mock.period_splits.return_value = SPLITS
    mock.insight_total.side_effect = lambda kind, start, end: Decimal("100.00") if kind == "expense" else Decimal("300.00")
    mock.budget_progress.return_value = [{"name": "Mercado", "spent": Decimal("850000.00"), "limit": Decimal("1000000.00")}]
    return mock


@pytest_asyncio.fixture
async def enabled(db, firefly, settings):
    lock = asyncio.Lock()  # the tests share one session; the real opener gives each task its own

    @asynccontextmanager
    async def opener():
        async with lock:
            yield db, firefly, settings

    snapshots.enable(opener)
    yield
    await snapshots.disable()


@pytest.fixture
def snapshot_api(client, service, settings, monkeypatch):
    monkeypatch.setattr("app.api.routers.snapshots.get_firefly", lambda: service.firefly)
    monkeypatch.setattr("app.api.routers.snapshots.get_settings", lambda: settings)
    return client


def this_month(settings):
    return snapshots.current_month(settings)


async def settle():
    """Wait for every recomputation task."""
    await asyncio.gather(*list(snapshots._inflight.values()))


def add_item(db, **changes):
    values = dict(
        source="email", source_ref=str(uuid4()), sender="alertas@nequi.com.co", status="created", amount=Decimal("1000.00"),
        currency="COP", transaction_type="withdrawal", occurred_at=now_utc(), merchant="TIENDA",
    )
    values.update(changes)
    db.add(InboxItem(**values))


# ---- 1. snapshots ----------------------------------------------------------------------------------------


async def test_compute_aggregates_the_current_month_in_bogota_time(db, settings, firefly):
    data = await snapshots.compute(db, firefly, settings, "2026-11", now=NOW)
    assert data["month"] == "2026-11" and data["as_of"] == "2026-11-10" and data["is_current"] is True
    assert data["totals"] == {"withdrawal": "180000.00", "deposit": "1000000.00", "transfer": "5000.00"}
    assert (data["spent"], data["income"], data["balance"]) == ("180000.00", "1000000.00", "820000.00")
    assert data["by_category"] == [
        {"category": "Mercado", "spent": "150000.00", "count": 2}, {"category": "Sin categoría", "spent": "30000.00", "count": 1},
    ]
    assert data["by_account"] == [{"account": "Nequi", "spent": "180000.00"}]
    assert data["top_merchants"][0] == {"merchant": "EXITO", "spent": "150000.00", "count": 2}
    assert data["top_merchants"][1]["merchant"] == "Sin comercio"
    assert len(data["daily"]) == 30
    assert data["daily"][1] == {"date": "2026-11-02", "spent": "150000.00", "income": "0.00"}
    assert data["daily"][0]["income"] == "1000000.00"
    assert [row["month"] for row in data["cashflow"]][0:1] == ["2025-12"] and data["cashflow"][-1] == {"month": "2026-11", "spent": "100.00", "income": "300.00"}
    assert len(data["cashflow"]) == 12
    assert data["budgets"] == [{"name": "Mercado", "spent": "850000.00", "limit": "1000000.00", "pct": 85}]
    assert data["forecast"]["month"] == "2026-11" and data["daily_available"] == data["forecast"]["daily_available"]
    assert data["unavailable"] == []
    firefly.period_splits.assert_awaited_once()


async def test_compute_past_month_has_no_forecast_and_includes_the_late_night_split(db, settings, firefly):
    data = await snapshots.compute(db, firefly, settings, "2026-10", now=NOW)
    assert data["is_current"] is False and data["as_of"] == "2026-10-31"
    assert data["forecast"] is None and data["daily_available"] is None
    assert data["totals"]["withdrawal"] == "10000.00" and data["by_category"] == [{"category": "Octubre", "spent": "10000.00", "count": 2}]
    future = await snapshots.compute(db, firefly, settings, "2026-12", now=NOW)
    assert future["totals"]["withdrawal"] == "0.00" and future["forecast"] is None


async def test_compute_marks_a_failing_optional_section_unavailable(db, settings, firefly):
    firefly.insight_total.side_effect = RuntimeError("down")
    data = await snapshots.compute(db, firefly, settings, "2026-11", now=NOW)
    assert data["cashflow"] is None and data["unavailable"] == ["cashflow"] and data["spent"] == "180000.00"
    firefly.period_splits.side_effect = FireflyError("down")
    with pytest.raises(FireflyError):
        await snapshots.compute(db, firefly, settings, "2026-11", now=NOW)


async def test_get_without_snapshot_computes_in_line_then_serves_it(snapshot_api, db, settings, firefly):
    month = this_month(settings)
    first = await snapshot_api.get(f"{API}/dashboard/snapshot", headers=ADMIN, params={"month": month})
    assert first.status_code == 200
    body = first.json()
    assert set(body) == {"month", "computed_at", "stale", "data"} and body["month"] == month and body["stale"] is False
    assert body["data"]["month"] == month and firefly.period_splits.await_count == 1
    again = await snapshot_api.get(f"{API}/dashboard/snapshot", headers=ADMIN, params={"month": month})
    assert again.json()["computed_at"] == body["computed_at"] and firefly.period_splits.await_count == 1
    assert (await snapshots.get(db, month)).version == snapshots.VERSION


async def test_old_snapshot_is_served_stale_and_recomputed_in_the_background(snapshot_api, enabled, db, settings, firefly):
    month = this_month(settings)
    await snapshot_api.get(f"{API}/dashboard/snapshot", headers=ADMIN, params={"month": month})
    row = await snapshots.get(db, month)
    row.computed_at = now_utc() - timedelta(hours=3)
    await db.commit()
    stale = await snapshot_api.get(f"{API}/dashboard/snapshot", headers=ADMIN, params={"month": month})
    assert stale.json()["stale"] is True and stale.json()["data"]["month"] == month
    await settle()
    fresh = await snapshot_api.get(f"{API}/dashboard/snapshot", headers=ADMIN, params={"month": month})
    assert fresh.json()["stale"] is False and firefly.period_splits.await_count == 2


async def test_recomputation_is_single_flight_per_month(enabled, db, settings, firefly):
    calls = []

    async def slow(start, end):
        calls.append(start)
        await asyncio.sleep(0.05)
        return SPLITS

    firefly.period_splits.side_effect = slow
    month = this_month(settings)
    first, second = snapshots.run(month), snapshots.run(month)
    assert first is second
    assert await first is True and len(calls) == 1
    # A change that arrives while one runs asks for exactly one more pass.
    running = snapshots.run(month)
    assert snapshots.run(month, again=True) is running and snapshots.run(month, again=True) is running
    await running
    assert len(calls) == 3


async def test_refresh_forces_recomputation_and_needs_review_scope(snapshot_api, db, settings, firefly):
    month = this_month(settings)
    await snapshot_api.get(f"{API}/dashboard/snapshot", headers=ADMIN, params={"month": month})
    refreshed = await snapshot_api.post(f"{API}/dashboard/snapshot/refresh", headers=ADMIN, params={"month": month})
    assert refreshed.status_code == 200 and refreshed.json()["stale"] is False and firefly.period_splits.await_count == 2
    reader = (await enroll_key(snapshot_api, ["read"]))["key"]
    assert (await snapshot_api.post(f"{API}/dashboard/snapshot/refresh", headers=auth(reader), params={"month": month})).status_code == 403
    assert (await snapshot_api.get(f"{API}/dashboard/snapshot", headers=auth(reader), params={"month": month})).status_code == 200
    assert (await snapshot_api.get(f"{API}/dashboard/snapshot", headers=PHONE, params={"month": month})).status_code == 403


@pytest.mark.parametrize("month", ["2026-13", "2026-1", "abcd-ef", "1999-12", "2999-01"])
async def test_invalid_month_is_rejected(snapshot_api, month):
    response = await snapshot_api.get(f"{API}/dashboard/snapshot", headers=ADMIN, params={"month": month})
    assert response.status_code == 422 and response.json()["code"] == "invalid_month"
    assert (await snapshot_api.get(f"{API}/dashboard/snapshot", headers=ADMIN)).status_code == 422


async def test_unavailable_firefly_without_snapshot_is_502(snapshot_api, settings, firefly):
    firefly.period_splits.side_effect = FireflyError("down")
    response = await snapshot_api.get(f"{API}/dashboard/snapshot", headers=ADMIN, params={"month": this_month(settings)})
    assert response.status_code == 502 and response.json()["code"] == "upstream_unavailable"


async def test_after_cycle_refreshes_current_month_and_previous_once_a_day(enabled, db, settings, firefly):
    month = this_month(settings)
    before = snapshots.previous_month(month)
    await snapshots.after_cycle(db, settings)
    await settle()
    assert {row.month for row in (await db.scalars(select(DashboardSnapshot))).all()} == {month, before}
    assert firefly.period_splits.await_count == 2
    await snapshots.after_cycle(db, settings)
    await settle()
    assert firefly.period_splits.await_count == 3  # only the current month again
    row = await snapshots.get(db, before)
    row.computed_at = now_utc() - timedelta(hours=25)
    await db.commit()
    await snapshots.after_cycle(db, settings)
    await settle()
    assert firefly.period_splits.await_count == 5


async def test_cycle_schedules_the_snapshot(enabled, service, db, settings, firefly):
    await service.cycle()
    await settle()
    assert await snapshots.get(db, this_month(settings)) is not None


async def test_touch_debounces_and_only_tracks_current_and_previous_month(enabled, db, settings, firefly, monkeypatch):
    monkeypatch.setattr(snapshots, "DEBOUNCE_SECONDS", 0.05)
    month = this_month(settings)
    for _ in range(3):
        snapshots.touch(month, settings)
        await asyncio.sleep(0.01)
    snapshots.touch("2020-01", settings)
    assert list(snapshots._debounce) == [month]
    await asyncio.sleep(0.2)
    await settle()
    assert firefly.period_splits.await_count == 1 and not snapshots._debounce
    assert await snapshots.get(db, month) is not None


async def test_automatic_triggers_do_nothing_until_enabled(db, settings, firefly):
    snapshots.touch(this_month(settings), settings)
    snapshots.schedule(this_month(settings))
    await snapshots.after_cycle(db, settings)
    assert not snapshots._inflight and not snapshots._debounce


async def test_item_changes_touch_the_snapshot(service, db, monkeypatch):
    touched = []
    monkeypatch.setattr(snapshots, "touch_item", lambda item, settings: touched.append((item.id, item.status)))
    add_item(db, status="pending", source_ref="touch-1", amount=None, transaction_type=None)
    await db.commit()
    row = await service.process(await service.inbox.by_ref("touch-1"))
    assert row.status == "created" and touched == [(row.id, "created")]
    await service.review(row, "void")
    assert touched[-1] == (row.id, "voided")


# ---- 2. digest ----------------------------------------------------------------------------------------


async def test_digest_policy_defaults_and_round_trip(client, db):
    assert (await client.get(f"{API}/settings/digest", headers=ADMIN)).json() == {"daily": True, "weekly": True, "hour": 20, "silence_days": 3}
    saved = await client.put(f"{API}/settings/digest", headers=ADMIN, json={"daily": False, "weekly": True, "hour": 7, "silence_days": 5})
    assert saved.status_code == 200 and saved.json() == {"daily": False, "weekly": True, "hour": 7, "silence_days": 5}
    kept = await client.put(f"{API}/settings/digest", headers=ADMIN, json={"daily": True, "weekly": False, "hour": 0})
    assert kept.json() == {"daily": True, "weekly": False, "hour": 0, "silence_days": 5}
    assert (await client.get(f"{API}/settings/digest", headers=ADMIN)).json() == kept.json()
    audit = (await db.scalars(select(Audit).where(Audit.action == "settings.digest").order_by(Audit.id))).all()
    assert len(audit) == 2 and audit[1].detail["previous"]["hour"] == 7


async def test_digest_policy_validation_and_scopes(client):
    url = f"{API}/settings/digest"
    for body in ({"daily": True, "weekly": True, "hour": 24}, {"daily": True, "weekly": True, "hour": -1},
                 {"daily": True, "weekly": True, "hour": 8, "silence_days": 0}, {"daily": True, "weekly": True, "hour": 8, "extra": 1},
                 {"daily": True, "hour": 8}):
        assert (await client.put(url, headers=ADMIN, json=body)).status_code == 422
    reader = (await enroll_key(client, ["read"]))["key"]
    assert (await client.get(url, headers=auth(reader))).status_code == 200
    assert (await client.put(url, headers=auth(reader), json={"daily": True, "weekly": True, "hour": 8})).status_code == 403
    assert (await client.get(url, headers=PHONE)).status_code == 403


async def test_a_damaged_policy_falls_back_to_defaults_field_by_field(db):
    from app.db.models_flags import RuntimeFlag

    db.add(RuntimeFlag(key=digest.POLICY_KEY, value={"daily": "yes", "weekly": False, "hour": 99, "silence_days": 7}))
    await db.commit()
    assert await digest.policy(db) == {"daily": True, "weekly": False, "hour": 20, "silence_days": 7}


async def test_due_follows_hour_weekday_and_what_was_already_sent(service, db, settings, monkeypatch):
    sunday_night = datetime(2026, 11, 23, 2, 0, tzinfo=UTC)  # Sunday 22 Nov, 21:00 in Bogota
    assert await digest.due(db, settings, sunday_night) == [False, True]
    assert await digest.due(db, settings, datetime(2026, 11, 22, 23, 0, tzinfo=UTC)) == []  # 18:00, before the hour
    assert await digest.due(db, settings, datetime(2026, 11, 24, 2, 0, tzinfo=UTC)) == [False]  # Monday: no weekly
    await digest.set_policy(db, daily=False, weekly=True, hour=20)
    assert await digest.due(db, settings, sunday_night) == [True]
    await digest.set_policy(db, daily=True, weekly=True, hour=22)
    assert await digest.due(db, settings, sunday_night) == []
    await digest.set_policy(db, daily=True, weekly=True, hour=20)
    await db.commit()
    monkeypatch.setattr(pipeline, "_now", lambda: sunday_night)
    sent = await service.digest_tick()
    assert set(sent) == {"daily", "weekly"} and sent["daily"]["start"] == "2026-11-22" and sent["weekly"]["start"] == "2026-11-16"
    assert await service.digest_tick() == {}
    assert sorted(alert.type for alert in (await db.scalars(select(Alert))).all()) == ["digest_daily", "digest_weekly"]


async def test_digest_text_has_movements_spending_top_categories_review_and_capture_failures(service, db):
    add_item(db, amount=Decimal("100000.00"), category="Mercado")
    add_item(db, amount=Decimal("20000.00"), category="Mercado")
    add_item(db, amount=Decimal("5000.00"), category="Transporte")
    add_item(db, amount=Decimal("50000.00"), transaction_type="deposit")
    add_item(db, amount=Decimal("9000.00"), category="Ocio", occurred_at=now_utc() - timedelta(days=5))  # another day
    for _ in range(2):
        add_item(db, status="needs_review")
    add_item(db, status="failed")
    await db.commit()
    result = await service.digest()
    assert result["registered"] == 4 and result["emitted"] is True
    alert = (await db.scalars(select(Alert).where(Alert.type == "digest_daily"))).one()
    assert alert.body.startswith("Hoy se registraron 4 movimientos.")
    assert "Gasto total $125.000 COP" in alert.body
    assert "Top categorías: Mercado ($120.000 COP), Transporte ($5.000 COP)." in alert.body
    assert "2 movimientos en revisión" in alert.body
    assert "Fallos de captura: 1 movimiento no se pudo registrar." in alert.body
    assert (await service.digest())["emitted"] is False


async def test_weekly_digest_covers_the_current_week(service, db):
    add_item(db, amount=Decimal("7000.00"), category="Mercado")
    await db.commit()
    await service.digest(weekly=True)
    alert = (await db.scalars(select(Alert).where(Alert.type == "digest_weekly"))).one()
    assert alert.body.startswith("Esta semana se registró 1 movimiento.") and "Sin fallos de captura" in alert.body


async def test_digest_reports_failing_mailbox_and_llm(service, db, settings):
    settings.mail_accounts = [MailAccount(name="gmail", provider="gmail", username="u@example.com", password="pass12345678")]
    db.add(MailAccountState(name="gmail", consecutive_failures=3))
    for _ in range(3):
        await capture.note_llm(db, False)
    await db.commit()
    await service.digest()
    body = (await db.scalars(select(Alert).where(Alert.type == "digest_daily"))).one().body
    assert "el buzón «gmail» falla al consultarse" in body and "las últimas extracciones con IA fallaron" in body


async def test_new_charges_are_grouped_into_the_daily_digest(service, db):
    row = await service.process(await _email_item(service))
    assert row.status == "created"
    assert not (await db.scalars(select(Alert).where(Alert.type == "new_charge"))).all()
    assert await digest.grouped(db) == (1, Decimal("12000.00"))
    await service.digest()
    body = (await db.scalars(select(Alert).where(Alert.type == "digest_daily"))).one().body
    assert "Registrados automáticamente: 1 cobro por $12.000 COP." in body
    assert await digest.grouped(db) == (0, Decimal(0))


async def test_new_charges_alert_one_by_one_when_the_daily_digest_is_off(service, db):
    await digest.set_policy(db, daily=False, weekly=True, hour=20)
    await db.commit()
    await service.process(await _email_item(service))
    assert len((await db.scalars(select(Alert).where(Alert.type == "new_charge"))).all()) == 1
    assert await digest.grouped(db) == (0, Decimal(0))


async def test_grouping_stops_at_the_auto_approval_cap(db, settings):
    cap = (await flags.policy(db, settings))["max_amount_cop"]
    assert await digest.should_group(db, settings, SimpleNamespace(amount=cap - 1)) is True
    assert await digest.should_group(db, settings, SimpleNamespace(amount=cap)) is False
    assert await digest.should_group(db, settings, SimpleNamespace(amount=None)) is False
    await digest.set_policy(db, daily=False, weekly=True, hour=20)
    assert await digest.should_group(db, settings, SimpleNamespace(amount=Decimal(1))) is False


async def _email_item(service):
    row = await service.inbox.add(
        source="email", source_ref=str(uuid4()), sender="alertas@nequi.com.co", raw_text="Pagaste $12.000 en SUPERMERCADO CENTRAL",
        subject_or_title="Compra", occurred_at=datetime.now(UTC), status="pending",
    )
    await service.session.commit()
    return row


# ---- 3. capture watch ---------------------------------------------------------------------------------


def events(db, count, days_ago, **changes):
    for number in range(count):
        add_item(
            db, status="ignored", source="notification", app="com.nequi.MobileApp", sender="com.nequi.MobileApp",
            created_at=now_utc() - timedelta(days=days_ago, minutes=number), **changes,
        )


async def capture_gaps(db):
    return list((await db.scalars(select(Alert).where(Alert.type == "capture_gap").order_by(Alert.id))).all())


async def test_a_source_that_used_to_arrive_and_went_quiet_alerts_once(db, settings):
    events(db, 3, 10)
    await db.commit()
    created = await capture.check_sources(db, settings, 3)
    alert, = created
    assert alert.type == "capture_gap" and alert.body.startswith("Hace 10 días no llega nada de Nequi (notificaciones)")
    assert await capture.check_sources(db, settings, 3) == []  # deduplicated
    assert len(await capture_gaps(db)) == 1


async def test_source_checks_ignore_recent_events_and_rare_sources(db, settings):
    events(db, 3, 10)
    events(db, 1, 1)  # something arrived inside the window
    await db.commit()
    assert await capture.check_sources(db, settings, 3) == []
    for item in (await db.scalars(select(InboxItem))).all():
        await db.delete(item)
    events(db, 2, 10)  # fewer than three events before: it never "used to arrive"
    await db.commit()
    assert await capture.check_sources(db, settings, 3) == []


async def test_email_and_sms_sources_use_their_own_labels_and_the_silence_days(db, settings):
    for number in range(3):
        add_item(db, status="ignored", source="email", sender="alertas@nequi.com.co", created_at=now_utc() - timedelta(days=4, minutes=number))
        add_item(db, status="ignored", source="sms", sender="85954", created_at=now_utc() - timedelta(days=40 - number))
    await db.commit()
    bodies = [alert.body for alert in await capture.check_sources(db, settings, 3)]
    assert len(bodies) == 1 and bodies[0].startswith("Hace 4 días no llega nada de Nequi (correo)")
    assert await capture.check_sources(db, settings, 5) == []  # four quiet days are not five


async def test_a_failing_mailbox_that_went_quiet_alerts(db, settings):
    db.add(MailAccountState(name="gmail", consecutive_failures=2))
    db.add(MailAccountState(name="yahoo", consecutive_failures=0))
    for name in ("gmail", "yahoo"):
        for number in range(3):
            add_item(db, status="ignored", source="email", account_name=name, sender="x@bank.co", created_at=now_utc() - timedelta(days=6, minutes=number))
    await db.commit()
    alert, = await capture.check_mailboxes(db, settings, 3)
    assert "buzón «gmail»" in alert.body and "Hace 6 días" in alert.body and "2 intentos seguidos" in alert.body


async def test_llm_streak_alerts_after_n_failed_extractions_and_resets_on_success(db):
    for _ in range(2):
        await capture.note_llm(db, False)
    assert await capture.check_llm(db, 3) is None
    await capture.note_llm(db, False)
    alert = await capture.check_llm(db, 3)
    assert alert.type == "capture_gap" and "Las últimas 3 extracciones con IA fallaron" in alert.body
    assert await capture.check_llm(db, 3) is None  # deduplicated
    await capture.note_llm(db, True)
    assert await capture.llm_streak(db) == 0 and await capture.check_llm(db, 1) is None


async def test_processor_tracks_llm_outcomes(service, clients, db, extraction, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", lambda *args, **kwargs: None)
    llm = clients[1]
    llm.analyze.side_effect = RuntimeError("provider down")
    await service.process(await _email_item(service))
    assert await capture.llm_streak(db) == 1
    llm.analyze.side_effect = None
    llm.analyze.return_value = replace(extraction, template_id="llm:test")
    await service.process(await _email_item(service))
    assert await capture.llm_streak(db) == 0


async def test_periodic_checks_run_the_capture_watch(service, db):
    events(db, 3, 10)
    await db.commit()
    await service.alerts()
    alert, = await capture_gaps(db)
    assert "Nequi" in alert.body
    await digest.set_policy(db, daily=True, weekly=True, hour=20, silence_days=15)
    await db.commit()
    assert await capture.check_sources(db, service.settings, 15) == []


# ---- 4. server error log ------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def errlog(db):
    @asynccontextmanager
    async def opener():
        yield db
        await db.commit()

    error_log._pending.clear()
    error_log.install(opener, autoflush=False)
    yield error_log
    await error_log.uninstall()


def pending():
    return list(error_log._pending)


def test_sanitize_removes_secrets_tokens_and_raw_text():
    key = ADMIN["X-API-Key"]
    text = f"fail Bearer abcdef1234567890 api_key=sk-abcdefghijklmnopq password: hunter22 mail a@b.com key {key}"
    cleaned = error_log.sanitize(text)
    for leaked in ("abcdef1234567890", "sk-abcdefghijklmnopq", "hunter22", "a@b.com", key):
        assert leaked not in cleaned
    quoted = error_log.sanitize('Cannot parse "' + "pagaste en la tienda " * 10 + '"')
    assert "[texto omitido]" in quoted and "pagaste" not in quoted
    assert "mundo secreto" not in error_log.sanitize("eco: mundo secreto fin", scrub=["mundo secreto"])
    assert len(error_log.sanitize("ab " * 5000, error_log.MAX_BODY)) == error_log.MAX_BODY
    assert error_log.sanitize(None) == ""


async def test_nothing_is_stored_until_installed():
    error_log._pending.clear()
    error_log.record("firefly", "GET x", "boom")
    assert pending() == []


async def test_record_and_flush_store_a_redacted_row(errlog, db):
    errlog.record("firefly", "GET accounts", "Firefly 500", exc_type="FireflyError", status_code=500, body="ab " * 5000)
    errlog.record("weird", "ab " * 100, "m")
    assert await errlog.flush() is True
    first, second = (await db.scalars(select(ServerError).order_by(ServerError.id))).all()
    assert (first.component, first.operation, first.exc_type, first.status_code) == ("firefly", "GET accounts", "FireflyError", 500)
    assert len(first.response_body) == 4096 and first.at.tzinfo is not None
    assert second.component == "server" and len(second.operation) == 100 and second.status_code is None


async def test_the_queue_and_the_table_keep_the_last_300(errlog, db):
    for number in range(305):
        errlog.record("api", f"op {number}", "m")
    assert len(pending()) == 300
    await errlog.flush()
    for number in range(10):
        db.add(ServerError(component="api", operation=f"extra {number}", message="m"))
    await db.flush()
    assert await errlog.trim(db) == 10
    ids = (await db.scalars(select(ServerError.id).order_by(ServerError.id))).all()
    assert len(ids) == 300 and ids[-1] == max(ids)


async def test_purge_drops_rows_older_than_30_days(db):
    db.add(ServerError(component="api", operation="old", message="m", at=now_utc() - timedelta(days=31)))
    db.add(ServerError(component="api", operation="new", message="m", at=now_utc() - timedelta(days=29)))
    await db.flush()
    assert await error_log.purge(db) == 1
    assert [row.operation for row in (await db.scalars(select(ServerError))).all()] == ["new"]


async def test_cycle_purge_removes_expired_errors(service, db):
    db.add(ServerError(component="api", operation="old", message="m", at=now_utc() - timedelta(days=40)))
    await db.commit()
    await service._purge()
    assert (await db.scalars(select(ServerError))).all() == []


async def test_logging_handler_records_error_records_only(errlog):
    logging.getLogger("app.services.processor").error("alert_check_failed")
    logging.getLogger("app.services.processor").warning("not an error")
    logging.getLogger("sqlalchemy.engine").error("ignored")
    record = logging.LogRecord("app.clients.llm", logging.ERROR, "", 0, {"event": "extract_failed", "error_type": "ValueError", "inbox_id": 4}, None, None)
    error_log._handler.emit(record)
    first, second = pending()
    assert first["component"] == "cycle" and first["message"] == "alert_check_failed"
    assert (second["component"], second["operation"], second["exc_type"]) == ("llm", "extract_failed", "ValueError")
    assert "inbox_id=4" in second["message"]


def firefly_client(handler):
    config = Settings(finance_api_key=ADMIN["X-API-Key"], firefly_base_url="http://firefly.test", firefly_api_token="tok-secret-value-1234", _env_file=None)
    client = FireflyClient(config)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://firefly.test/api/v1/")
    return client


async def test_firefly_client_records_failures_without_the_token(errlog):
    def handler(request):
        if request.url.path.endswith("/gone"):
            return httpx.Response(404, json={"message": "nope"})
        if request.url.path.endswith("/bad"):
            return httpx.Response(422, json={"message": "tok-secret-value-1234 rejected", "errors": {"a": ["x"]}})
        if request.url.path.endswith("/net"):
            raise httpx.ConnectError("connection refused")
        return httpx.Response(503, text="maintenance tok-secret-value-1234")

    client = firefly_client(handler)
    for path, error in (("gone", PermanentError), ("bad", PermanentError), ("net", FireflyError), ("down", FireflyError)):
        with pytest.raises(error):
            await client._request("GET", path)
    rows = pending()
    assert [row["operation"] for row in rows] == ["GET bad", "GET net", "GET down"]  # a 404 is an answer, not a failure
    assert [row["status_code"] for row in rows] == [422, None, 503]
    assert rows[1]["exc_type"] == "ConnectError" and rows[2]["exc_type"] == "FireflyError"
    assert all("tok-secret-value-1234" not in str(row.values()) for row in rows)
    assert "maintenance" in rows[2]["response_body"]
    await client.close()


async def test_llm_client_records_each_failed_model_and_the_exhausted_chain(errlog):
    config = Settings(
        finance_api_key=ADMIN["X-API-Key"], llm_api_key="llm-secret-key-123456", llm_models="m1", llm_base_url="http://llm.test/v1", _env_file=None,
    )
    llm = LLMClient(config)
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(500, text="echo texto del mensaje bancario llm-secret-key-123456"),
    ))
    with pytest.raises(RuntimeError):
        await llm.complete_json("prompt", {"UNTRUSTED_DATA": "texto del mensaje bancario"}, "cycle")
    model, chain = pending()
    assert model["component"] == "llm" and model["status_code"] == 500 and model["operation"] == "chat/completions m1"
    assert chain["operation"] == "chain" and "exhausted" in chain["message"]
    assert "llm-secret-key-123456" not in str(model.values()) and "texto del mensaje" not in str(model.values())
    await llm.close()


async def test_imap_client_records_poll_failures_without_credentials(errlog, monkeypatch):
    account = MailAccount(name="gmail", provider="gmail", username="user@example.com", password="pass12345678")
    client = IMAPClient(account)

    def broken(*args):
        raise RuntimeError("login failed for user@example.com with pass12345678")

    monkeypatch.setattr(client, "_poll", broken)
    with pytest.raises(RuntimeError):
        await client.poll(None, 0, 1, lambda header: False)
    row, = pending()
    assert row["component"] == "mailbox" and row["operation"] == "poll gmail"
    assert "pass12345678" not in row["message"] and "user@example.com" not in row["message"]


async def test_unhandled_route_exception_is_recorded(client, errlog, monkeypatch):
    from app.main import app

    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(reports, "capture_status", broken)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as http:
        response = await http.get(f"{API}/system/capture", headers=ADMIN)
    assert response.status_code == 500 and response.json()["code"] == "internal_error"
    row, = [entry for entry in pending() if entry["operation"] == "GET /api/v1/system/capture"]
    assert (row["component"], row["exc_type"]) == ("api", "RuntimeError")


async def test_scheduler_records_job_failures_and_ticks_the_digest_hourly(monkeypatch, settings, errlog):
    from app.services import scheduler

    monkeypatch.setattr(scheduler, "_get_engine", lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")))

    async def fail():
        raise ValueError("job broke")

    async def ok():
        return {}

    service = scheduler.SchedulerService(fail, ok, ok, ok, ok, ok, ok, ok, settings=settings)
    assert {job.id for job in service.scheduler.get_jobs()} == {
        "cycle", "sender_learning", "reconcile", "digest_tick", "monthly_narrative", "weekly_subscriptions", "leader_election",
    }
    assert set(service.callbacks) >= {"digest", "weekly_digest", "digest_tick"}
    with pytest.raises(ValueError):
        await service._run("cycle")
    row, = pending()
    assert (row["component"], row["operation"], row["exc_type"]) == ("cycle", "job cycle", "ValueError")


async def test_errors_endpoint_filters_pages_and_requires_review(client, db):
    base = now_utc()
    for number, component in enumerate(["firefly", "llm", "firefly", "mailbox"]):
        db.add(ServerError(component=component, operation=f"op{number}", message=f"m{number}", at=base - timedelta(hours=10 - number),
                           exc_type="X", status_code=500 if component == "firefly" else None, response_body="body"))
    await db.commit()
    url = f"{API}/system/errors"
    everything = (await client.get(url, headers=ADMIN)).json()
    assert everything["total"] == 4 and [item["operation"] for item in everything["items"]] == ["op3", "op2", "op1", "op0"]
    assert set(everything["items"][0]) == {"id", "at", "component", "operation", "message", "exc_type", "status_code", "response_body"}
    only = (await client.get(url, headers=ADMIN, params={"component": "firefly"})).json()
    assert only["total"] == 2 and {item["component"] for item in only["items"]} == {"firefly"}
    page = (await client.get(url, headers=ADMIN, params={"limit": 1, "offset": 1})).json()
    assert page["total"] == 4 and [item["operation"] for item in page["items"]] == ["op2"]
    since = (await client.get(url, headers=ADMIN, params={"since": (base - timedelta(hours=8, minutes=30)).isoformat()})).json()
    assert [item["operation"] for item in since["items"]] == ["op3", "op2"] and since["total"] == 2
    assert (await client.get(url, headers=ADMIN, params={"limit": 301})).status_code == 422
    reader = (await enroll_key(client, ["read"]))["key"]
    reviewer = (await enroll_key(client, ["review"]))["key"]
    assert (await client.get(url, headers=auth(reader))).status_code == 403
    assert (await client.get(url, headers=auth(reviewer))).status_code == 200
    assert (await client.get(url, headers=PHONE)).status_code == 403
