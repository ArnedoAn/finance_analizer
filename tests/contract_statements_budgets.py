"""Contract fixtures of the statement reconciliation and the monthly budgets (area ``statements_budgets``).

Both areas write to Firefly, so each phase gets its own simulated Firefly and LLM and its own ``Processor`` (swapped in
through the dependency override while the phase runs): nothing of the main test's shared mocks is touched. The statement
PDF is built here, the LLM answers are canned and the background tasks of the routers are awaited on the test's session.
"""
import json
import sys
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

from app.api.dependencies import get_processor
from app.api.routers import budgets as budgets_router
from app.api.routers import statements as statements_router
from app.clients.firefly import FireflyClient
from app.clients.llm import LLMClient
from app.main import app
from app.parsers.statements import ParsedBlock, ParsedRow
from app.services import ai_budget, ai_features, budget_status, statements
from app.services.processor import Processor
from tests.test_budgets_v4 import World
from tests.test_ledger_v4 import FireflyLedger
from tests.test_statements_v4 import encrypted, make_pdf

API = "/api/v1"
OWN_ACCOUNT_ID = 1  # the Nequi account every test database starts with
STATUS_NOW = datetime(2026, 11, 6, 17, 0, tzinfo=UTC)  # 12:00 in Bogota: day 6 of 30
SWEEP_MONTH = "2026-07"  # no other section seeds movements in this month
ACCOUNTS = [
    {"id": "1", "name": "Nequi", "type": "asset"}, {"id": "2", "name": "SUPERMERCADO CENTRAL", "type": "expense"},
    {"id": "3", "name": "SALARIO", "type": "revenue"}, {"id": "4", "name": "Bancolombia", "type": "asset"},
    {"id": "7", "name": "AGUAS DE CARTAGENA", "type": "expense"}, {"id": "9", "name": "CAFE DOS", "type": "expense"},
]
STATEMENT_MONEY = ("credits", "debits", "opening_balance", "closing_balance", "expected_closing_balance", "difference")
STATUS_MONEY = ("limit", "spent", "remaining", "projected")


# --- Helpers ---------------------------------------------------------------------------------------------------------


def _without_totals(value):
    if isinstance(value, dict):
        return {key: _without_totals(item) for key, item in value.items() if key != "totals"}
    if isinstance(value, list):
        return [_without_totals(item) for item in value]
    return value


