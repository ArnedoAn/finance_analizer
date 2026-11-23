"""Monthly budgets (import, status, assignment, learning) and PSE payments, with Firefly and the LLM simulated.

The PSE end-to-end tests rely on the ``Processor`` hooks documented in ``docs/contract-additions/ServerBudgets.md``
(``pse.tag``, ``pse.check``, ``pse.supersedes``, the containment match of ``_resolve``); the rest does not.
"""
import importlib.util
import io
import zipfile
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from app.api.dependencies import hash_secret
from app.api.routers import budgets as budgets_router
from app.core.exceptions import PermanentError
from app.core.timezone import now_utc
from app.db.models import AiUsage, BudgetAssignment, BudgetMap, InboxItem
from app.db.models_assistant import Audit
from app.db.models_devices import Device
from app.db.models_ledger import LedgerChange
from app.parsers import Extraction
from app.services import (
    ai_budget,
    ai_features,
    ai_insights,
    budget_assign,
    budget_import,
    budget_status,
    pse,
)
from app.services import processor as pipeline
from tests.test_ledger_v4 import FireflyLedger

ADMIN = {"X-API-Key": "test-admin-key-01234567890123456789"}
API = "/api/v1"
ZONE = ZoneInfo("America/Bogota")
CURRENT = budget_assign.current_month(ZONE)
M = "2031-03"  # a month far from today: the fixtures give the current month its own limits
MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "20261124_13_budgets.py"


# --- the simulated Firefly: ledger, budgets and monthly limits ------------------------------------------------------------------


class World:
    """Budgets and monthly limits next to the stateful ledger of ``tests/test_ledger_v4.py`` (they share the budget table)."""

    def __init__(self, firefly, ledger):
        self.firefly, self.ledger = firefly, ledger
        self.limits: dict[tuple[str, str], Decimal] = {}
        self.spent: dict[str, Decimal] = {}
        self.writes: list[tuple] = []
        self.refuse: set[str] = set()
        firefly.budgets.side_effect = self.budgets
        firefly.budget_limits.side_effect = self.budget_limits
        firefly.create_budget.side_effect = self.create_budget
        firefly.create_budget_limit.side_effect = self.create_budget_limit
        firefly.update_budget_limit.side_effect = self.update_budget_limit
        firefly.budget_progress.side_effect = self.budget_progress
        firefly.accounts.side_effect = lambda: [dict(account) for account in ledger.accounts.values()]

    def id_of(self, name):
        return next(ident for ident, known in self.ledger.budgets.items() if known == name)

    async def budgets(self):
        return [{"id": ident, "name": name} for ident, name in self.ledger.budgets.items()]

    async def budget_limits(self, budget_id, start, end):
        amount = self.limits.get((budget_id, start.strftime("%Y-%m")))
        if amount is None:
            return []
        return [{"id": f"L{budget_id}-{start:%Y-%m}", "amount": amount, "start": start.isoformat(), "end": end.isoformat()}]

    async def create_budget(self, name):
        if name in self.refuse:
            raise PermanentError("Firefly 422: nope")
        ident = str(max(int(key) for key in self.ledger.budgets) + 1)
        self.ledger.budgets[ident] = name
        self.writes.append(("budget", name))
        return {"id": ident, "name": name}

    async def create_budget_limit(self, budget_id, start, end, amount):
        assert (end.day, start.day) == (end.day, 1) and start.month == end.month
        self.limits[(budget_id, start.strftime("%Y-%m"))] = amount
        self.writes.append(("limit", self.ledger.budgets[budget_id], start.strftime("%Y-%m"), f"{amount:.2f}"))
        return f"L{budget_id}-{start:%Y-%m}"

    async def update_budget_limit(self, budget_id, limit_id, start, end, amount):
        assert limit_id == f"L{budget_id}-{start:%Y-%m}"
        self.limits[(budget_id, start.strftime("%Y-%m"))] = amount
        self.writes.append(("limit_update", self.ledger.budgets[budget_id], start.strftime("%Y-%m"), f"{amount:.2f}"))

    async def budget_progress(self, start, end):
        month = start.strftime("%Y-%m")
        return [
            {"name": name, "spent": self.spent.get(name, Decimal(0)).quantize(Decimal("0.01")), "limit": self.limits[(ident, month)].quantize(Decimal("0.01"))}
            for ident, name in self.ledger.budgets.items() if (ident, month) in self.limits
        ]


@pytest.fixture
def world(clients):
    firefly, _ = clients
    extra = [
        {"id": "6", "name": "Dale", "type": "asset"}, {"id": "7", "name": "AGUAS DE CARTAGENA", "type": "expense"},
        {"id": "8", "name": "TIENDA UNO", "type": "expense"}, {"id": "9", "name": "CAFE DOS", "type": "expense"},
    ]
    firefly.accounts.return_value = [*firefly.accounts.return_value, *extra]
    firefly.categories.return_value = ["Servicios", "Alimentación", "Agua", "Ocio"]
    ledger = FireflyLedger(firefly, firefly.accounts.return_value, budgets={"11": "Agua", "12": "Mercado", "13": "Ocio"}, bills={})
    made = World(firefly, ledger)
    made.limits[("11", CURRENT)] = Decimal("100000.00")
    made.limits[("12", CURRENT)] = Decimal("800000.00")
    return made


@pytest.fixture
def llm(clients):
    clients[1].complete_json = AsyncMock(return_value=answer())
    return clients[1]


@pytest.fixture
def background(monkeypatch, db, service):
    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(budgets_router, "get_db_session", session)
    monkeypatch.setattr(budgets_router, "get_firefly", lambda: service.firefly)
    monkeypatch.setattr(budgets_router, "get_llm", lambda: service.llm)
    budgets_router._assign_task = None
    yield
    budgets_router._assign_task = None


@pytest.fixture(autouse=True)
def isolated_budget_settings(settings, monkeypatch):
    monkeypatch.setattr(ai_budget, "get_settings", lambda: settings)


def answer(budget="Agua", confidence=0.93, reason="Servicio público de agua"):
    return {"budget": budget, "confidence": confidence, "reason": reason}


def sent(llm, index=-1):
    return llm.complete_json.await_args_list[index].args[1]


async def audits(db, action):
    return list((await db.scalars(select(Audit).where(Audit.action == action).order_by(Audit.id))).all())


async def device(db, name, scopes):
    key = f"{name}-key-" + "0123456789" * 3
    db.add(Device(name=name, key_hash=hash_secret(key), scopes=scopes))
    await db.commit()
    return {"X-API-Key": key}


async def expense(service, world, *, merchant="SUPERMERCADO CENTRAL", category=None, source="sms", destination="2", amount="12000.00", **fields):
    """A registered expense that also lives in the simulated ledger."""
    ref = str(uuid4())
    extra = {"category_name": category} if category else {}
    group = world.ledger.seed(amount=amount, source="1", destination=destination, external_id="fa:" + ref, **extra)
    values = dict(
        source=source, source_ref=ref, sender="85954", subject_or_title="Compra", raw_text=None, occurred_at=now_utc() - timedelta(hours=1),
        status="created", method="parser", transaction_type="withdrawal", amount=Decimal(amount), currency="COP", merchant=merchant,
        category=category, asset_account="Nequi", firefly_transaction_id=group,
    )
    row = await service.inbox.add(**{**values, **fields})
    await service.session.commit()
    return row


async def assignment(db, item_id):
    return await db.scalar(select(BudgetAssignment).where(BudgetAssignment.item_id == item_id), execution_options={"populate_existing": True})


async def run(service, llm, **kwargs):
    return await budget_assign.run_batch(service.session, service.firefly, llm, service.settings, **kwargs)


