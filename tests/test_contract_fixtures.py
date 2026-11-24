"""Contract conformance between the real analyzer and the app's Dart models (offline).

The test seeds realistic synthetic data (inbox items in every state, a rule, merchants, alerts, an assistant batch with
applied proposals, an audit trail, a transfer pair and an anomaly) over the simulated Firefly of ``conftest.py`` and
calls EVERY endpoint behind a method of ``app/lib/api/analyzer/analyzer_api.dart`` with a ``read``+``review`` device
enrolled through ``/devices/enroll``. Each real JSON body is stored as ``app/test/fixtures/contract/<method>.json`` (the
method name in snake_case); error bodies are stored as ``{"status": ..., "body": {...}}`` in ``error_<status>_<code>.json``.
``app/test/api/contract_fixtures_test.dart`` parses those files with the Dart models.

* ``UPDATE_CONTRACT_FIXTURES=1`` rewrites the files.
* Otherwise the bodies are compared with the files and any difference fails: the contract cannot drift unnoticed.

Only values that change on every run are normalized: server-stamped instants close to the wall clock (``created_at``,
``at``, ``applied_at``...) become ``FROZEN``, ``revert_deadline`` becomes ``FROZEN`` + 7 days and the enrollment key a
fixed placeholder. Seeded data uses absolute dates, so everything else is deterministic.
"""
import asyncio
import difflib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.routers import history, system, widgets
from app.api.routers import inbox as inbox_router
from app.core.config import MailAccount
from app.db.models import MailAccountState
from app.db.models_rules import Merchant, Rule
from app.db.repositories import KnownSenderRepository
from app.services import alerts as alerting
from tests import (
    contract_agent_memory,
    contract_capture,
    contract_quality,
    contract_snapshots_review,
    contract_statements_budgets,
)
from tests.test_ledger_v4 import FireflyLedger

ADMIN = {"X-API-Key": "test-admin-key-01234567890123456789"}
API = "/api/v1"
ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "app" / "test" / "fixtures" / "contract"
DART_API = ROOT / "app" / "lib" / "api" / "analyzer" / "analyzer_api.dart"
UPDATE = os.environ.get("UPDATE_CONTRACT_FIXTURES") == "1"

FROZEN = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)  # stand-in for every volatile server stamp
REVERT_WINDOW = timedelta(days=7)
REPORT_NOW = datetime(2026, 11, 6, 17, 0, tzinfo=UTC)  # "now" of the time-dependent endpoints (12:00 in Bogota)
NEAR_NOW = timedelta(days=3)
ISO_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
DECIMAL_TEXT = re.compile(r"^-?\d+\.\d{2}$")
MONEY_KEYS = frozenset({
    "amount", "previous_amount", "income", "spent", "balance", "projected_spent", "projected_balance",
    "daily_available", "spent_today", "expenses", "limit", "available", "diff", "max_amount_cop",
})
MONEY_MAPS = frozenset({"totals", "by_category", "by_account"})


# --- Normalization and storage ---------------------------------------------------------------------------------------


def _normalize(value, started, key=None):
    if isinstance(value, dict):
        return {name: _normalize(item, started, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item, started, key) for item in value]
    if not isinstance(value, str):
        return value
    if key == "key" and value.startswith("cofre_"):
        return "cofre_" + "x" * 43
    if key == "group_id":
        return "0" * 32
    if ISO_INSTANT.match(value):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return value
        if moment.tzinfo is not None:
            if key in ("revert_deadline", "undo_deadline"):
                return (FROZEN + REVERT_WINDOW).isoformat()
            if abs(moment - started) <= NEAR_NOW:
                return FROZEN.isoformat()
    return value


# Objects whose keys are column names mapped to positions (budget import `columns`): not money.
NON_MONEY_OBJECTS = frozenset({"columns"})


def _assert_money_strings(name, value, key=None):
    """Money crosses the API as a two-decimal string, never as a JSON number (contract section 14)."""
    if isinstance(value, dict):
        for field, item in value.items():
            if field in NON_MONEY_OBJECTS:
                continue
            if field in MONEY_MAPS and isinstance(item, dict):
                for label, amount in item.items():
                    assert isinstance(amount, str) and DECIMAL_TEXT.match(amount), f"{name}: {field}[{label}] = {amount!r}"
            _assert_money_strings(name, item, field)
    elif isinstance(value, list):
        for item in value:
            _assert_money_strings(name, item, key)
    elif key in MONEY_KEYS and value is not None:
        assert isinstance(value, str) and DECIMAL_TEXT.match(value), f"{name}: {key} = {value!r}"


def _dump(body) -> str:
    return json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _api_fixture_names() -> set[str]:
    """The ``AnalyzerApi`` methods, in snake_case: each one needs a fixture."""
    source = DART_API.read_text(encoding="utf-8")
    names = re.findall(r"^  Future<[^\n;]*?>\s+(\w+)\(", source, re.M)
    assert len(names) > 30, f"Could not read the AnalyzerApi methods from {DART_API}"
    return {re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower() for name in names}


