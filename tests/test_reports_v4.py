"""Forecast, subscriptions, compare, capture status and backup status (contract sections 4, 6, 11, 14).

All data is synthetic and dated explicitly; the reference "today" is pinned with the ``day``/``date`` argument
or by patching ``app.services.reports.now_utc``.
"""
import itertools
import json
import sys
import types
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.db.models import InboxItem
from app.db.models_devices import Device
from app.services import reports

ADMIN_KEY = "test-admin-key-01234567890123456789"
PHONE_KEY = "test-phone-key-01234567890123456789"
ADMIN = {"X-API-Key": ADMIN_KEY}
BOGOTA = ZoneInfo("America/Bogota")
_refs = itertools.count(1)


def local(month, day, hour=12, minute=0, year=2026):
    return datetime(year, month, day, hour, minute, tzinfo=BOGOTA)


def add(db, when, amount, merchant=None, *, kind="withdrawal", category=None, tags=None, status="created", currency="COP", source="sms", created_at=None):
    db.add(InboxItem(
        source=source, source_ref=f"synthetic-{next(_refs)}", occurred_at=when, status=status, amount=Decimal(amount),
        currency=currency, merchant=merchant, category=category, transaction_type=kind, tags=tags,
        created_at=created_at or when,
    ))


async def get(client, path, headers=ADMIN, **params):
    return await client.get("/api/v1" + path, headers=headers, params=params)


async def test_forecast_without_subscriptions(client, db):
    add(db, local(11, 2), "1000000.00", "EMPRESA", kind="deposit")
    add(db, local(11, 3), "100000.00", "MERCADO", category="Comida")
    add(db, local(11, 10), "50000.00", "TAXI", category="Transporte")
    add(db, local(11, 4), "30000.00", None, kind="transfer")  # transfers never count
    add(db, local(11, 5), "20.00", "STEAM", currency="USD")  # forecast is COP only
    add(db, local(11, 6), "99999.00", "PENDIENTE", status="pending")  # only the created ledger
    add(db, local(10, 31, 23, 30), "77777.00", "OCTUBRE")  # previous month (local)
    await db.commit()

    response = await get(client, "/reports/forecast", date="2026-11-10")
    assert response.status_code == 200
    assert response.json() == {
        "month": "2026-11", "as_of": "2026-11-10", "currency": "COP",
        "income": "1000000.00", "spent": "150000.00", "balance": "850000.00",
        "projected_spent": "450000.00", "projected_balance": "550000.00",
        "days_elapsed": 10, "days_left": 20, "daily_available": "42500.00", "spent_today": "50000.00",
    }


async def test_forecast_with_subscriptions(client, db):
    add(db, local(11, 1), "2000000.00", "EMPRESA", kind="deposit")
    for month in (8, 9, 10):
        add(db, local(month, 20), "15000.00", "Netflix")  # next charge Nov 20, not yet collected
    for month in (8, 9, 10, 11):
        add(db, local(month, 5), "80000.00", "Gimnasio")  # already collected in November
    add(db, local(11, 3), "120000.00", "MERCADO", category="Comida")
    await db.commit()

    body = (await get(client, "/reports/forecast", date="2026-11-10")).json()
    # spent = 80k gym + 120k market; variable = 120k only (gym is recurring); fixed = Netflix 15k.
    assert body["spent"] == "200000.00"
    assert body["projected_spent"] == "455000.00"  # 200k + 15k + (120k / 10) * 20
    assert body["projected_balance"] == "1545000.00"
    assert body["daily_available"] == "89250.00"  # (2,000k - 15k - 200k) / 20
    assert body["spent_today"] == "0.00"


async def test_forecast_tag_filter(client, db):
    add(db, local(11, 2), "100000.00", "VIAJE", tags=["Trip"])
    add(db, local(11, 3), "40000.00", "MERCADO", tags=["casa"])
    await db.commit()

    body = (await get(client, "/reports/forecast", date="2026-11-10", tag="trip")).json()
    assert body["spent"] == "100000.00"