def budget_of(world, group):
    return world.ledger.journals(group)[0]["budget_name"]


# --- file builders --------------------------------------------------------------------------------------------------------------


def csv_bytes(*lines, delimiter=";"):
    return "\n".join(delimiter.join(line) if not isinstance(line, str) else line for line in lines).encode("utf-8")


def xlsx_bytes(rows, *, inline=False, extra_xml=""):
    strings: list[str] = []

    def shared(text):
        if text not in strings:
            strings.append(text)
        return strings.index(text)

    sheet = []
    for number, row in enumerate(rows, 1):
        cells = []
        for column, value in enumerate(row):
            ref = f"{chr(65 + column)}{number}"
            if value is None:
                continue
            if isinstance(value, int | float | Decimal):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            elif inline:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
            else:
                cells.append(f'<c r="{ref}" t="s"><v>{shared(value)}</v></c>')
        sheet.append(f'<row r="{number}">{"".join(cells)}</row>')
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    parts = {
        "xl/workbook.xml": f'<?xml version="1.0"?>{extra_xml}<workbook xmlns="{main}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Hoja1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<?xml version="1.0"?><worksheet xmlns="{main}"><sheetData>{"".join(sheet)}</sheetData></worksheet>',
        "xl/sharedStrings.xml": f'<?xml version="1.0"?><sst xmlns="{main}">{"".join(f"<si><t>{escape(text)}</t></si>" for text in strings)}</sst>',
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


async def upload(client, data, *, name="presupuesto.csv", headers=ADMIN, **fields):
    form = {"month": M, "dry_run": "true", **fields}
    return await client.post(f"{API}/budgets/import", headers=headers, files={"file": (name, data, "application/octet-stream")}, data={key: str(value).lower() if isinstance(value, bool) else str(value) for key, value in form.items()})


# --- import: reading ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), [
    ("$1.500.000", "1500000.00"), ("1.500.000,50", "1500000.50"), ("$ 250.000", "250000.00"), ("1,500,000.00", "1500000.00"),
    ("1,500,000", "1500000.00"), ("1500000", "1500000.00"), ("1500000.5", "1500000.50"), ("12.500", "12500.00"), ("0.5", "0.50"),
    ("1500,75", "1500.75"), ("300000 COP", "300000.00"), ("(5.000)", "-5000.00"), ("-5.000", "-5000.00"),
    ("abc", None), ("", None), (None, None), ("12-34", None), ("1e6", None),
])
def test_parse_amount_colombian_formats(text, expected):
    result = budget_import.parse_amount(text)
    assert (None if result is None else f"{result:.2f}") == expected


def test_header_detection_prefers_the_budgeted_amount_over_spent_columns():
    rows = [["Rubro", "Real", "Valor mensual", "Diferencia"], ["Agua", "90000", "100000", "10000"]]
    assert budget_import._find_header(rows) == (0, 0, 2)
    assert budget_import._find_header([["Concepto", "Presupuesto"], ["x", "1"]]) == (0, 0, 1)
    assert budget_import._find_header([["Agua", "100000"], ["Mercado", "800000"]]) is None
    assert budget_import._find_header([["Categoría", "Gastado"], ["x", "1"]]) is None


def test_month_series_wraps_the_year():
    assert budget_import.month_series("2026-11", 0) == ["2026-11"]
    assert budget_import.month_series("2026-11", 3) == ["2026-11", "2026-12", "2027-01", "2027-02"]


async def test_csv_dry_run_plans_create_update_unchanged_without_writing(client, world, clients, db):
    world.limits[("11", M)] = Decimal("100000.00")
    world.limits[("12", M)] = Decimal("800000.00")
    data = csv_bytes("Categoría;Monto", "Agua;$100.000", "Mercado;900.000", "Transporte;$ 250.000,50", "Agua;0", "Total;1.250.000,50", ";", "Sin monto;n/a")
    response = await upload(client, data)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True and body["month"] == M and body["months"] == [M]
    assert body["rows"] == [
        {"name": "Agua", "amount": "100000.00", "action": "unchanged", "budget_id": "11"},
        {"name": "Mercado", "amount": "900000.00", "action": "update", "budget_id": "12"},
        {"name": "Transporte", "amount": "250000.50", "action": "create"},
    ]
    assert body["total"] == "1250000.50" and body["failed"] == 0
    assert {(row["name"], row["reason"]) for row in body["skipped"]} == {("Agua", "monto_invalido"), ("Total", "total"), ("Sin monto", "monto_invalido")}
    assert body["columns"] == {"name": 0, "amount": 1, "header_row": 0, "source": "headers"}
    assert world.writes == [] and await audits(db, "budgets.import") == []
    clients[1].complete_json.assert_not_called()


async def test_import_applies_creates_updates_and_is_idempotent(client, world, db):
    world.limits[("12", M)] = Decimal("800000.00")
    data = csv_bytes("Concepto;Valor", "Agua;100.000", "Mercado;900.000", "Transporte;250.000")
    response = await upload(client, data, dry_run=False)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [(row["name"], row["action"]) for row in body["rows"]] == [("Agua", "update"), ("Mercado", "update"), ("Transporte", "create")]
    assert all(row.get("budget_id") for row in body["rows"]) and body["failed"] == 0
    assert world.writes == [
        ("limit", "Agua", M, "100000.00"), ("limit_update", "Mercado", M, "900000.00"),
        ("budget", "Transporte"), ("limit", "Transporte", M, "250000.00"),
    ]
    entry = (await audits(db, "budgets.import"))[0]
    assert entry.detail == {"month": M, "repeat_months": 0, "rows": 3, "failed": 0, "create": 1, "update": 2, "unchanged": 0, "total": "1250000.00"}
    # A second run changes nothing.
    world.writes.clear()
    again = (await upload(client, data, dry_run=False)).json()
    assert [row["action"] for row in again["rows"]] == ["unchanged"] * 3 and world.writes == []


async def test_repeat_months_copies_the_limit_to_the_following_months(client, world):
    series = budget_import.month_series(M, 2)
    response = await upload(client, csv_bytes("Categoría;Monto", "Agua;100000"), dry_run=False, repeat_months=2, month=M)
    assert response.status_code == 200, response.text
    assert response.json()["months"] == series
    assert world.writes == [("limit", "Agua", month, "100000.00") for month in series]
    # One of the months already has another amount: only that one is updated.
    world.limits[("11", series[1])] = Decimal("90000.00")
    world.writes.clear()
    again = (await upload(client, csv_bytes("Categoría;Monto", "Agua;100000"), dry_run=False, repeat_months=2, month=M)).json()
    assert again["rows"][0]["action"] == "update"
    assert world.writes == [("limit_update", "Agua", series[1], "100000.00")]
    world.writes.clear()
    assert (await upload(client, csv_bytes("Categoría;Monto", "Agua;100000"), dry_run=False, repeat_months=2, month=M)).json()["rows"][0]["action"] == "unchanged"
    changed = (await upload(client, csv_bytes("Categoría;Monto", "Agua;110000"), dry_run=False, repeat_months=2, month=M)).json()
    assert changed["rows"][0]["action"] == "update"
    assert world.writes == [("limit_update", "Agua", month, "110000.00") for month in series]