def _totals_maps(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "totals" and isinstance(item, dict):
                yield item
            else:
                yield from _totals_maps(item)
    elif isinstance(value, list):
        for item in value:
            yield from _totals_maps(item)


def _store_with_totals(rec, name, body, money):
    """Record a body whose ``totals`` map mixes money with a status or a percent.

    ``Recorder.store`` demands that every entry of a ``totals`` map is a money string, so the money entries (``money``) are
    checked here and the rest of the body goes through the recorder's own check and normalization.
    """
    main = sys.modules[type(rec).__module__]
    assert name not in rec.bodies, f"fixture recorded twice: {name}"
    main._assert_money_strings(name, _without_totals(body))
    for totals in _totals_maps(body):
        for key in money:
            value = totals[key]
            assert value is None or (isinstance(value, str) and main.DECIMAL_TEXT.match(value)), f"{name}: totals.{key} = {value!r}"
    rec.bodies[name] = main._normalize(body, rec.started)


async def _expect(env, method, path, status, code, **kwargs):
    """An error answer that is asserted but not stored as a fixture."""
    response = await env.client.request(method, API + path, headers=env.reviewer, **kwargs)
    assert response.status_code == status and response.json()["code"] == code, response.text


# --- Statements ------------------------------------------------------------------------------------------------------

STATEMENT_LINES = [
    "EXTRACTO DE CUENTA", "Titular: Maria Perez Gomez", "Cuenta 1234567890 CC 1.234.567.890", "Periodo: 2026-10-01 a 2026-10-31",
    "Saldo inicial 1000000.00", "2026-10-05 COMPRA TIENDA ALFA 25000.00", "2026-10-07 ABONO NOMINA 500000.00",
    "2026-10-12 RETIRO CAJERO 100000.00", "2026-10-20 COMPRA CAFE BETA 18000.00", "2026-10-25 PAGO SERVICIOS PUBLICOS 40000.00",
    "Saldo final 1317000.00",
]


def _statement_block() -> ParsedBlock:
    """What the simulated model reads from the PDF: two rows match the ledger, one differs in amount, two are missing."""
    rows = (
        ParsedRow(date(2026, 10, 5), "COMPRA TIENDA ALFA", Decimal("25000.00"), "debit", Decimal("975000.00")),
        ParsedRow(date(2026, 10, 7), "ABONO NOMINA", Decimal("500000.00"), "credit", Decimal("1475000.00")),
        ParsedRow(date(2026, 10, 12), "RETIRO CAJERO", Decimal("100000.00"), "debit", Decimal("1375000.00")),
        ParsedRow(date(2026, 10, 20), "COMPRA CAFE BETA", Decimal("18000.00"), "debit", Decimal("1357000.00")),
        ParsedRow(date(2026, 10, 25), "PAGO SERVICIOS PUBLICOS", Decimal("40000.00"), "debit", Decimal("1317000.00")),
    )
    return ParsedBlock(
        rows=rows, opening_balance=Decimal("1000000.00"), closing_balance=Decimal("1317000.00"),
        period_from=date(2026, 10, 1), period_to=date(2026, 10, 31),
    )


def _journal(firefly_id, kind, day, description, amount):
    return {
        "firefly_id": firefly_id, "transaction_journal_id": firefly_id, "type": kind, "date": f"{day}T12:00:00-05:00",
        "description": description, "amount": amount,
        "source_id": "1" if kind in {"withdrawal", "transfer"} else "99", "destination_id": "1" if kind == "deposit" else "98",
    }


LEDGER = [
    _journal("10", "withdrawal", "2026-10-06", "Tienda Alfa", "25000.00"),
    _journal("11", "deposit", "2026-10-07", "Nomina", "500000.00"),
    _journal("12", "withdrawal", "2026-10-20", "Cafe Beta", "18500.00"),
    _journal("13", "withdrawal", "2026-10-15", "Algo sin extracto", "9000.00"),
    _journal("14", "withdrawal", "2026-11-02", "Fuera del periodo", "7000.00"),
    _journal("15", "reconciliation", "2026-10-31", "Ajuste", "1.00"),
]


async def _statements(env):
    rec = env.rec
    firefly = AsyncMock(spec=FireflyClient)
    firefly.accounts.return_value = ACCOUNTS
    firefly.categories.return_value = ["Alimentación", "Transporte"]
    firefly.find_by_external_id.return_value = None
    firefly.list_auto_transactions.return_value = []
    firefly.create_account.return_value = {"id": "5", "name": "OTRO", "type": "expense"}
    firefly.account_transactions.return_value = LEDGER

    async def create_transaction(payload):
        if "SERVICIOS" in json.dumps(payload):  # Firefly does not take the utilities payment
            raise OSError("offline")
        return "tx-1"

    firefly.create_transaction.side_effect = create_transaction
    llm = AsyncMock(spec=LLMClient)
    llm.statement_rows = AsyncMock(return_value=_statement_block())
    processor = Processor(env.db, firefly, llm, settings=env.settings)

    async def background(statement_id, data, password):
        await statements.process(env.db, firefly, llm, statement_id, data, password)

    with env.monkeypatch.context() as patch:
        patch.setattr(statements_router, "process_background", background)
        patch.setattr(ai_budget, "record", AsyncMock())
        patch.setitem(app.dependency_overrides, get_processor, lambda: processor)

        form = {"own_account_id": str(OWN_ACCOUNT_ID)}
        uploaded = await rec.call(
            "upload_statement", "POST", "/statements", env.reviewer, status=202,
            files={"file": ("extracto-octubre.pdf", make_pdf(pages=[STATEMENT_LINES]), "application/pdf")}, data=form,
        )
        statement_id = uploaded["id"]
        assert uploaded["status"] == "processing", uploaded
        # An encrypted PDF without its password stops in needs_password (the app retries with the password).
        locked = await rec.call(
            None, "POST", "/statements", env.reviewer, status=202,
            files={"file": ("extracto-cifrado.pdf", encrypted(make_pdf(pages=[STATEMENT_LINES]), "1010"), "application/pdf")}, data=form,
        )
        locked_id = locked["id"]
        assert locked_id != statement_id
        await _expect(
            env, "POST", "/statements", 422, "not_a_pdf",
            files={"file": ("nota.pdf", b"MZ\x90\x00 definitely not a pdf", "application/pdf")}, data=form,
        )
        assert llm.statement_rows.await_count == 1  # the encrypted one never reached the model

        listing = await rec.call(None, "GET", "/statements", env.reviewer, params={"limit": 50, "offset": 0})
        _store_with_totals(rec, "list_statements", listing, STATEMENT_MONEY)
        assert [item["id"] for item in listing["items"]] == [locked_id, statement_id], listing
        assert listing["items"][0]["status"] == "needs_password" and listing["items"][0]["result"] is None
        assert listing["items"][1]["result"] == {"matched": 2, "missing": 2, "extra": 1, "mismatched": 1, "imported": 0}  # counts only

        detail = await rec.call(None, "GET", f"/statements/{statement_id}", env.reviewer)
        _store_with_totals(rec, "get_statement", detail, STATEMENT_MONEY)
        assert detail["status"] == "ready" and detail["reason"] is None and detail["account"] == {"id": OWN_ACCOUNT_ID, "name": "Nequi"}
        assert detail["period"] == {"from": "2026-10-01", "to": "2026-10-31"} and detail["rows_count"] == 5, detail
        assert detail["totals"] == {
            "credits": "500000.00", "debits": "183000.00", "opening_balance": "1000000.00", "closing_balance": "1317000.00",
            "expected_closing_balance": "1317000.00", "difference": "0.00", "balance_check": "ok",
        }, detail
        result = detail["result"]
        assert result["matched"] == 2 and result["imported"] == 0
        missing = {row["description"]: row["row_id"] for row in result["missing"]}
        assert list(missing) == ["RETIRO CAJERO", "PAGO SERVICIOS PUBLICOS"], result
        assert [(row["firefly_id"], row["description"]) for row in result["extra"]] == [("13", "Algo sin extracto")], result
        assert [(row["description"], row["firefly_amount"], row["difference"]) for row in result["mismatched"]] == [("COMPRA CAFE BETA", "18500.00", "-500.00")]
        mismatched_id = result["mismatched"][0]["row_id"]

        await _expect(env, "POST", f"/statements/{locked_id}/import-missing", 409, "statement_not_ready", json={"row_ids": [1]})
        imported = await rec.call(
            "import_statement_missing", "POST", f"/statements/{statement_id}/import-missing", env.reviewer,
            json={"row_ids": [missing["RETIRO CAJERO"], missing["PAGO SERVICIOS PUBLICOS"], mismatched_id, 999999], "confirm_unbalanced": False},
        )
        assert [(row["row_id"], row["firefly_id"]) for row in imported["created"]] == [(missing["RETIRO CAJERO"], "tx-1")], imported
        assert isinstance(imported["created"][0]["inbox_id"], int)
        assert imported["skipped"] == [{"row_id": mismatched_id, "reason": "not_missing"}, {"row_id": 999999, "reason": "not_found"}], imported
        assert len(imported["failed"]) == 1 and imported["failed"][0]["row_id"] == missing["PAGO SERVICIOS PUBLICOS"], imported
        assert imported["failed"][0]["reason"] == "upstream_unavailable" and isinstance(imported["failed"][0]["inbox_id"], int)
        after = (await rec.call(None, "GET", f"/statements/{statement_id}", env.reviewer))["result"]
        assert after["imported"] == 1 and [row["row_id"] for row in after["missing"]] == [missing["PAGO SERVICIOS PUBLICOS"]], after

        assert await rec.call("delete_statement", "DELETE", f"/statements/{locked_id}", env.reviewer) == {"deleted": True}
        await _expect(env, "GET", f"/statements/{locked_id}", 404, "statement_not_found")


# --- Budgets ---------------------------------------------------------------------------------------------------------

BUDGET_ANSWERS = {
    "AGUAS DE CARTAGENA": ("Agua", 0.7, "Servicio público de agua"),
    "CAFE DOS": ("Ocio", 0.6, "Café y salidas"),
    "ALMACENES OLIMPICA": ("Mercado", 0.93, "Compra de supermercado"),
}


async def _budget_answer(prompt, content, cycle_id, usage=None, model=None):
    """The simulated model: the budget depends on the merchant it is shown."""
    usage["model"] = "deepseek-v4.1-flash"
    merchant = content["UNTRUSTED_DATA"]["item"]["merchant"].upper()
    budget, confidence, reason = next(answer for name, answer in BUDGET_ANSWERS.items() if name in merchant)
    return {"budget": budget, "confidence": confidence, "reason": reason}


def _default_threshold(original):
    """``ai_features.get`` with the stock confidence threshold, whatever another section may have stored."""
    async def get(session):
        return {**await original(session), "confidence_threshold": 0.85}

    return get


async def _expense(processor, ledger, ref, merchant, destination, amount, category, day):
    """A registered July expense that also lives in the simulated ledger."""
    group = ledger.seed(
        amount=amount, source="1", destination=destination, date=f"2026-07-{day:02d}T12:00:00-05:00", description=f"Compra {merchant}",
        external_id=f"fa:{ref}", category_name=category,
    )
    row = await processor.inbox.add(
        source="sms", source_ref=ref, sender="85954", subject_or_title="Compra", raw_text=None, occurred_at=datetime(2026, 7, day, 17, 0, tzinfo=UTC),
        status="created", method="parser", transaction_type="withdrawal", amount=Decimal(amount), currency="COP", merchant=merchant,
        category=category, asset_account="Nequi", firefly_transaction_id=group,
    )
    await processor.session.commit()
    return row


async def _budgets(env):
    rec, db = env.rec, env.db
    firefly = AsyncMock(spec=FireflyClient)
    firefly.accounts.return_value = ACCOUNTS
    firefly.categories.return_value = ["Servicios", "Alimentación", "Restaurantes"]
    ledger = FireflyLedger(firefly, ACCOUNTS, budgets={"11": "Agua", "12": "Mercado", "13": "Ocio"}, bills={})
    world = World(firefly, ledger)
    llm = AsyncMock(spec=LLMClient)
    llm.complete_json = AsyncMock(side_effect=_budget_answer)
    processor = Processor(db, firefly, llm, settings=env.settings)

    @asynccontextmanager
    async def session():
        yield db

    with env.monkeypatch.context() as patch:
        patch.setitem(app.dependency_overrides, get_processor, lambda: processor)
        patch.setattr(ai_budget, "get_settings", lambda: env.settings)
        patch.setattr(ai_features, "get", _default_threshold(ai_features.get))
        patch.setattr(budget_status, "now_utc", lambda: STATUS_NOW)
        patch.setattr(budgets_router, "get_db_session", session)
        patch.setattr(budgets_router, "get_firefly", lambda: firefly)
        patch.setattr(budgets_router, "get_llm", lambda: llm)
        patch.setattr(budgets_router, "_assign_task", None)

        # -- Import: a dry run over a CSV with Colombian amounts, a total row and unusable rows, planned for two months ----
        for month in ("2026-12", "2027-01"):
            world.limits[("11", month)] = Decimal("100000.00")  # Agua already has this limit: unchanged
            world.limits[("12", month)] = Decimal("800000.00")  # Mercado has another one: update
        sheet = "\n".join([
            "Categoría;Monto", "Agua;$100.000", "Mercado;900.000", "Transporte;$ 250.000,50", "Agua;0", "Total;1.250.000,50", ";", "Sin monto;n/a",
        ]).encode("utf-8")
        planned = await rec.call(
            "import_budgets", "POST", "/budgets/import", env.reviewer,
            files={"file": ("presupuesto.csv", sheet, "application/octet-stream")}, data={"month": "2026-12", "dry_run": "true", "repeat_months": "1"},
        )
        assert planned["dry_run"] is True and planned["month"] == "2026-12" and planned["months"] == ["2026-12", "2027-01"], planned
        assert planned["rows"] == [
            {"name": "Agua", "amount": "100000.00", "action": "unchanged", "budget_id": "11"},
            {"name": "Mercado", "amount": "900000.00", "action": "update", "budget_id": "12"},
            {"name": "Transporte", "amount": "250000.50", "action": "create"},
        ], planned
        assert planned["total"] == "1250000.50" and planned["failed"] == 0
        assert {(row["name"], row["reason"]) for row in planned["skipped"]} == {("Agua", "monto_invalido"), ("Total", "total"), ("Sin monto", "monto_invalido")}
        assert planned["columns"] == {"name": 0, "amount": 1, "header_row": 0, "source": "headers"}
        assert world.writes == [] and llm.complete_json.await_count == 0

        # -- Status: one budget over its limit, one at risk, one fine (day 6 of 30) -----------------------------------------
        for ident, limit in (("11", "100000.00"), ("12", "500000.00"), ("13", "300000.00")):
            world.limits[(ident, "2026-11")] = Decimal(limit)
        world.spent.update({"Agua": Decimal("40000"), "Mercado": Decimal("600000"), "Ocio": Decimal("30000")})
        status = await rec.call(None, "GET", "/budgets/status", env.reviewer, params={"month": "2026-11"})
        _store_with_totals(rec, "get_budget_status", status, STATUS_MONEY)
        assert (status["month"], status["days_in_month"], status["days_elapsed"]) == ("2026-11", 30, 6), status
        assert [(row["name"], row["state"]) for row in status["budgets"]] == [("Mercado", "over"), ("Agua", "at_risk"), ("Ocio", "ok")], status
        assert status["budgets"][0] == {
            "name": "Mercado", "limit": "500000.00", "spent": "600000.00", "remaining": "-100000.00", "percent": 120.0,
            "projected": "3000000.00", "projected_percent": 600.0, "state": "over",
        }, status
        assert status["totals"] == {
            "limit": "900000.00", "spent": "670000.00", "remaining": "230000.00", "percent": 74.4, "projected": "3350000.00",
            "projected_percent": 372.2, "state": "at_risk",
        }, status
        await _expect(env, "GET", "/budgets/status", 422, "invalid_month", params={"month": "noviembre"})

        # -- Assignment: the month sweep runs in the background; water and coffee wait for the owner, the supermarket is applied --
        for ident in ("11", "12", "13"):
            world.limits[(ident, SWEEP_MONTH)] = Decimal("500000.00")
        await _expense(processor, ledger, "contract-budget-water", "AGUAS DE CARTAGENA", "7", "85400.00", "Servicios", 10)
        await _expense(processor, ledger, "contract-budget-coffee", "CAFE DOS", "9", "12000.00", "Restaurantes", 12)
        await _expense(processor, ledger, "contract-budget-market", "ALMACENES OLIMPICA", "2", "154000.00", "Alimentación", 14)
        queued = await rec.call("assign_budgets", "POST", "/budgets/assign", env.reviewer, status=202, params={"month": SWEEP_MONTH})
        assert queued == {"queued": 3}, queued
        await budgets_router._assign_task
        assert llm.complete_json.await_count == 3
        assert (await rec.call(None, "POST", "/budgets/assign", env.reviewer, status=202, params={"month": SWEEP_MONTH})) == {"queued": 0}
        await _expect(env, "POST", "/budgets/assign", 422, "invalid_month", params={"month": "mañana"})

        everything = await rec.call(None, "GET", "/budgets/suggestions", env.reviewer, params={"status": "all", "limit": 100, "offset": 0})
        assert [(row["budget"], row["status"], row["source"]) for row in everything["items"]] == [
            ("Mercado", "auto_applied", "ai"), ("Agua", "pending", "ai"), ("Ocio", "pending", "ai"),
        ], everything
        assert everything["items"][0]["group_id"] and everything["items"][0]["applied_at"] is not None

        suggestions = await rec.call(
            "list_budget_suggestions", "GET", "/budgets/suggestions", env.reviewer, params={"status": "pending", "limit": 100, "offset": 0},
        )
        assert suggestions["total"] == 2, suggestions
        water, coffee = suggestions["items"]
        assert (water["budget"], water["confidence"], water["status"], water["model"]) == ("Agua", 0.7, "pending", "deepseek-v4.1-flash"), water
        assert (coffee["budget"], coffee["confidence"], coffee["reason"]) == ("Ocio", 0.6, "Café y salidas"), coffee
        assert water["item"]["merchant"] == "AGUAS DE CARTAGENA" and water["item"]["amount"] == "85400.00"
        assert water["group_id"] is None and water["applied_at"] is None

        # -- The owner applies everything at or above 0.7 (and names an id that does not exist), then discards the other one ----
        applied = await rec.call(
            "apply_budget_suggestions", "POST", "/budgets/suggestions/apply", env.reviewer, json={"ids": [999999], "all_above": 0.7},
        )
        assert applied == {"applied": [water["id"]], "failed": [{"id": 999999, "detail": "La sugerencia no existe."}]}, applied
        assert ledger.journals(water["item"]["firefly_transaction_id"])[0]["budget_name"] == "Agua"
        assert ledger.journals(coffee["item"]["firefly_transaction_id"])[0]["budget_name"] is None
        discarded = await rec.call("discard_budget_suggestion", "POST", f"/budgets/suggestions/{coffee['id']}/discard", env.reviewer)
        assert discarded["id"] == coffee["id"] and discarded["status"] == "discarded" and discarded["budget"] == "Ocio", discarded
        await _expect(env, "POST", f"/budgets/suggestions/{coffee['id']}/discard", 409, "suggestion_not_pending")
        await _expect(env, "POST", "/budgets/suggestions/999999/discard", 404, "suggestion_not_found")


async def record(env):
    await _statements(env)
    await _budgets(env)