def _finish(bodies: dict) -> None:
    missing = sorted(_api_fixture_names() - bodies.keys())
    assert not missing, f"AnalyzerApi methods without a contract fixture: {missing}"
    texts = {name: _dump(body) for name, body in bodies.items()}
    if UPDATE:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        for old in FIXTURE_DIR.glob("*.json"):
            if old.stem not in texts:
                old.unlink()
        for name, text in texts.items():
            (FIXTURE_DIR / f"{name}.json").write_bytes(text.encode("utf-8"))
        return
    on_disk = {path.stem: path for path in FIXTURE_DIR.glob("*.json")} if FIXTURE_DIR.is_dir() else {}
    problems = [f"missing file: {name}.json" for name in sorted(texts.keys() - on_disk.keys())]
    problems += [f"stale file no endpoint writes: {name}.json" for name in sorted(on_disk.keys() - texts.keys())]
    for name in sorted(texts.keys() & on_disk.keys()):
        stored = json.loads(on_disk[name].read_text(encoding="utf-8"))
        if stored != json.loads(texts[name]):
            diff = list(difflib.unified_diff(_dump(stored).splitlines(), texts[name].splitlines(), "stored", "current", lineterm="", n=1))
            problems.append(f"{name}.json differs:\n" + "\n".join(diff[:30]))
    assert not problems, (
        "The analyzer contract drifted from app/test/fixtures/contract. If the change is intended, rerun with "
        "UPDATE_CONTRACT_FIXTURES=1 and review the diff together with the Dart models.\n" + "\n".join(problems)
    )


class Recorder:
    """Calls the API in-process and keeps every normalized body under its fixture name."""

    def __init__(self, client, started):
        self.client, self.started, self.bodies = client, started, {}

    def store(self, name, body):
        assert name not in self.bodies, f"fixture recorded twice: {name}"
        normalized = _normalize(body, self.started)
        if not name.startswith("error_") and name != "list_audit":
            _assert_money_strings(name, normalized)
        self.bodies[name] = normalized

    async def call(self, name, method, path, headers, status=200, **kwargs):
        response = await self.client.request(method, API + path, headers=headers, **kwargs)
        assert response.status_code == status, f"{method} {path}: {response.status_code} {response.text}"
        body = response.json()
        if name:
            self.store(name, body)
        return body

    async def error(self, name, method, path, headers, status, code, **kwargs):
        response = await self.client.request(method, API + path, headers=headers, **kwargs)
        assert response.status_code == status, f"{method} {path}: {response.status_code} {response.text}"
        body = response.json()
        assert body["code"] == code and isinstance(body["detail"], str), body
        self.store(name, {"status": status, "body": body})
        return body


# --- Synthetic data --------------------------------------------------------------------------------------------------