async def test_xlsx_with_shared_and_inline_strings_numbers_text_amounts_and_a_total_row(client, world):
    rows = [
        ["Gastos del hogar", None, None], ["Rubro", "Valor mensual", "Real"], ["Agua", 1500000, 90000], ["Mercado", "$1.200.000", 700000],
        [None, None, None], ["Ocio", 250000.5, 10], ["TOTAL", 2950000, None],
    ]
    for inline in (False, True):
        response = await upload(client, xlsx_bytes(rows, inline=inline), name="presupuesto.xlsx")
        assert response.status_code == 200, response.text
        body = response.json()
        assert [(row["name"], row["amount"]) for row in body["rows"]] == [("Agua", "1500000.00"), ("Mercado", "1200000.00"), ("Ocio", "250000.50")]
        assert body["columns"] == {"name": 0, "amount": 1, "header_row": 1, "source": "headers"}


async def test_headerless_file_asks_the_llm_for_the_columns_looking_only_at_the_first_rows(client, world, clients, llm, db):
    lines = [f"Categoria {n};{n}00000;nota privada {n}" for n in range(1, 12)]
    llm.complete_json.return_value = {"header_row": None, "name_column": 0, "amount_column": 1}
    await ai_budget.set_policy(db, 0)  # an owner's task: never braked
    await db.commit()
    response = await upload(client, csv_bytes(*lines))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["columns"]["source"] == "llm" and len(body["rows"]) == 11 and body["rows"][0] == {"name": "Categoria 1", "amount": "100000.00", "action": "create"}
    rows = sent(llm)["UNTRUSTED_DATA"]["rows"]
    assert len(rows) == 6 and rows[0] == ["Categoria 1", "100000", "nota privada 1"]
    assert "UNTRUSTED_DATA" in llm.complete_json.await_args.args[0]
    usage = (await db.scalars(select(AiUsage).where(AiUsage.kind == "budget_columns"))).one()
    assert (usage.calls, usage.auto_calls) == (1, 0)


@pytest.mark.parametrize("reply", [
    {"header_row": None, "name_column": 5, "amount_column": 1}, {"header_row": None, "name_column": 1, "amount_column": 1},
    {"header_row": None, "name_column": 1, "amount_column": 0}, {"name_column": "0"}, "no sé",
])
async def test_unusable_llm_columns_are_refused(client, world, llm, reply):
    llm.complete_json.return_value = reply
    response = await upload(client, csv_bytes("Agua;100000", "Mercado;800000"))
    assert response.status_code == 422 and response.json()["code"] == "columns_not_found"


async def test_llm_down_means_columns_not_found(client, world, llm):
    llm.complete_json.side_effect = RuntimeError("LLM chain exhausted")
    assert (await upload(client, csv_bytes("Agua;100000", "Mercado;800000"))).json()["code"] == "columns_not_found"


async def test_import_refuses_bad_requests_and_files(client, world):
    ok = csv_bytes("Categoría;Monto", "Agua;100000")
    assert (await upload(client, ok, month="2026-13")).json()["code"] == "invalid_month"
    assert (await upload(client, ok, repeat_months=12)).json()["code"] == "invalid_repeat"
    assert (await upload(client, ok, repeat_months=-1)).status_code == 422
    assert (await upload(client, b"")).json()["code"] == "invalid_file"
    assert (await upload(client, b"\xd0\xcf\x11\xe0 old excel", name="x.xls")).json()["code"] == "invalid_file"
    assert (await upload(client, b"PK not really a zip", name="x.xlsx")).json()["code"] == "invalid_file"
    assert (await upload(client, csv_bytes("Categoría;Monto", "Total;100"))).json()["code"] == "no_rows"
    big = await upload(client, b"x" * (budget_import.MAX_BYTES + 5))
    assert big.status_code == 413 and big.json()["code"] == "file_too_large"
    many = csv_bytes("Concepto;Monto", *[f"Cat {n};{n}000" for n in range(1, 300)])
    assert (await upload(client, many)).json()["code"] == "too_many_rows"
    assert world.writes == []


async def test_an_xlsx_with_a_dtd_is_refused(client, world):
    bomb = xlsx_bytes([["Categoría", "Monto"], ["Agua", 1]], extra_xml='<!DOCTYPE lolz [<!ENTITY a "aaaa">]>')
    response = await upload(client, bomb, name="x.xlsx")
    assert response.status_code == 422 and response.json()["code"] == "invalid_file"


async def test_a_row_firefly_refuses_does_not_stop_the_others(client, world):
    world.refuse.add("Transporte")
    response = await upload(client, csv_bytes("Categoría;Monto", "Transporte;1000", "Salud;2000"), dry_run=False)
    body = response.json()
    assert response.status_code == 200 and body["failed"] == 1
    assert "error" in body["rows"][0] and "error" not in body["rows"][1] and body["rows"][1]["budget_id"]
    assert ("limit", "Salud", M, "2000.00") in world.writes


async def test_budget_import_routes_need_the_right_scopes(client, world, db):
    reader = await device(db, "reader", ["read"])
    reviewer = await device(db, "reviewer", ["review"])
    ok = csv_bytes("Categoría;Monto", "Agua;100000")
    assert (await upload(client, ok, headers=reader)).status_code == 403
    assert (await upload(client, ok, headers={})).status_code == 401
    assert (await upload(client, ok, headers=reviewer)).status_code == 200
    assert (await client.get(f"{API}/budgets/status", headers=reader)).status_code == 200
    assert (await client.get(f"{API}/budgets/suggestions", headers=reader)).status_code == 200
    for method, url, body in (("POST", f"{API}/budgets/assign", None), ("POST", f"{API}/budgets/suggestions/apply", {"ids": [1]}), ("POST", f"{API}/budgets/suggestions/1/discard", None)):
        assert (await client.request(method, url, headers=reader, json=body)).status_code == 403, url


# --- status ----------------------------------------------------------------------------------------------------------------------


async def test_status_per_budget_with_projection(client, world, monkeypatch):
    month = "2026-11"
    world.limits[("11", month)] = Decimal("100000.00")
    world.limits[("12", month)] = Decimal("500000.00")
    world.limits[("13", month)] = Decimal("300000.00")
    world.spent.update({"Agua": Decimal("40000"), "Mercado": Decimal("600000"), "Ocio": Decimal("30000")})
    monkeypatch.setattr(budget_status, "now_utc", lambda: now_utc().replace(year=2026, month=11, day=10, hour=15))
    body = (await client.get(f"{API}/budgets/status", headers=ADMIN, params={"month": month})).json()
    assert (body["month"], body["days_in_month"], body["days_elapsed"]) == (month, 30, 10)
    assert [row["name"] for row in body["budgets"]] == ["Mercado", "Agua", "Ocio"]  # most used first
    assert body["budgets"][0] == {
        "name": "Mercado", "limit": "500000.00", "spent": "600000.00", "remaining": "-100000.00", "percent": 120.0,
        "projected": "1800000.00", "projected_percent": 360.0, "state": "over",
    }
    assert body["budgets"][1] == {
        "name": "Agua", "limit": "100000.00", "spent": "40000.00", "remaining": "60000.00", "percent": 40.0,
        "projected": "120000.00", "projected_percent": 120.0, "state": "at_risk",
    }
    assert body["budgets"][2]["state"] == "ok" and body["budgets"][2]["projected"] == "90000.00"
    assert body["totals"] == {
        "limit": "900000.00", "spent": "670000.00", "remaining": "230000.00", "percent": 74.4, "projected": "2010000.00",
        "projected_percent": 223.3, "state": "at_risk",
    }
    # A closed month projects what was spent; a future month projects zero; no month means the current one.
    world.limits[("11", "2026-10")] = Decimal("100000.00")
    past = (await client.get(f"{API}/budgets/status", headers=ADMIN, params={"month": "2026-10"})).json()
    assert past["days_elapsed"] == 31 and past["budgets"][0]["projected"] == past["budgets"][0]["spent"]
    world.limits[("11", "2027-01")] = Decimal("100000.00")
    future = (await client.get(f"{API}/budgets/status", headers=ADMIN, params={"month": "2027-01"})).json()
    assert future["days_elapsed"] == 0 and future["budgets"][0]["projected"] == "0.00"
    assert (await client.get(f"{API}/budgets/status", headers=ADMIN)).json()["month"] == CURRENT
    bad = await client.get(f"{API}/budgets/status", headers=ADMIN, params={"month": "noviembre"})
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_month"