async def test_subscriptions_detect_new_and_price_increase(client, db):
    for month, amount in ((8, "10000.00"), (9, "10000.00"), (10, "10000.00"), (11, "11000.00")):
        add(db, local(month, 15), amount, "Spotify", category="Ocio", tags=["personal"])
    add(db, local(10, 20), "20000.00", "Disney")
    add(db, local(11, 19), "20000.00", "Disney")
    for day in (2, 9, 16):
        add(db, local(11, day), "5000.00", "Bus")
    add(db, local(11, 1), "9000.00", "Irregular")  # 10-day gap: not a known cadence
    add(db, local(11, 11), "9000.00", "Irregular")
    add(db, local(9, 3), "10000.00", "Variable")  # monthly but the earlier prices swing over 20 %
    add(db, local(10, 3), "20000.00", "Variable")
    add(db, local(11, 3), "20000.00", "Variable")
    add(db, local(11, 8), "7000.00", "Una vez")
    await db.commit()

    items = (await get(client, "/reports/subscriptions", date="2026-11-20")).json()["items"]
    assert [item["merchant"] for item in items] == ["Bus", "Spotify", "Disney"]  # ordered by next_date
    bus, spotify, disney = items
    assert spotify == {
        "merchant": "Spotify", "category": "Ocio", "currency": "COP", "amount": "11000.00", "previous_amount": "10000.00",
        "change_pct": "10.00", "period_days": 31, "occurrences": 4, "last_date": "2026-11-15", "next_date": "2026-12-15",
        "status": "price_increase",
    }
    assert (disney["status"], disney["occurrences"], disney["next_date"], disney["change_pct"]) == ("new", 2, "2026-12-19", "0.00")
    assert (bus["status"], bus["period_days"], bus["next_date"]) == ("new", 7, "2026-11-23")

    filtered = (await get(client, "/reports/subscriptions", date="2026-11-20", tag="personal")).json()["items"]
    assert [item["merchant"] for item in filtered] == ["Spotify"]


async def test_old_stable_subscription_is_active(client, db):
    for month in (6, 7, 8, 9):
        add(db, local(month, 10), "30000.00", "Nube")
    await db.commit()

    items = (await get(client, "/reports/subscriptions", date="2026-10-12")).json()["items"]
    assert [(item["merchant"], item["status"]) for item in items] == [("Nube", "active")]
    # A cancelled subscription (over a full cycle overdue) drops out.
    assert (await get(client, "/reports/subscriptions", date="2027-03-01")).json() == {"items": []}


async def test_compare_by_category(client, db):
    add(db, local(10, 10), "60000.00", "A", category="Comida")
    add(db, local(10, 31, 23, 30), "40000.00", "B", category="Comida")  # Oct 31 local, Nov 1 UTC
    add(db, local(10, 12), "40000.00", "C", category="Transporte")
    add(db, local(10, 15), "10000.00", "D")
    add(db, local(11, 3), "150000.00", "E", category="Comida")
    add(db, local(11, 4), "20000.00", "F", category="Transporte")
    add(db, local(11, 5), "30000.00", "G", category="Ocio")
    add(db, local(11, 6), "500000.00", "H", kind="deposit", category="Salario")  # income is not spending
    await db.commit()

    response = await get(client, "/reports/compare", a_from="2026-10-01", a_to="2026-10-31", b_from="2026-11-01", b_to="2026-11-30")
    assert response.status_code == 200
    assert response.json() == {
        "a": {"from": "2026-10-01", "to": "2026-10-31", "total": "150000.00"},
        "b": {"from": "2026-11-01", "to": "2026-11-30", "total": "200000.00"},
        "categories": [
            {"category": "Comida", "a": "100000.00", "b": "150000.00", "diff": "50000.00", "diff_pct": "50.00"},
            {"category": "Ocio", "a": "0.00", "b": "30000.00", "diff": "30000.00", "diff_pct": None},
            {"category": "Transporte", "a": "40000.00", "b": "20000.00", "diff": "-20000.00", "diff_pct": "-50.00"},
            {"category": "Sin categoría", "a": "10000.00", "b": "0.00", "diff": "-10000.00", "diff_pct": "-100.00"},
        ],
    }


async def test_compare_rejects_inverted_range(client):
    response = await get(client, "/reports/compare", a_from="2026-10-31", a_to="2026-10-01", b_from="2026-11-01", b_to="2026-11-30")
    assert response.status_code == 422