def at(month, day, hour=15, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


async def make(service, ref, occurred_at, **changes):
    values = dict(
        source="sms", source_ref=ref, sender="85954", subject_or_title="", raw_text=None, occurred_at=occurred_at,
        status="created", method="parser", transaction_type="withdrawal", amount=Decimal("12000.00"), currency="COP",
        merchant="SUPERMERCADO CENTRAL", asset_account="Nequi",
    )
    values.update(changes)
    values.setdefault("original_kind", values["transaction_type"])
    values.setdefault("template_id", "nequi.pagaste" if values["method"] == "parser" else None)
    values.setdefault("firefly_transaction_id", f"ff-{ref}" if values["status"] == "created" else None)
    row = await service.inbox.add(**values)
    await service.session.commit()
    return row


NO_AMOUNT = dict(amount=None, currency=None, merchant=None, asset_account=None, transaction_type=None)


async def seed_items(service):
    ids = SimpleNamespace()
    # Supermarket history in October (the base of the unusual-amount anomaly), then one unusually large purchase.
    for number, (day, amount) in enumerate(((1, "11000.00"), (4, "12500.00"), (8, "13000.00"), (12, "11800.00"), (17, "12200.00"), (22, "12900.00"))):
        row = await make(service, f"hist-{number}", at(10, day), amount=Decimal(amount), category="Alimentación")
        if number == 0:
            ids.history_first = row
    ids.big = await make(
        service, "super-big", at(11, 3), amount=Decimal("98000.00"), category="Alimentación", subject_or_title="Compra",
        raw_text="Bancolombia: Hola Maria Lopez, compraste $98.000,00 en SUPERMERCADO CENTRAL con tu cuenta 0550012345678 el 03/11/2026.",
    )
    # Subscriptions: Netflix went up in November, Spotify is steady.
    for month, amount in ((8, "38900.00"), (9, "38900.00"), (10, "38900.00"), (11, "44900.00")):
        ids.netflix_last = await make(service, f"netflix-{month}", at(month, 5), amount=Decimal(amount), merchant="NETFLIX", category="Entretenimiento")
    for month in (8, 9, 10):
        await make(service, f"spotify-{month}", at(month, 12), amount=Decimal("16900.00"), merchant="SPOTIFY", category="Entretenimiento")
    # The same charge reported by two sources four minutes apart.
    ids.d1_first = await make(service, "d1-sms", at(11, 4, 15, 0), amount=Decimal("45000.00"), merchant="TIENDA D1")
    ids.d1_second = await make(
        service, "d1-notif", at(11, 4, 15, 4), amount=Decimal("45000.00"), merchant="TIENDA D1", source="notification",
        app="com.nequi.MobileApp", sender="Nequi",
    )
    ids.salary = await make(
        service, "salary", at(11, 1), transaction_type="deposit", amount=Decimal("3500000.00"), merchant="EMPRESA DEMO SAS",
        category="Ingresos", source="email", sender="nomina@empresa-demo.co", subject_or_title="Pago de nómina",
        account_name="primary", message_id="<nomina-2026-11@mail.example>",
    )
    # A transfer between own accounts announced by two banks.
    ids.out = await make(service, "tr-out", at(11, 5, 16, 0), amount=Decimal("50000.00"), merchant="TRANSFERENCIA PROPIA")
    ids.incoming = await make(
        service, "tr-in", at(11, 5, 16, 5), source="email", sender="alertas@bancolombia.com.co", transaction_type="deposit",
        amount=Decimal("50000.00"), merchant="TRANSFERENCIA PROPIA", asset_account="Bancolombia",
    )
    ids.cinema = await make(service, "cinema", at(11, 2, 23, 0), amount=Decimal("24000.00"), merchant="CINEMA PARADISO", category="Entretenimiento")
    ids.manual = await make(
        service, "manual:seed-almuerzo", at(11, 6, 18, 0), source="manual", sender="owner", method="manual", amount=Decimal("54000.00"),
        merchant="RESTAURANTE LA 85", category="Alimentación", subject_or_title="Almuerzo del equipo", raw_text="Almuerzo del equipo",
        description="Almuerzo del equipo", tags=["trabajo"],
    )
    # Waiting for review.
    ids.rappi = await make(
        service, "nr-rappi", at(11, 6, 14, 0), status="needs_review", amount=Decimal("32000.00"), merchant="RAPPI COLOMBIA",
        review_reason="SMS extraction requires human review", subject_or_title="Compra",
        raw_text="Bancolombia: Hola Maria Lopez, compraste $32.000 en RAPPI COLOMBIA con tu cuenta 0550012345678 el 06/11/2026.",
    )
    ids.tienda = await make(
        service, "nr-tienda", at(11, 6, 13, 30), status="needs_review", amount=Decimal("18500.00"), merchant="TIENDA DEL BARRIO",
        review_reason="SMS extraction requires human review", raw_text="Nequi: pagaste $18.500 en TIENDA DEL BARRIO.",
    )
    ids.alkosto = await make(
        service, "nr-alkosto", at(11, 5, 19, 30), status="needs_review", amount=Decimal("2500000.00"), merchant="ALKOSTO ELECTRODOMESTICOS",
        review_reason="amount absent, invalid, or over review threshold", raw_text="Nequi: pagaste $2.500.000 en ALKOSTO ELECTRODOMESTICOS.",
    )
    ids.unknown = await make(
        service, "nr-unknown", at(11, 6, 12, 0), status="needs_review", amount=Decimal("76000.00"), merchant="COMERCIO DESCONOCIDO",
        review_reason="SMS extraction requires human review", raw_text="Nequi: pagaste $76.000 en COMERCIO DESCONOCIDO.",
    )
    ids.email = await make(
        service, "nr-email", at(11, 5, 21, 0), source="email", status="needs_review", sender="alertas@nequi.com.co", account_name="primary",
        message_id="<aviso-77@mail.example>", subject_or_title="Aviso de compra", amount=Decimal("64900.00"), merchant="AMAZON PRIME",
        review_reason="sender_auth_failed", raw_text="Se realizó una compra por $64.900 en AMAZON PRIME. Titular: Juan Perez Gomez.",
    )
    ids.unparsed = await make(
        service, "nr-unparsed", at(11, 6, 15, 0), status="needs_review", method="llm", template_id="llm:test", **NO_AMOUNT,
        review_reason="LLM extraction requires human review", raw_text="Movimiento pendiente de verificación en tu cuenta terminada en 1234.",
    )
    # Every other state.
    ids.pending = await make(service, "pending-1", at(11, 6, 16, 45), status="pending", method=None, **NO_AMOUNT, raw_text="Pagaste $15.000 a CAFE LUNA")
    ids.failed = await make(
        service, "failed-1", at(11, 5, 20, 0), status="failed", method=None, **NO_AMOUNT, attempts=3,
        last_error="Upstream processing failed; retry safely by external_id", raw_text="Compraste $9.900 en CAFE LUNA",
    )
    ids.rejected = await make(
        service, "rejected-1", at(11, 6, 13, 0), status="rejected", amount=Decimal("12000.00"), review_reason="SMS extraction requires human review",
        raw_text="Nequi: pagaste $12.000 en SUPERMERCADO CENTRAL.",
    )
    ids.voided = await make(service, "voided-1", at(11, 3, 14, 0), status="voided", amount=Decimal("15000.00"), merchant="PARQUEADERO CENTRO", firefly_transaction_id="ff-voided-1")
    ids.duplicate = await make(
        service, "dup-1", at(11, 4, 15, 6), status="duplicate", source="notification", app="com.nequi.MobileApp", sender="Nequi",
        amount=Decimal("45000.00"), merchant="TIENDA D1", duplicate_of_id=ids.d1_first.id,
    )
    ids.ignored = await make(
        service, "ignored-1", at(11, 4, 9, 0), status="ignored", method="sensitive_code", template_id="ignore.sensitive_code",
        review_reason="sensitive_code", **NO_AMOUNT,
    )
    return ids


async def seed_catalog(db):
    db.add_all([
        Merchant(name="SUPERMERCADO CENTRAL", default_category="Alimentación", count=7, last_seen_at=at(10, 22)),
        Merchant(name="NETFLIX", default_category="Entretenimiento", count=4, last_seen_at=at(11, 5)),
        Merchant(name="TIENDA D1", default_category=None, count=2, last_seen_at=at(11, 4, 15, 4)),
        Rule(match="merchant_exact", pattern="SUPERMERCADO CENTRAL", category="Alimentación", merchant="SUPERMERCADO CENTRAL", source="learned", hits=7),
    ])
    await db.commit()


async def seed_alerts(db, ids):
    async def emit(kind, **fields):
        alert = await alerting.emit(
            db, kind, min_interval_minutes=alerting.ONCE_MINUTES, dedupe_key=f"contract:{kind}:{fields.get('item_id')}", **fields,
        )
        assert alert is not None
        return alert

    question = "¿Este cobro de RAPPI COLOMBIA por $32.000 es Alimentación?"
    await emit(
        "pending_review", title="Movimiento por revisar", item_id=ids.rappi.id, amount=ids.rappi.amount, currency="COP",
        body="Gasto de $32.000 COP en RAPPI COLOMBIA necesita tu confirmación antes de registrarse en Firefly.",
        suggested_categories=["Alimentación", "Transporte"],
    )
    await emit(
        "question", title="Una pregunta sobre un movimiento", body=question, question=question, options=["Alimentación", "Transporte"],
        item_id=ids.rappi.id, amount=ids.rappi.amount, currency="COP",
    )
    await emit(
        "big_amount", title="Movimiento de monto alto", item_id=ids.alkosto.id, amount=ids.alkosto.amount, currency="COP",
        body="Gasto de $2.500.000 COP en ALKOSTO ELECTRODOMESTICOS supera el límite de revisión de $2.000.000 COP.",
    )
    await emit(
        "price_increase", title="Subió el precio de NETFLIX", item_id=ids.netflix_last.id, amount=ids.netflix_last.amount, currency="COP",
        body="NETFLIX pasó de 38.900,00 COP a 44.900,00 COP (15,42 %).",
    )
    await emit(
        "system_failure", title="Falló el registro de un movimiento", item_id=ids.failed.id,
        body="Movimiento sin monto interpretado desde un SMS no pudo registrarse en Firefly; requiere atención.",
    )
    acked = await emit("digest_daily", title="Resumen del 05/11/2026", body="Gastos $360.900 COP (periodo anterior $129.200 COP), ingresos $3.550.000 COP.")
    acked.acked_at = at(11, 6, 13, 0)
    await db.commit()


# --- The test --------------------------------------------------------------------------------------------------------


@pytest.mark.skipif(not DART_API.is_file(), reason="needs the finan monorepo: app/ is absent in the standalone analyzer repo")
async def test_every_analyzer_endpoint_matches_its_contract_fixture(client, service, db, clients, settings, monkeypatch, tmp_path):
    started = datetime.now(UTC)
    firefly, llm = clients
    rec = Recorder(client, started)
    monkeypatch.setattr(alerting, "get_settings", lambda: settings)
    # Firefly's insight endpoint answers with a JSON array of per-category groups.
    firefly.insight.return_value = [
        {"id": "3", "name": "Alimentación", "difference": "-152000.00", "difference_float": -152000.0, "currency_id": "1", "currency_code": "COP"},
    ]
    firefly.budget_progress.return_value = [
        {"name": "Mercado", "spent": Decimal("90000"), "limit": Decimal("100000")},
        {"name": "Ocio", "spent": Decimal("10000"), "limit": Decimal("100000")},
        {"name": "Transporte", "spent": Decimal("60000"), "limit": Decimal("100000")},
        {"name": "Salud", "spent": Decimal("30000"), "limit": Decimal("100000")},
    ]
    backup_file = tmp_path / "backup-status.json"
    stamp = (started - timedelta(hours=3)).isoformat()
    backup_file.write_text(json.dumps({"status": "ok", "last_run_at": stamp, "last_success_at": stamp, "detail": "Copia completa de la base de datos"}), encoding="utf-8")
    settings.backup_status_file = str(backup_file)
    settings.mail_accounts = [MailAccount(name="primary", provider="gmail", username="demo@gmail.com", password="demo-password")]
    db.add(MailAccountState(name="primary", last_poll_at=started - timedelta(minutes=5), consecutive_failures=0))
    await db.commit()

    # -- Devices: a read+review device (the app) and a read-only one (for the 403) -------------------------------------
    code = (await rec.call(None, "POST", "/devices/enroll-token", ADMIN, json={"scopes": ["read", "review"]}))["code"]
    enrolled = await rec.call("enroll", "POST", "/devices/enroll", {}, status=201, json={"code": code, "name": "Pixel de prueba", "scopes": ["read", "review"]})
    reviewer = {"X-API-Key": enrolled["key"]}
    code = (await rec.call(None, "POST", "/devices/enroll-token", ADMIN, json={"scopes": ["read"]}))["code"]
    viewer_device = await rec.call(None, "POST", "/devices/enroll", {}, status=201, json={"code": code, "name": "Tablet solo lectura", "scopes": ["read"]})
    viewer = {"X-API-Key": viewer_device["key"]}
    await rec.error("error_401_invalid_code", "POST", "/devices/enroll", {}, 401, "invalid_code", json={"code": "codigo-invalido", "name": "x", "scopes": ["read"]})
    await rec.call("auth_check", "GET", "/auth/check", reviewer)

    # -- Seed ------------------------------------------------------------------------------------------------------------
    ids = await seed_items(service)
    await seed_catalog(db)
    await seed_alerts(db, ids)

    # -- Inbox reads -----------------------------------------------------------------------------------------------------
    inbox = await rec.call("list_inbox", "GET", "/inbox", reviewer, params={"limit": 50, "offset": 0})
    assert {item["status"] for item in inbox["items"]} >= {"created", "needs_review", "pending", "failed", "rejected", "voided", "duplicate", "ignored"}
    unparsed = await rec.call("list_unparsed", "GET", "/inbox/unparsed", reviewer, params={"limit": 50, "offset": 0})
    assert unparsed["items"]
    await rec.call("get_inbox_item", "GET", f"/inbox/{ids.rappi.id}", reviewer)
    await rec.call("get_inbox_item_raw", "GET", f"/inbox/{ids.rappi.id}", reviewer, params={"include_raw": "true"})
    suggestions = await rec.call("get_suggestions", "GET", f"/inbox/{ids.rappi.id}/suggestions", reviewer)
    assert suggestions["categories"]

    # -- Reports ---------------------------------------------------------------------------------------------------------
    await rec.call("report_summary", "GET", "/reports/summary", reviewer, params={"period": "month", "date": "2026-11-06"})
    await rec.call("forecast", "GET", "/reports/forecast", reviewer, params={"date": "2026-11-06"})
    await rec.call("subscriptions", "GET", "/reports/subscriptions", reviewer, params={"date": "2026-11-06"})
    await rec.call("compare", "GET", "/reports/compare", reviewer, params={"a_from": "2026-10-01", "a_to": "2026-10-31", "b_from": "2026-11-01", "b_to": "2026-11-06"})

    # -- Alerts and system state ---------------------------------------------------------------------------------------
    alerts = await rec.call("list_alerts", "GET", "/alerts", reviewer, params={"after_id": 0, "limit": 50, "include_acked": "true"})
    assert alerts["items"]
    with monkeypatch.context() as patch:
        patch.setattr(system, "get_firefly", lambda: firefly)
        patch.setattr(system, "get_llm", lambda: llm)
        await rec.call("health", "GET", "/health", reviewer)
    await rec.call("capture_status", "GET", "/system/capture", reviewer)
    await rec.call("backup_status", "GET", "/backup/status", reviewer)

    # -- Catalog, transfers, anomalies, widgets, flags -------------------------------------------------------------------
    merchants = await rec.call("list_merchants", "GET", "/merchants", reviewer, params={"limit": 50, "offset": 0})
    pending = await rec.call("pending_transfers", "GET", "/transfers/pending", reviewer)
    assert pending["items"]
    with monkeypatch.context() as patch:
        patch.setattr("app.services.reports.now_utc", lambda: REPORT_NOW)
        anomalies = await rec.call("list_anomalies", "GET", "/anomalies", reviewer, params={"since": "2026-11-01T00:00:00+00:00"})
    assert {entry["kind"] for entry in anomalies["items"]} >= {"unusual_amount", "duplicate_charge"}
    with monkeypatch.context() as patch:
        patch.setattr(widgets, "get_firefly", lambda: firefly)
        patch.setattr(widgets, "now_utc", lambda: REPORT_NOW)
        patch.setattr("app.services.reports.now_utc", lambda: REPORT_NOW)
        widget = await rec.call("widget_summary", "GET", "/widgets/summary", reviewer)
    assert widget["budgets"] and widget["recent"]
    await rec.call("get_flags_default", "GET", "/flags", reviewer)

    # -- Rules and merchants ---------------------------------------------------------------------------------------------
    rule = await rec.call("create_rule", "POST", "/rules", reviewer, status=201, json={"match": "text_contains", "pattern": "uber", "category": "Transporte"})
    rules = await rec.call("list_rules", "GET", "/rules", reviewer)
    assert len(rules["items"]) >= 2
    firefly.create_category.side_effect = lambda name: name
    created_category = await rec.call("create_category", "POST", "/categories", reviewer, status=201, json={"name": "Mascotas"})
    assert created_category == {"name": "Mascotas", "created": True}
    defaults = await rec.call("create_default_categories", "POST", "/categories/defaults", reviewer)
    assert "Transporte" in defaults["existing"] and "Compras" in defaults["created"]
    tienda_d1 = next(merchant for merchant in merchants["items"] if merchant["name"] == "TIENDA D1")
    await rec.call("set_merchant_category", "PUT", f"/merchants/{tienda_d1['id']}", reviewer, json={"default_category": "Alimentación", "create_rule": True})
    await rec.error("error_422_validation_error", "POST", "/rules", reviewer, 422, "validation_error", json={"match": "text_contains", "pattern": "x", "category": "Categoria inexistente"})

    # -- Review actions ----------------------------------------------------------------------------------------------------
    await rec.call("classify", "POST", f"/inbox/{ids.tienda.id}/classify", reviewer, json={"category": "Alimentación", "create_rule": True})
    await rec.call(
        "approve", "POST", f"/inbox/{ids.rappi.id}/approve", reviewer,
        json={"category": "Alimentación", "tags": ["domicilio"], "description": "Pedido a domicilio"},
    )
    await rec.call("edit", "PATCH", f"/inbox/{ids.manual.id}", reviewer, json={"description": "Almuerzo con el equipo de trabajo", "tags": ["trabajo", "equipo"]})
    await rec.call("void_item", "POST", f"/inbox/{ids.cinema.id}/void", reviewer)
    await rec.call("reject", "POST", f"/inbox/{ids.alkosto.id}/reject", reviewer)
    await rec.call("retry_failed", "POST", "/inbox/retry-failed", reviewer)
    await rec.call("reprocess", "POST", f"/inbox/{ids.rejected.id}/reprocess", reviewer)
    await rec.error("error_409_conflict", "POST", f"/inbox/{ids.netflix_last.id}/reprocess", reviewer, 409, "conflict")
    await rec.error("error_403_insufficient_scope", "POST", f"/inbox/{ids.email.id}/approve", viewer, 403, "insufficient_scope")
    await rec.error("error_404_not_found", "GET", "/inbox/999999", reviewer, 404, "not_found")

    # -- Manual and quick entries ------------------------------------------------------------------------------------------
    await rec.call(
        "create_manual_transaction", "POST", "/transactions/manual", {**reviewer, "Idempotency-Key": "manual-contract-1"}, status=201,
        json={
            "client_ref": "manual-contract-1", "amount": "54000.00", "description": "Almuerzo de prueba", "type": "withdrawal",
            "category": "Alimentación", "account": "Nequi", "merchant": "RESTAURANTE EL FOGON", "date": "2026-11-06T17:00:00+00:00",
        },
    )
    await rec.call("quick_entry", "POST", "/transactions/quick", {**reviewer, "Idempotency-Key": "quick-contract-1"}, status=201, json={"text": "25k taxi", "client_ref": "quick-contract-1"})
    await rec.error("error_422_unparseable", "POST", "/transactions/quick", reviewer, 422, "unparseable", json={"text": "taxi al aeropuerto", "client_ref": "quick-contract-2"})

    # -- Transfer pairing --------------------------------------------------------------------------------------------------
    await rec.call("pair_transfer", "POST", "/transfers/pair", reviewer, json={"out_id": ids.out.id, "in_id": ids.incoming.id})

    # -- Assistant: a batch with applied proposals, then its revert ------------------------------------------------------
    batch = await rec.call(
        "create_batch", "POST", "/assistant/batches", reviewer, status=201,
        json={
            "window": {"from": "2026-11-01T00:00:00+00:00", "to": "2026-11-06T23:00:00+00:00"},
            "instruction": "Revisa las categorías y etiquetas de los gastos de la primera semana de noviembre",
        },
    )
    batch_url = f"/assistant/batches/{batch['id']}"
    await rec.error(
        "error_422_outside_window", "POST", f"{batch_url}/proposals", reviewer, 422, "outside_window",
        json={"target_id": ids.history_first.id, "field": "category", "new_value": "Transporte", "reason": "Fuera de la ventana del lote"},
    )
    first = await rec.call(
        "create_proposal", "POST", f"{batch_url}/proposals", reviewer, status=201,
        json={"target_id": ids.big.id, "field": "tags", "new_value": ["mercado", "mensual"], "reason": "Compra grande del supermercado"},
    )
    second = await rec.call(None, "POST", f"{batch_url}/proposals", reviewer, status=201, json={"target_id": ids.big.id, "field": "description", "new_value": "Compra grande del mes", "reason": "Facilita buscarla después"})
    third = await rec.call(None, "POST", f"{batch_url}/proposals", reviewer, status=201, json={"target_id": ids.d1_first.id, "field": "category", "new_value": "Alimentación", "reason": "TIENDA D1 vende alimentos"})
    risky = await rec.call(None, "POST", f"{batch_url}/proposals", reviewer, status=201, json={"target_id": ids.d1_second.id, "field": "void", "new_value": None, "reason": "Es el mismo cobro que avisó el SMS"})
    assert risky["risky"] is True
    approved = [first["id"], second["id"], third["id"]]
    applied = await rec.call("apply_batch", "POST", f"{batch_url}/apply", reviewer, json={"approved_ids": approved})
    assert applied["applied"] == approved and applied["failed"] == [], applied
    await rec.call("get_batch", "GET", batch_url, reviewer)
    reverted = await rec.call("revert_batch", "POST", f"{batch_url}/revert", reviewer)
    assert sorted(reverted["reverted"]) == sorted(approved) and reverted["failed"] == [], reverted

    # -- Alerts, rules, flags and audit ----------------------------------------------------------------------------------
    await rec.call("ack_alert", "POST", f"/alerts/{alerts['items'][0]['id']}/ack", reviewer)
    await rec.call("delete_rule", "DELETE", f"/rules/{rule['id']}", reviewer)
    await rec.call("reject_suspicious", "POST", f"/inbox/{ids.unknown.id}/reject", reviewer, json={"suspicious": True})
    await rec.call("get_flags", "GET", "/flags", reviewer)
    resumed = await rec.call("resume_auto_approve", "POST", "/flags/auto-approve/resume", reviewer)
    assert resumed == {"paused": False}
    audit = await rec.call("list_audit", "GET", "/audit", reviewer, params={"limit": 100, "offset": 0})
    assert audit["items"]

    # -- Own accounts (the audit list above is already recorded, so these writes do not change it) -----------------------
    bancolombia = await rec.call(
        "add_own_account", "POST", "/own-accounts", reviewer, status=201,
        json={
            "firefly_account": "Bancolombia", "bank": "bancolombia", "kind": "savings", "last4": ["5678"], "aliases": ["bancolombia"],
            "sources": ["sms:87400", "app:com.demo.BancoApp", "email:Alertas@Banco-Demo.co"],
        },
    )
    assert bancolombia["firefly_exists"] is True and bancolombia["sources"] == ["sms:87400", "app:com.demo.BancoApp", "email:alertas@banco-demo.co"]
    await rec.call(
        "update_own_account", "PATCH", f"/own-accounts/{bancolombia['id']}", reviewer,
        json={
            "firefly_account": "Bancolombia", "bank": "bancolombia", "kind": "savings", "last4": ["5678"], "aliases": ["bancolombia", "ahorros"],
            "sources": ["sms:87400", "sms:87401", "app:com.demo.BancoApp", "email:alertas@banco-demo.co"],
        },
    )
    owned = await rec.call("list_own_accounts", "GET", "/own-accounts", reviewer)
    assert [account["firefly_account"] for account in owned["items"]] == ["Bancolombia", "Nequi"]
    await rec.call("delete_own_account", "DELETE", f"/own-accounts/{bancolombia['id']}", reviewer)

    # -- Trusted mail senders: one learned and waiting for approval, one added by the owner ----------------------------
    learned = await KnownSenderRepository(db).add("notificaciones@nu.com.co", name="Nu Colombia", source="learned", status="pending_approval")
    await db.commit()
    owner_sender = await rec.call(
        "add_sender", "POST", "/senders", reviewer,
        json={"keyword": "alertasynotificaciones@bancolombia.com.co", "name": "Bancolombia", "require_auth": True},
    )
    listed = await rec.call("list_senders", "GET", "/senders", reviewer)
    assert [sender["status"] for sender in listed["senders"]] == ["active", "pending_approval"], listed
    accepted = await rec.call("approve_sender", "POST", f"/senders/{learned.id}/approve", reviewer)
    assert accepted["status"] == "active" and accepted["source"] == "learned", accepted
    await rec.call("delete_sender", "DELETE", f"/senders/{owner_sender['id']}", reviewer)

    # -- History import: the mail job runs on the application's own engine and both it and the SMS queue are process
    # globals, so they are stubbed to keep the bodies deterministic (a running job with an empty SMS queue, then the queue).
    async def idle(*args):
        return None

    with monkeypatch.context() as patch:
        patch.setattr(history, "get_settings", lambda: settings)
        patch.setattr(history, "_mail_job", None)
        patch.setattr(history, "_mail_task", None)
        patch.setattr(history, "_sms_progress", {"queued": 0, "processed": 0})
        patch.setattr(history, "_sms_windows", {})
        patch.setattr(history, "_run_mail", idle)
        patch.setattr(history, "_process_sms", idle)
        job = await rec.call(
            "start_mail_history", "POST", "/history/mail", reviewer, status=202,
            json={"since": "2026-11-01", "until": "2026-11-05", "dry_run": True},
        )
        assert job["job"]["state"] == "running" and job["job"]["dry_run"] is True, job
        sms_at = int(datetime(2026, 11, 5, 15, 0, tzinfo=UTC).timestamp() * 1000)
        sms_batch = [
            {"sender": "85954", "body": "Pagaste $12.000 en TIENDA DEMO", "date": sms_at},
            {"sender": "85954", "body": "Recibiste $40.000 de JUAN PEREZ", "date": sms_at + 60_000},
            {"sender": "12345", "body": "Promo: 2x1 este fin de semana", "date": sms_at},
        ]
        imported = await rec.call("import_sms", "POST", "/history/sms", reviewer, json={"dry_run": False, "items": sms_batch})
        assert imported["queued"] == 2 and imported["counts"]["untrusted"] == 1, imported
        status = await rec.call("history_status", "GET", "/history", reviewer)
        assert status["mail"]["state"] == "running" and status["sms"] == {"queued": 2, "processed": 0}, status

    # -- Auto-approval policy and inbox auto-resolve. The re-evaluation runs on the application's own engine, so it is stubbed like
    # the history import: a task that waits for a gate keeps the "already running" answer deterministic.
    await rec.call("update_auto_approve", "PUT", "/settings/auto-approve", reviewer, json={"enabled": True, "max_amount_cop": "5000000.00"})
    assert (await rec.call("get_auto_approve", "GET", "/settings/auto-approve", reviewer)) == {"enabled": True, "max_amount_cop": "5000000.00"}
    gate = asyncio.Event()

    async def hold(ids):
        await gate.wait()

    with monkeypatch.context() as patch:
        patch.setattr(inbox_router, "_resolve_task", None)
        patch.setattr(inbox_router, "_run_auto_resolve", hold)
        queued = await rec.call("auto_resolve_inbox", "POST", "/inbox/auto-resolve", reviewer, status=202)
        assert queued["queued"] >= 1, queued
        await rec.error("error_409_auto_resolve_running", "POST", "/inbox/auto-resolve", reviewer, 409, "auto_resolve_running")
        gate.set()
        await inbox_router._resolve_task

    # -- Ledger editing (section 15): a stateful Firefly stands in for the transaction endpoints -----------------------
    FireflyLedger(firefly, list(await service.accounts()), budgets={"11": "Mercado"}, bills={"21": "Internet"})
    created = await rec.call(
        "create_ledger_transaction", "POST", "/ledger/transactions", reviewer, status=201,
        json={
            "client_ref": "ledger-contract-1", "type": "withdrawal", "date": "2026-11-06T12:00:00-05:00",
            "description": "Compras de la semana", "notes": "Pagado con Nequi",
            "splits": [
                {
                    "amount": "54000.00", "source_account": "Nequi", "destination_account": "SUPERMERCADO CENTRAL",
                    "category": "Alimentación", "budget": "Mercado", "bill": "Internet", "tags": ["casa"],
                },
                {
                    "amount": "20000.00", "source_account": "Nequi", "destination_account": "TAXI LIBRE", "category": "Transporte",
                    "foreign_amount": "5.00", "foreign_currency": "USD", "description": "Taxi", "notes": "Carrera nocturna",
                },
            ],
        },
    )
    ledger_id = created["transaction"]["id"]
    await rec.call("get_ledger_transaction", "GET", f"/ledger/transactions/{ledger_id}", reviewer)
    await rec.call(
        "update_ledger_transaction", "PUT", f"/ledger/transactions/{ledger_id}", reviewer,
        json={
            "type": "withdrawal", "date": "2026-11-06T12:00:00-05:00", "description": "Compras de la semana y taxi",
            "splits": [
                {
                    "amount": "54000.00", "source_account": "Nequi", "destination_account": "SUPERMERCADO CENTRAL",
                    "category": "Alimentación", "budget": "Mercado", "tags": ["casa", "mercado"],
                },
                {
                    "amount": "20000.00", "source_account": "Nequi", "destination_account": "TAXI LIBRE", "category": "Transporte",
                    "foreign_amount": "5.00", "foreign_currency": "USD", "description": "Taxi",
                },
            ],
        },
    )
    lunch = {
        "type": "withdrawal", "date": "2026-11-05T13:00:00-05:00", "description": "Almuerzo",
        "splits": [{"amount": "18000.00", "source_account": "Nequi", "destination_account": "SUPERMERCADO CENTRAL", "tags": ["viejo"]}],
    }
    lunch_id = (await rec.call(None, "POST", "/ledger/transactions", reviewer, status=201, json={**lunch, "client_ref": "ledger-contract-2"}))["transaction"]["id"]
    dinner_id = (await rec.call(None, "POST", "/ledger/transactions", reviewer, status=201, json={**lunch, "client_ref": "ledger-contract-3", "description": "Cena"}))["transaction"]["id"]
    bulk = await rec.call(
        "bulk_ledger_edit", "POST", "/ledger/bulk", reviewer,
        json={"ids": [lunch_id, dinner_id, "999999"], "set": {"category": "Transporte", "tags_add": ["lote"], "tags_remove": ["viejo"]}},
    )
    assert bulk["changed"] == [lunch_id, dinner_id] and [row["id"] for row in bulk["failed"]] == ["999999"], bulk
    deleted = await rec.call("delete_ledger_transaction", "DELETE", f"/ledger/transactions/{dinner_id}", reviewer)
    changes = await rec.call("list_ledger_changes", "GET", "/ledger/changes", reviewer, params={"limit": 50, "offset": 0})
    assert changes["total"] == 7 and changes["items"][0]["id"] == deleted["change"]["id"], changes
    restored = await rec.call("undo_ledger_change", "POST", f"/ledger/changes/{deleted['change']['id']}/undo", reviewer)
    assert restored["transaction"]["id"] != dinner_id, restored
    # The dinner came back with a new id, so only the lunch edit of the bulk group can still be undone.
    group = await rec.call("undo_ledger_group", "POST", f"/ledger/groups/{bulk['group_id']}/undo", reviewer)
    assert len(group["undone"]) == 1 and len(group["failed"]) == 1, group

    # -- Endpoints of the later phases: one self-contained section per area (see tests/contract_*.py) -------------------
    env = SimpleNamespace(
        rec=rec, client=client, service=service, db=db, clients=clients, settings=settings, monkeypatch=monkeypatch,
        reviewer=reviewer, ids=ids, tmp_path=tmp_path,
    )
    for section in (contract_snapshots_review, contract_statements_budgets, contract_capture, contract_agent_memory, contract_quality):
        await section.record(env)

    _finish(rec.bodies)