# --- assignment: learned mappings, the LLM and the owner's queue ----------------------------------------------------------------


async def test_learned_merchant_mapping_assigns_without_the_llm_and_is_undoable(service, world, llm, db):
    db.add(BudgetMap(kind="merchant", key="SUPERMERCADO CENTRAL", budget="Mercado", streak=2, learned=True))
    await db.commit()
    item = await expense(service, world)
    item_id, group = item.id, item.firefly_transaction_id
    result = await run(service, llm)
    assert result["auto_applied"] == 1 and result["skipped"] is None
    llm.complete_json.assert_not_called()
    assert budget_of(world, group) == "Mercado"
    row = await assignment(db, item_id)
    assert (row.status, row.budget, row.source, row.confidence, row.month) == ("auto_applied", "Mercado", "learned", 1.0, CURRENT)
    assert row.reason.startswith("Aprendido") and row.group_id and row.applied_at
    change = await db.scalar(select(LedgerChange).where(LedgerChange.group_id == row.group_id))
    assert change.actor == "system" and change.after_version["splits"][0]["budget"] == "Mercado"
    assert (await audits(db, "budget_assign.auto_apply"))[0].detail["source"] == "learned"
    # Nothing is decided twice, and the server's own write never teaches a mapping.
    assert (await run(service, llm)) == {"skipped": "nothing_to_do"}
    assert await db.scalar(select(sa.func.count()).select_from(BudgetMap)) == 1
    undo = await client_undo(service, row.group_id)
    assert undo == []


async def client_undo(service, group_id):
    from app.services import ledger

    class Owner:
        device_id, name = None, "admin"

    undone, failed = await ledger.undo_group(service.session, service, group_id, Owner())
    assert undone
    return failed


async def test_category_mapping_and_a_budget_named_like_the_category(service, world, llm, db):
    db.add(BudgetMap(kind="category", key="servicios", budget="Agua", streak=3, learned=True))
    db.add(BudgetMap(kind="category", key="ocio", budget="Mercado", streak=1, learned=False))  # not learned yet: ignored
    await db.commit()
    by_category = await expense(service, world, merchant="OTRO", category="Servicios")
    by_name = await expense(service, world, merchant="ALGO", category="Ocio")
    ids = (by_category.id, by_name.id)
    result = await run(service, llm)
    assert result["auto_applied"] == 2
    llm.complete_json.assert_not_called()
    first, second = await assignment(db, ids[0]), await assignment(db, ids[1])
    assert (first.budget, first.source) == ("Agua", "learned") and (second.budget, second.source) == ("Ocio", "category_name")


async def test_merchant_mapping_wins_over_category_and_a_missing_budget_falls_through(service, world, llm, db):
    db.add_all([
        BudgetMap(kind="merchant", key="SUPERMERCADO CENTRAL", budget="Ocio", streak=2, learned=True),
        BudgetMap(kind="category", key="servicios", budget="Agua", streak=3, learned=True),
        BudgetMap(kind="merchant", key="TIENDA UNO", budget="Presupuesto borrado", streak=2, learned=True),
    ])
    await db.commit()
    both = await expense(service, world, category="Servicios")
    gone = await expense(service, world, merchant="TIENDA UNO", destination="8")
    llm.complete_json.return_value = answer("Mercado", 0.9)
    await run(service, llm)
    assert (await assignment(db, both.id)).budget == "Ocio"
    assert (await assignment(db, gone.id)).source == "ai" and llm.complete_json.await_count == 1


async def test_llm_assigns_with_the_budgets_of_the_month_redacted_evidence_and_ten_similar_movements(service, world, llm, db):
    for n in range(12):
        similar = await expense(service, world, merchant="AGUAS DE CARTAGENA", destination="7", category="Servicios", amount=f"{50000 + n}.00")
        db.add(BudgetAssignment(item_id=similar.id, status="applied" if n % 2 else "auto_applied", budget="Agua", month=CURRENT))
    db.add(BudgetAssignment(item_id=(await expense(service, world, merchant="AGUAS DE CARTAGENA", destination="7")).id, status="pending", budget="Mercado", month=CURRENT))
    target = await expense(
        service, world, merchant="AGUAS DE CARTAGENA", destination="7", category="Servicios", amount="85400.00",
        description="Pago PSE a Aguas de Cartagena. Ref ana@example.com 12345678901",
    )
    await db.commit()
    target_id, group = target.id, target.firefly_transaction_id
    result = await run(service, llm)
    assert result["auto_applied"] == 1
    content = sent(llm)
    assert content["CONTEXT"]["budgets"] == ["Agua", "Mercado"]  # Ocio has no limit this month
    item = content["UNTRUSTED_DATA"]["item"]
    assert item["merchant"] == "AGUAS DE CARTAGENA" and item["category"] == "Servicios" and item["amount"] == "85400.00"
    wire = str(content)
    assert "ana@example.com" not in wire and "12345678901" not in wire and "[EMAIL]" in wire
    similar = content["UNTRUSTED_DATA"]["similar"]
    assert len(similar) == 10 and all(row["budget"] == "Agua" and row["merchant"] == "AGUAS DE CARTAGENA" for row in similar)
    assert set(similar[0]) == {"merchant", "category", "amount", "budget"}
    assert "UNTRUSTED_DATA" in llm.complete_json.await_args.args[0]
    assert budget_of(world, group) == "Agua"
    row = await assignment(db, target_id)
    assert (row.status, row.source, row.confidence, row.reason) == ("auto_applied", "ai", 0.93, "Servicio público de agua")
    usage = (await db.scalars(select(AiUsage).where(AiUsage.kind == "budget_assign"))).one()
    assert (usage.calls, usage.auto_calls) == (1, 1)  # the post-cycle batch is automatic


async def test_the_owner_threshold_decides_between_applying_and_suggesting(service, world, llm, db):
    item = await expense(service, world)
    item_id, group = item.id, item.firefly_transaction_id
    llm.complete_json.return_value = answer("Mercado", 0.9)
    await ai_features.update(db, confidence_threshold=0.95)
    await db.commit()
    assert (await run(service, llm))["pending"] == 1
    row = await assignment(db, item_id)
    assert (row.status, row.budget, row.confidence, row.group_id) == ("pending", "Mercado", 0.9, None)
    assert budget_of(world, group) is None  # nothing was written to Firefly


async def test_llm_answers_that_are_not_usable(service, world, llm, db):
    first, second, third = await expense(service, world), await expense(service, world), await expense(service, world)
    ids = (first.id, second.id, third.id)
    llm.complete_json.side_effect = [answer("Presupuesto inventado", 0.99), {"budget": "Agua", "confidence": 7}, answer(None, 0)]
    result = await run(service, llm)
    assert (result["none"], result["error"]) == (2, 1)
    rows = [await assignment(db, item_id) for item_id in ids]
    assert [(row.status, row.note) for row in rows] == [("none", "sin_presupuesto_claro"), ("error", "respuesta_invalida"), ("none", "sin_presupuesto_claro")]
    assert all(row.budget is None for row in rows)
    # A failed one is retried by automatic runs only after six hours; the month sweep takes it at once.
    zone = ZONE
    assert await budget_assign.pending_ids(db, zone) == []
    rows[1].updated_at = now_utc() - timedelta(hours=7)
    await db.commit()
    assert await budget_assign.pending_ids(db, zone) == [second.id]
    assert set(await budget_assign.pending_ids(db, zone, month=CURRENT)) == set(ids[:2] + ids[2:])