async def test_alert_check_emits_stable_dedupe_keys(db, settings, monkeypatch):
    for month, amount in ((8, "10000.00"), (9, "10000.00"), (10, "10000.00"), (11, "11000.00")):
        add(db, local(month, 15), amount, "Spotify")
    add(db, local(10, 20), "20000.00", "Disney")
    add(db, local(11, 19), "20000.00", "Disney")
    for month in (6, 7, 8, 9, 10, 11):
        add(db, local(month, 2), "30000.00", "Nube")  # active: no alert
    await db.commit()
    monkeypatch.setattr(reports, "now_utc", lambda: datetime(2026, 11, 20, 17, 0, tzinfo=UTC))
    calls = []

    async def emit(session, type, **kwargs):
        calls.append((type, kwargs))
        return object()

    monkeypatch.setitem(sys.modules, "app.services.alerts", types.SimpleNamespace(emit=emit, ONCE_MINUTES=525600))

    assert await reports.alert_check(db, settings, None) == 2
    first = {(kind, kw["dedupe_key"]) for kind, kw in calls}
    assert first == {
        ("price_increase", "price_increase:spotify:10000.00:11000.00"),
        ("new_subscription", "new_subscription:disney:20000.00"),
    }
    calls.clear()
    await reports.alert_check(db, settings, None)
    assert {(kind, kw["dedupe_key"]) for kind, kw in calls} == first
    price = next(kw for kind, kw in calls if kind == "price_increase")
    assert price["amount"] == Decimal("11000.00") and price["currency"] == "COP"
    assert "11.000,00 COP" in price["body"] and "raw" not in price["body"].lower()


def write_status(path, **fields):
    path.write_text(json.dumps(fields), encoding="utf-8")


def iso(delta):
    return (datetime.now(UTC) - delta).isoformat()