async def test_model_down_is_an_error_row_and_a_budget_firefly_already_has_is_kept(service, world, llm, db):
    plain, has = await expense(service, world), await expense(service, world)
    world.ledger.journals(has.firefly_transaction_id)[0].update(budget_id="13", budget_name="Ocio")
    llm.complete_json.side_effect = RuntimeError("LLM chain exhausted")
    result = await run(service, llm)
    assert (result["error"], result["existing"]) == (1, 1)
    assert (await assignment(db, plain.id)).note == "modelo_no_disponible"
    kept = await assignment(db, has.id)
    assert (kept.status, kept.budget, kept.source) == ("existing", "Ocio", "firefly")
    assert budget_of(world, has.firefly_transaction_id) == "Ocio"
    assert llm.complete_json.await_count == 1


async def test_the_guard_brakes_the_llm_but_not_the_learned_mappings(service, world, llm, db):
    db.add(BudgetMap(kind="merchant", key="TIENDA UNO", budget="Mercado", streak=2, learned=True))
    await db.commit()
    learned = await expense(service, world, merchant="TIENDA UNO", destination="8")
    needs_llm = await expense(service, world, merchant="CAFE DOS", destination="9")
    await ai_budget.set_policy(db, 0)
    await db.commit()
    result = await run(service, llm)
    assert result["auto_applied"] == 1 and result["skipped"] == "budget"
    llm.complete_json.assert_not_called()
    assert await assignment(db, learned.id) is not None and await assignment(db, needs_llm.id) is None  # retried next cycle


async def test_no_budgets_in_firefly_means_nothing_to_assign(service, world, llm, db):
    await expense(service, world)
    world.ledger.budgets.clear()
    assert await run(service, llm) == {"skipped": "no_budgets"}
    world.ledger.budgets["11"] = "Agua"
    world.limits.clear()  # no limits this month: every budget is offered
    llm.complete_json.return_value = answer("Agua", 0.9)
    assert (await run(service, llm))["auto_applied"] == 1
    assert sent(llm)["CONTEXT"]["budgets"] == ["Agua"]


async def test_automatic_candidates_are_recent_automatic_expenses_without_a_decision(service, world, llm, db):
    recent = await expense(service, world)
    await expense(service, world, source="manual", source_ref="manual:1")
    old = await expense(service, world)
    old.created_at = now_utc() - timedelta(days=10)
    deposit = await expense(service, world)
    deposit.transaction_type = "deposit"
    pending_review = await expense(service, world)
    pending_review.status = "needs_review"
    await db.commit()
    assert await budget_assign.pending_ids(db, ZONE) == [recent.id]
    assert set(await budget_assign.pending_ids(db, ZONE, month=CURRENT)) == {recent.id, old.id, (await db.scalar(select(InboxItem.id).where(InboxItem.source == "manual")))}


async def pending_suggestions(client):
    body = (await client.get(f"{API}/budgets/suggestions", headers=ADMIN)).json()
    return body


async def test_suggestions_are_listed_applied_in_bulk_discarded_and_taught(client, service, world, llm, db):
    firefly = service.firefly
    low = [await expense(service, world, merchant="AGUAS DE CARTAGENA", destination="7") for _ in range(3)]
    other = await expense(service, world, merchant="CAFE DOS", destination="9")
    llm.complete_json.side_effect = lambda *args, **kwargs: answer("Agua", 0.7 if "AGUAS" in str(args[1]) else 0.6)
    result = await run(service, llm)
    assert result["pending"] == 4
    body = await pending_suggestions(client)
    assert body["total"] == 4
    entry = body["items"][0]
    assert set(entry) == {"id", "status", "item", "budget", "confidence", "source", "reason", "model", "group_id", "created_at", "updated_at", "applied_at"}
    assert entry["status"] == "pending" and entry["budget"] == "Agua" and entry["confidence"] == 0.7 and entry["source"] == "ai"
    assert entry["item"]["merchant"] == "AGUAS DE CARTAGENA" and entry["item"]["id"] in {item.id for item in low}
    assert [row["confidence"] for row in body["items"]] == [0.7, 0.7, 0.7, 0.6]
    suggestion = {row["item"]["id"]: row["id"] for row in body["items"]}

    assert (await client.post(f"{API}/budgets/suggestions/apply", headers=ADMIN, json={})).status_code == 422
    assert (await client.post(f"{API}/budgets/suggestions/apply", headers=ADMIN, json={"all_above": 1.5})).status_code == 422
    # Bulk: everything at or above 0.7. Each is an owner assignment: two teach the merchant, the third is already learned.
    response = await client.post(f"{API}/budgets/suggestions/apply", headers=ADMIN, json={"all_above": 0.7, "ids": [9999]})
    assert response.status_code == 200, response.text
    applied = response.json()
    assert applied["failed"] == [{"id": 9999, "detail": "La sugerencia no existe."}]
    assert set(applied["applied"]) == {suggestion[item.id] for item in low}
    assert all(budget_of(world, item.firefly_transaction_id) == "Agua" for item in low)
    assert budget_of(world, other.firefly_transaction_id) is None
    row = await assignment(db, low[0].id)
    assert row.status == "applied" and row.group_id
    change = await db.scalar(select(LedgerChange).where(LedgerChange.group_id == row.group_id))
    assert change.actor == "admin"
    learned = await db.scalar(select(BudgetMap).where(BudgetMap.kind == "merchant", BudgetMap.key == "AGUAS DE CARTAGENA"))
    assert (learned.budget, learned.streak, learned.learned) == ("Agua", 3, True)
    assert [(log.detail["kind"], log.detail["name"], log.detail["budget"]) for log in await audits(db, "budget_map.learn")] == [("merchant", "AGUAS DE CARTAGENA", "Agua")]
    assert (await audits(db, "budget_assignment.apply"))[0].detail["via"] in {"all_above", "ids"}
    again = await client.post(f"{API}/budgets/suggestions/apply", headers=ADMIN, json={"ids": [suggestion[low[0].id]]})
    assert again.json()["failed"] == [{"id": suggestion[low[0].id], "detail": "La sugerencia ya no está vigente."}]

    # The next water bill needs no model: the mapping is learned.
    llm.complete_json.reset_mock()
    nxt = await expense(service, world, merchant="AGUAS DE CARTAGENA", destination="7")
    assert (await run(service, llm))["auto_applied"] == 1 and llm.complete_json.await_count == 0
    assert budget_of(world, nxt.firefly_transaction_id) == "Agua"

    # Discard the other one.
    discarded = await client.post(f"{API}/budgets/suggestions/{suggestion[other.id]}/discard", headers=ADMIN)
    assert discarded.status_code == 200 and discarded.json()["status"] == "discarded"
    assert (await client.post(f"{API}/budgets/suggestions/{suggestion[other.id]}/discard", headers=ADMIN)).json()["code"] == "suggestion_not_pending"
    assert (await client.post(f"{API}/budgets/suggestions/9999/discard", headers=ADMIN)).status_code == 404
    assert (await audits(db, "budget_assignment.discard"))[0].detail["budget"] == "Agua"
    assert (await pending_suggestions(client))["total"] == 0
    assert (await client.get(f"{API}/budgets/suggestions", headers=ADMIN, params={"status": "discarded"})).json()["total"] == 1
    assert firefly.update_transaction.await_count >= 4


async def test_applying_a_suggestion_whose_budget_vanished_fails_cleanly(client, service, world, llm, db):
    item_id = (await expense(service, world)).id
    llm.complete_json.return_value = answer("Mercado", 0.5)
    await run(service, llm)
    suggestion_id = (await pending_suggestions(client))["items"][0]["id"]
    world.ledger.budgets.pop("12")
    response = await client.post(f"{API}/budgets/suggestions/apply", headers=ADMIN, json={"ids": [suggestion_id]})
    assert response.json()["applied"] == [] and response.json()["failed"][0]["id"] == suggestion_id
    assert (await assignment(db, item_id)).status == "pending"


async def test_pending_suggestions_of_items_that_left_become_stale(client, service, world, llm, db):
    item = await expense(service, world)
    llm.complete_json.return_value = answer("Mercado", 0.5)
    await run(service, llm)
    saved = await db.get(InboxItem, item.id)
    saved.status = "voided"
    await db.commit()
    assert (await pending_suggestions(client))["total"] == 0
    assert (await client.get(f"{API}/budgets/suggestions", headers=ADMIN, params={"status": "stale"})).json()["total"] == 1


async def teach(client, world, group, budget):
    response = await client.post(f"{API}/ledger/bulk", headers=ADMIN, json={"ids": [group], "set": {"budget": budget}})
    assert response.status_code == 200 and response.json()["failed"] == [], response.text


async def test_two_equal_owner_assignments_teach_the_merchant_and_a_contradiction_unlearns_it(client, service, world, db):
    groups = [world.ledger.seed(destination="2", category_name="Alimentación") for _ in range(4)]
    await teach(client, world, groups[0], "Mercado")
    row = await db.scalar(select(BudgetMap).where(BudgetMap.kind == "merchant"))
    assert (row.key, row.budget, row.streak, row.learned) == ("SUPERMERCADO CENTRAL", "Mercado", 1, False)
    await teach(client, world, groups[1], "Mercado")
    row = await db.scalar(select(BudgetMap).where(BudgetMap.kind == "merchant"), execution_options={"populate_existing": True})
    assert (row.streak, row.learned) == (2, True)
    assert (await audits(db, "budget_map.learn"))[0].detail["budget"] == "Mercado"
    category = await db.scalar(select(BudgetMap).where(BudgetMap.kind == "category"), execution_options={"populate_existing": True})
    assert (category.key, category.streak, category.learned) == ("alimentación", 2, False)  # categories need three
    await teach(client, world, groups[2], "Ocio")  # contradicts the learned merchant mapping
    row = await db.scalar(select(BudgetMap).where(BudgetMap.kind == "merchant"), execution_options={"populate_existing": True})
    assert (row.budget, row.streak, row.learned) == ("Ocio", 1, False)
    unlearn = (await audits(db, "budget_map.unlearn"))[0].detail
    assert unlearn["budget"] == "Mercado" and unlearn["contradicted_by"] == "Ocio"
    # Changing something else, or the same budget again, teaches nothing.
    before = row.streak
    await teach(client, world, groups[2], "Ocio")  # the same budget again: no change, nothing to learn
    row = await db.scalar(select(BudgetMap).where(BudgetMap.kind == "merchant"), execution_options={"populate_existing": True})
    assert row.streak == before


async def test_a_failure_while_learning_never_fails_the_ledger_edit(client, world, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(budget_assign, "learn_budget_correction", broken)
    group = world.ledger.seed(destination="2")
    await teach(client, world, group, "Mercado")
    assert world.ledger.journals(group)[0]["budget_name"] == "Mercado"


# --- the month sweep --------------------------------------------------------------------------------------------------------------


async def test_month_sweep_decides_the_expenses_of_the_month_without_a_budget(client, service, world, llm, db, background):
    await ai_budget.set_policy(db, 0)  # an owner's request is not braked
    await db.commit()
    a = await expense(service, world)
    manual = await expense(service, world, source="manual", source_ref="manual:7")
    decided = await expense(service, world)
    db.add(BudgetAssignment(item_id=decided.id, status="auto_applied", budget="Agua", month=CURRENT))
    nothing = await expense(service, world)
    db.add(BudgetAssignment(item_id=nothing.id, status="none", month=CURRENT))
    last_year = await expense(service, world, occurred_at=now_utc() - timedelta(days=400))
    await db.commit()
    ids = {"a": a.id, "manual": manual.id, "nothing": nothing.id, "old": last_year.id}
    response = await client.post(f"{API}/budgets/assign", headers=ADMIN, params={"month": CURRENT})
    assert response.status_code == 202 and response.json() == {"queued": 3}
    await budgets_router._assign_task
    assert (await assignment(db, ids["a"])).status == "auto_applied" and (await assignment(db, ids["manual"])).status == "auto_applied"
    assert (await assignment(db, ids["nothing"])).status == "auto_applied" and await assignment(db, ids["old"]) is None
    usage = (await db.scalars(select(AiUsage).where(AiUsage.kind == "budget_assign"))).one()
    assert (usage.calls, usage.auto_calls) == (3, 0)
    assert (await audits(db, "budgets.assign"))[0].detail == {"month": CURRENT, "queued": 3}
    assert (await client.post(f"{API}/budgets/assign", headers=ADMIN, params={"month": CURRENT})).json() == {"queued": 0}
    assert (await client.post(f"{API}/budgets/assign", headers=ADMIN, params={"month": "mañana"})).json()["code"] == "invalid_month"


async def test_only_one_month_sweep_at_a_time(client, service, world, llm, db, background):
    import asyncio

    await expense(service, world)
    gate = asyncio.Event()

    async def slow(*args, **kwargs):
        await gate.wait()
        return answer()

    llm.complete_json.side_effect = slow
    assert (await client.post(f"{API}/budgets/assign", headers=ADMIN)).status_code == 202
    busy = await client.post(f"{API}/budgets/assign", headers=ADMIN)
    assert busy.status_code == 409 and busy.json()["code"] == "budget_assign_running"
    gate.set()
    await budgets_router._assign_task


async def test_the_post_cycle_batch_runs_the_budgets_right_after_the_categorization(service, world, llm, monkeypatch):
    order = []

    async def categorize(*args, **kwargs):
        order.append("categorize")
        return {}

    async def quiet(*args, **kwargs):
        return {}

    async def budgets(session, firefly, llm_, settings, **kwargs):
        order.append("budgets")
        return await real(session, firefly, llm_, settings, **kwargs)

    real = budget_assign.run_batch
    monkeypatch.setattr(ai_insights.ai_categorize, "run_batch", categorize)
    monkeypatch.setattr(ai_insights.budget_assign, "run_batch", budgets)
    monkeypatch.setattr(ai_insights.ai_anomalies, "explain_new", quiet)
    monkeypatch.setattr(ai_insights.ai_subscriptions, "run", quiet)
    monkeypatch.setattr(ai_insights.ai_narrative, "after_cycle", quiet)
    item = await expense(service, world)
    group = item.firefly_transaction_id
    results = await ai_insights.after_cycle(service.session, service.firefly, llm, service.settings)
    assert order == ["categorize", "budgets"] and results["budgets"]["auto_applied"] == 1
    assert budget_of(world, group) == "Agua"


def test_migration_matches_the_models_and_is_chained():
    spec = importlib.util.spec_from_file_location("budgets_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (module.revision, module.down_revision) == ("20261124_13", "20261124_12")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE inbox_items (id INTEGER PRIMARY KEY)"))
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        module.upgrade()
    inspector = sa.inspect(engine)
    for model in (BudgetMap, BudgetAssignment):
        assert {c["name"] for c in inspector.get_columns(model.__tablename__)} == {c.name for c in model.__table__.columns}
    with pytest.raises(RuntimeError):
        module.downgrade()
    engine.dispose()