async def test_backup_status_ok(client, settings, tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    monkeypatch.setattr(settings, "backup_status_file", str(path))
    ran, succeeded = iso(timedelta(hours=1)), iso(timedelta(hours=1))
    write_status(path, status="ok", last_run_at=ran, last_success_at=succeeded, detail="12 MB")

    response = await get(client, "/backup/status")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["detail"] == "12 MB"
    assert datetime.fromisoformat(body["last_success_at"]) == datetime.fromisoformat(succeeded)
    assert set(body) == {"status", "last_run_at", "last_success_at", "detail"}


async def test_backup_status_failed(client, settings, tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    monkeypatch.setattr(settings, "backup_status_file", str(path))
    write_status(path, status="failed", last_run_at=iso(timedelta(hours=1)), last_success_at=iso(timedelta(hours=26)), detail="pg_dump falló")

    body = (await get(client, "/backup/status")).json()
    assert body["status"] == "failed" and body["detail"] == "pg_dump falló"


async def test_backup_status_old_success_is_stale(client, settings, tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    monkeypatch.setattr(settings, "backup_status_file", str(path))
    write_status(path, status="ok", last_run_at=iso(timedelta(days=3)), last_success_at=iso(timedelta(days=3)), detail=None)

    body = (await get(client, "/backup/status")).json()
    assert body["status"] == "stale"
    assert body["last_success_at"] is not None


async def test_backup_status_missing_or_unreadable_is_unknown(client, settings, tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    monkeypatch.setattr(settings, "backup_status_file", str(path))
    body = (await get(client, "/backup/status")).json()
    assert body["status"] == "unknown" and body["last_run_at"] is None and body["last_success_at"] is None

    for content in ("{not json", "[]", '{"status": ["ok"]}', '{"status": "weird"}'):
        path.write_text(content, encoding="utf-8")
        assert (await get(client, "/backup/status")).json()["status"] == "unknown"
    path.write_bytes(b"\xff\xfe\x00")
    assert (await get(client, "/backup/status")).json()["status"] == "unknown"


async def test_capture_status_recent_phone(client, db, monkeypatch):
    monkeypatch.setattr(reports, "now_utc", lambda: datetime(2026, 11, 20, 12, 0, tzinfo=UTC))
    add(db, local(11, 20), "1.00", "A", source="sms", created_at=datetime(2026, 11, 20, 10, 0, tzinfo=UTC))
    add(db, local(11, 18), "1.00", "B", source="notification", created_at=datetime(2026, 11, 18, 9, 0, tzinfo=UTC))
    add(db, local(11, 20), "1.00", "C", source="email", created_at=datetime(2026, 11, 20, 11, 0, tzinfo=UTC))
    for status in ("pending", "pending", "needs_review", "failed", "created"):
        add(db, local(11, 19), "1.00", "Q", status=status, source="manual", created_at=datetime(2026, 11, 1, tzinfo=UTC))
    db.add(Device(name="Teléfono", key_hash="a" * 64, scopes=["ingest"], last_used_at=datetime(2026, 11, 20, 9, 0, tzinfo=UTC)))
    db.add(Device(name="Revocado", key_hash="b" * 64, scopes=["read"], revoked_at=datetime(2026, 11, 1, tzinfo=UTC)))
    await db.commit()

    response = await get(client, "/system/capture")
    assert response.status_code == 200
    body = response.json()
    assert body["silent"] is False and body["phone_silence_hours"] == 24
    assert datetime.fromisoformat(body["last_event_at"]) == datetime(2026, 11, 20, 11, 0, tzinfo=UTC)
    assert {key: datetime.fromisoformat(value) for key, value in body["by_source"].items()} == {
        "sms": datetime(2026, 11, 20, 10, 0, tzinfo=UTC),
        "notification": datetime(2026, 11, 18, 9, 0, tzinfo=UTC),
        "email": datetime(2026, 11, 20, 11, 0, tzinfo=UTC),
    }
    assert body["queue"] == {"pending": 2, "needs_review": 1, "failed": 1}
    names = {device["name"] for device in body["devices"]}
    assert "Teléfono" in names and "Revocado" not in names
    assert all(set(device) == {"id", "name", "scopes", "last_used_at"} for device in body["devices"])
    assert "key_hash" not in response.text


async def test_capture_status_phone_silence(client, db, monkeypatch):
    monkeypatch.setattr(reports, "now_utc", lambda: datetime(2026, 11, 20, 12, 0, tzinfo=UTC))
    add(db, local(11, 18), "1.00", "A", source="sms", created_at=datetime(2026, 11, 18, 12, 0, tzinfo=UTC))
    add(db, local(11, 19), "1.00", "B", source="notification", created_at=datetime(2026, 11, 19, 8, 0, tzinfo=UTC))  # 28 h ago
    add(db, local(11, 20), "1.00", "C", source="email", created_at=datetime(2026, 11, 20, 11, 0, tzinfo=UTC))  # not the phone
    await db.commit()

    body = (await get(client, "/system/capture")).json()
    assert body["silent"] is True
    assert datetime.fromisoformat(body["last_event_at"]) == datetime(2026, 11, 20, 11, 0, tzinfo=UTC)


async def test_capture_status_empty(client):
    body = (await get(client, "/system/capture")).json()
    assert body["last_event_at"] is None and body["silent"] is False
    assert body["by_source"] == {"sms": None, "notification": None, "email": None}
    assert body["queue"] == {"pending": 0, "needs_review": 0, "failed": 0}


async def test_report_endpoints_require_read_scope(client):
    paths = [
        "/reports/forecast", "/reports/subscriptions", "/system/capture", "/backup/status",
        "/reports/compare?a_from=2026-10-01&a_to=2026-10-31&b_from=2026-11-01&b_to=2026-11-30",
    ]
    for path in paths:
        assert (await client.get("/api/v1" + path, headers={"X-API-Key": PHONE_KEY})).status_code == 403, path
        assert (await client.get("/api/v1" + path)).status_code == 401, path
        assert (await client.get("/api/v1" + path, headers=ADMIN)).status_code == 200, path


async def test_jump_above_twenty_percent_on_the_last_charge_is_price_increase(client, db):
    for month in (8, 9, 10):
        add(db, local(month, 15), "10000.00", "Streaming")
    add(db, local(11, 15), "13000.00", "Streaming")
    await db.commit()

    item, = (await get(client, "/reports/subscriptions", date="2026-11-20")).json()["items"]
    assert item["merchant"] == "Streaming" and item["status"] == "price_increase"
    assert (item["previous_amount"], item["amount"], item["change_pct"]) == ("10000.00", "13000.00", "30.00")


async def test_alert_check_emits_price_increase_for_jump_above_twenty_percent(db, settings, monkeypatch):
    for month in (8, 9, 10):
        add(db, local(month, 15), "10000.00", "Streaming")
    add(db, local(11, 15), "13000.00", "Streaming")
    await db.commit()
    monkeypatch.setattr(reports, "now_utc", lambda: datetime(2026, 11, 20, 17, 0, tzinfo=UTC))
    calls = []

    async def emit(session, type, **kwargs):
        calls.append((type, kwargs["dedupe_key"]))
        return object()

    monkeypatch.setitem(sys.modules, "app.services.alerts", types.SimpleNamespace(emit=emit, ONCE_MINUTES=525600))
    assert await reports.alert_check(db, settings, None) == 1
    assert calls == [("price_increase", "price_increase:streaming:10000.00:13000.00")]