# --- PSE ---------------------------------------------------------------------------------------------------------------------------


PSE_TEXT = """Comprobante de pago - PSE (ACH Colombia)
Estado de la transacción: {state}
Empresa: AGUAS DE CARTAGENA S.A. E.S.P. NIT 890.399.011-3
Banco: DALE
Valor de la transacción: $ 85.400,00
Fecha: 2026-11-23 09:15:02
CUS: 1234567890
Referencia: 5566778
Si su pago fue rechazado, comuníquese con su entidad financiera."""


def pse_extraction(**changes):
    values = dict(
        kind="withdrawal", amount=Decimal("85400.00"), currency="COP", merchant="Aguas de Cartagena", account_hint="Dale",
        template_id="llm:test", summary="Pago PSE a Aguas de Cartagena por $85.400 el 2026-11-23. CUS 1234",
    )
    return Extraction(**{**values, **changes})


@pytest_asyncio.fixture
async def pse_env(monkeypatch, service, clients, add_own_account, world):
    await add_own_account("Dale", bank="dale", kind="wallet", aliases=["dale"], sources=["email:pse.com.co", "app:com.davivienda.dale"])

    def parse(text, subject, sender):
        if "Pagaste" in text:  # the Dale notification has a template; the PSE e-mail goes to the model
            return Extraction(kind="withdrawal", amount=Decimal("85400.00"), merchant=NOTIFICATION_MERCHANT[0], template_id="dale.pse")
        return None

    NOTIFICATION_MERCHANT[0] = "PSE"
    monkeypatch.setattr(pipeline, "parse", parse)
    clients[1].analyze.return_value = pse_extraction()
    return service


NOTIFICATION_MERCHANT = ["PSE"]


async def pse_email(service, state="Aprobada", hours=1, ref=None, sender="notificaciones@pse.com.co"):
    row = await service.inbox.add(
        source="email", source_ref=ref or "email:" + uuid4().hex, sender=sender, subject_or_title="Comprobante de pago PSE",
        raw_text=PSE_TEXT.format(state=state), occurred_at=now_utc() - timedelta(hours=hours), status="pending",
    )
    await service.session.commit()
    return row


async def dale_notification(service, hours=1):
    row = await service.inbox.add(
        source="notification", source_ref="notification:" + uuid4().hex, sender="", app="com.davivienda.dale", subject_or_title="Dale",
        raw_text="Pagaste $85.400 por PSE desde tu cuenta Dale", occurred_at=now_utc() - timedelta(hours=hours), status="pending",
    )
    await service.session.commit()
    return row


def movements(world):
    return [group for group in world.ledger.groups.values() if group["attributes"]["transactions"][0]["type"] == "withdrawal"]


def test_pse_text_helpers():
    assert pse.is_pse("Comprobante de pago PSE") and pse.is_pse("Pagos Seguros en Línea") and pse.is_pse("ACH Colombia")
    assert not pse.is_pse("Compraste en EXITO con tarjeta") and not pse.is_pse(None) and not pse.is_pse("tipse")
    approved = PSE_TEXT.format(state="Aprobada")
    assert pse.status(approved) == "approved"  # the labelled state wins over the footer that says «rechazado»
    assert pse.status(PSE_TEXT.format(state="Rechazada")) == "rejected"
    assert pse.status(PSE_TEXT.format(state="Pendiente")) == "pending"
    assert pse.status(PSE_TEXT.format(state="Fallida")) == "rejected" and pse.status(PSE_TEXT.format(state="Exitosa")) == "approved"
    assert pse.status("Estado: En proceso de confirmación") == "pending"
    assert pse.status("Su pago no fue aprobado") == "rejected" and pse.status("Pago aprobado por su banco") == "approved"
    assert pse.status("Hola") is None
    assert pse.check(PSE_TEXT.format(state="Rechazada"), "withdrawal") == ["PSE payment was not approved"]
    assert pse.check(PSE_TEXT.format(state="Pendiente"), "withdrawal") == ["PSE payment is pending"]
    assert pse.check(PSE_TEXT.format(state="Rechazada"), "ignore") == [] and pse.check(PSE_TEXT.format(state="Pendiente"), "review") == []
    assert pse.check(approved, "withdrawal") == [] and pse.check("Compra rechazada en EXITO", "withdrawal") == []


def test_pse_entity_cleaning_and_generic_names():
    assert pse.clean_entity("Aguas de Cartagena S.A. E.S.P. NIT 890.399.011-3") == "AGUAS DE CARTAGENA"
    assert pse.clean_entity("EMPRESAS PUBLICAS DE MEDELLIN E.S.P.") == "EMPRESAS PUBLICAS DE MEDELLIN"
    assert pse.clean_entity("Claro Colombia SAS") == "CLARO COLOMBIA" and pse.clean_entity("Gases del Caribe LTDA, NIT: 800.1") == "GASES DEL CARIBE"
    assert pse.clean_entity("S.A.S") != "" and pse.clean_entity(None) == ""
    assert pse.has_entity("AGUAS DE CARTAGENA") and not pse.has_entity("PSE") and not pse.has_entity("Pago PSE") and not pse.has_entity(None) and not pse.has_entity("")


def test_pse_tag_marks_withdrawals_only():
    item = type("Item", (), {"transaction_type": "withdrawal", "tags": ["viaje"], "merchant": "Aguas de Cartagena S.A. E.S.P. NIT 890.399.011-3"})()
    assert pse.tag(item, "Comprobante PSE") is True and item.tags == ["viaje", "pse"] and item.merchant == "AGUAS DE CARTAGENA"
    assert pse.tag(item, "Comprobante PSE") is True and item.tags == ["viaje", "pse"]
    deposit = type("Item", (), {"transaction_type": "deposit", "tags": None, "merchant": "X"})()
    assert pse.tag(deposit, "PSE") is False and deposit.tags is None and pse.tag(item, "Compra en EXITO") is False


async def test_the_pse_duplicate_window_is_two_days_and_needs_another_channel(service, world):
    notification = await expense(service, world, merchant="PSE", amount="85400.00", source="notification", asset_account="Dale", tags=["pse"], occurred_at=now_utc() - timedelta(hours=30))
    email = await service.inbox.add(
        source="email", source_ref="email:1", sender="notificaciones@pse.com.co", status="pending", amount=Decimal("85400.00"), currency="COP",
        transaction_type="withdrawal", asset_account="Dale", merchant="AGUAS DE CARTAGENA", tags=["pse"], occurred_at=now_utc() - timedelta(hours=1),
    )
    repo = service.inbox
    assert (await repo.find_duplicate(email, 60)).id == notification.id  # 29 hours apart: only the PSE window sees it
    email.occurred_at = now_utc() + timedelta(days=2, hours=1)
    assert await repo.find_duplicate(email, 60) is None  # beyond two days
    email.occurred_at = now_utc() - timedelta(hours=1)
    email.asset_account = "Nequi"
    assert await repo.find_duplicate(email, 60) is None  # another own account
    email.asset_account, email.amount = "Dale", Decimal("85401.00")
    assert await repo.find_duplicate(email, 60) is None  # another amount
    email.amount = Decimal("85400.00")
    email.tags = []
    assert (await repo.find_duplicate(email, 60)).id == notification.id  # the earlier one is PSE: enough
    notification.tags = []
    await service.session.commit()
    assert await repo.find_duplicate(email, 60) is None  # neither is PSE: the normal 60 minutes apply
    email.tags = ["pse"]
    assert (await repo.find_duplicate(email, 60)).id == notification.id  # the new one is PSE: enough


async def test_pse_receipt_and_bank_e_mail_are_one_payment_but_two_pse_receipts_from_one_sender_are_two(service, world):
    first = await expense(service, world, merchant="AGUAS DE CARTAGENA", amount="85400.00", source="email", sender="notificaciones@pse.com.co", asset_account="Dale", tags=["pse"])
    same_sender = await service.inbox.add(
        source="email", source_ref="email:2", sender="notificaciones@pse.com.co", status="pending", amount=Decimal("85400.00"), currency="COP",
        transaction_type="withdrawal", asset_account="Dale", merchant="AGUAS DE CARTAGENA", tags=["pse"], occurred_at=now_utc(),
    )
    bank_email = await service.inbox.add(
        source="email", source_ref="email:3", sender="alertas@dale.com.co", status="pending", amount=Decimal("85400.00"), currency="COP",
        transaction_type="withdrawal", asset_account="Dale", merchant="PSE", occurred_at=now_utc(),
    )
    assert await service.inbox.find_duplicate(same_sender, 60) is None  # same channel and sender: never merged
    assert (await service.inbox.find_duplicate(bank_email, 60)).id == first.id


async def test_identical_purchases_of_other_channels_beyond_the_normal_window_stay_separate(service, world):
    await expense(service, world, amount="9000.00", source="sms", asset_account="Nequi", occurred_at=now_utc() - timedelta(hours=20))
    other = await service.inbox.add(
        source="notification", source_ref="n:1", status="pending", amount=Decimal("9000.00"), currency="COP", transaction_type="withdrawal",
        asset_account="Nequi", merchant="SUPERMERCADO CENTRAL", occurred_at=now_utc(),
    )
    assert await service.inbox.find_duplicate(other, 60) is None


async def test_an_approved_pse_receipt_registers_on_the_paying_bank_with_the_budget(pse_env, world, llm, db, clients):
    service = pse_env
    item = await pse_email(service)
    item_id = item.id
    await service.process(item)
    saved = await service.inbox.get(item_id)
    assert saved.status == "created" and saved.asset_account == "Dale" and saved.merchant == "AGUAS DE CARTAGENA"
    assert saved.transaction_type == "withdrawal" and saved.amount == Decimal("85400.00") and "pse" in saved.tags
    assert saved.description.startswith("Pago PSE a Aguas de Cartagena")
    [journal] = world.ledger.journals(saved.firefly_transaction_id)
    assert (journal["source_name"], journal["destination_name"], journal["description"].startswith("Pago PSE a")) == ("Dale", "AGUAS DE CARTAGENA", True)
    assert "pse" in journal["tags"] and "auto" in journal["tags"]
    prompt_text = clients[1].analyze.await_args.args[0]
    assert "NIT 890.399.011-3" not in prompt_text and "[ID]" in prompt_text and "1234567890" not in prompt_text  # redacted before the provider

    llm.complete_json.return_value = answer("Agua", 0.93)
    result = await budget_assign.run_batch(service.session, service.firefly, llm, service.settings)
    assert result["auto_applied"] == 1
    assert budget_of(world, saved.firefly_transaction_id) == "Agua"
    evidence = sent(llm)["UNTRUSTED_DATA"]["item"]
    assert evidence["merchant"] == "AGUAS DE CARTAGENA" and evidence["description"].startswith("Pago PSE a")
    assert sent(llm)["CONTEXT"]["budgets"] == ["Agua", "Mercado"]


async def test_a_rejected_pse_receipt_is_ignored_and_a_pending_one_goes_to_review(pse_env, world, clients):
    service = pse_env
    clients[1].analyze.return_value = Extraction(kind="ignore", reason="Pago rechazado por PSE")
    rejected = await pse_email(service, "Rechazada")
    await service.process(rejected)
    assert (await service.inbox.get(rejected.id)).status == "ignored" and movements(world) == []
    clients[1].analyze.return_value = pse_extraction(kind="review", amount=None, reason="Pago pendiente")
    pending = await pse_email(service, "Pendiente")
    await service.process(pending)
    assert (await service.inbox.get(pending.id)).status == "needs_review" and movements(world) == []


async def test_a_receipt_whose_state_says_otherwise_never_registers_even_if_the_model_says_withdrawal(pse_env, world):
    service = pse_env
    for state, reason in (("Rechazada", "PSE payment was not approved"), ("Pendiente", "PSE payment is pending")):
        row = await pse_email(service, state)
        await service.process(row)
        saved = await service.inbox.get(row.id)
        assert saved.status == "needs_review" and reason in saved.review_reason
    assert movements(world) == []


async def test_the_paying_bank_is_resolved_from_the_bank_name_when_several_accounts_share_the_pse_sender(pse_env, world, clients, add_own_account):
    service = pse_env
    await add_own_account("Bancolombia", bank="bancolombia", kind="savings", aliases=["bancolombia"], sources=["email:pse.com.co"])
    for hint, expected in (("Dale", "Dale"), ("Banco Dale", "Dale"), ("Bancolombia", "Bancolombia"), ("Nu", None)):
        clients[1].analyze.return_value = pse_extraction(account_hint=hint)
        row = await pse_email(service)
        await service.process(row)
        saved = await service.inbox.get(row.id)
        assert saved.asset_account == expected, hint
        assert saved.status == ("created" if expected else "needs_review")
        assert expected or "no unambiguous own account" in saved.review_reason


async def test_dale_notification_then_pse_receipt_leave_one_movement_with_the_paid_entity(pse_env, world):
    service = pse_env
    notification = await dale_notification(service, hours=30)
    await service.process(notification)
    first = await service.inbox.get(notification.id)
    assert first.status == "created" and first.merchant == "PSE" and first.asset_account == "Dale" and "pse" in first.tags
    old_group = first.firefly_transaction_id
    receipt = await pse_email(service, hours=1)
    await service.process(receipt)
    saved_receipt, saved_notification = await service.inbox.get(receipt.id), await service.inbox.get(notification.id)
    assert saved_receipt.status == "created" and saved_receipt.merchant == "AGUAS DE CARTAGENA"
    assert saved_notification.status == "voided" and old_group not in world.ledger.groups
    assert len(movements(world)) == 1
    assert world.ledger.journals(saved_receipt.firefly_transaction_id)[0]["destination_name"] == "AGUAS DE CARTAGENA"


async def test_pse_receipt_then_dale_notification_keeps_the_receipt(pse_env, world):
    service = pse_env
    receipt = await pse_email(service, hours=30)
    await service.process(receipt)
    kept = await service.inbox.get(receipt.id)
    assert kept.status == "created"
    notification = await dale_notification(service, hours=1)
    await service.process(notification)
    late = await service.inbox.get(notification.id)
    assert late.status == "duplicate" and late.duplicate_of_id == receipt.id
    assert len(movements(world)) == 1 and kept.firefly_transaction_id in world.ledger.groups


async def test_when_both_name_the_entity_the_first_registered_stays(pse_env, world):
    service = pse_env
    NOTIFICATION_MERCHANT[0] = "AGUAS DE CARTAGENA"
    notification = await dale_notification(service, hours=30)
    await service.process(notification)
    receipt = await pse_email(service, hours=1)
    await service.process(receipt)
    assert (await service.inbox.get(notification.id)).status == "created"
    saved = await service.inbox.get(receipt.id)
    assert saved.status == "duplicate" and saved.duplicate_of_id == notification.id
    assert len(movements(world)) == 1


async def test_two_receipts_of_two_payments_from_the_same_sender_both_register(pse_env, world):
    service = pse_env
    for _ in range(2):
        row = await pse_email(service)
        await service.process(row)
        assert (await service.inbox.get(row.id)).status == "created"
    assert len(movements(world)) == 2
