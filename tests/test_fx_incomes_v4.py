"""TRM from a simulated SODA service (cache, stale fallback, daily job), USD movements converted with the TRM of their day,
fixed and extra incomes, the ``extra_income`` alert and ``GET /plan/month``."""
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from app.clients.trm import TrmClient
from app.db.models import InboxEvent
from app.db.models_alerts import Alert
from app.db.models_debts import Debt, FxRate
from app.services import debts, fx
from app.services import processor as pipeline
from tests.test_devices_v4 import API, auth, enroll_key

ADMIN = auth("test-admin-key-01234567890123456789")
ZONE = ZoneInfo("America/Bogota")


class Soda:
    """datos.gov.co SODA stand-in: rows ``(valor, desde, hasta)``; answers the two queries the client makes and records them."""

    def __init__(self, rows):
        self.rows, self.calls, self.online = list(rows), [], True

    def row(self, value, since, until):
        return {"valor": value, "unidad": "COP", "vigenciadesde": f"{since}T00:00:00.000", "vigenciahasta": f"{until}T00:00:00.000"}

    def handler(self, request):
        self.calls.append(dict(request.url.params))
        if not self.online:
            raise httpx.ConnectError("offline", request=request)
        where = request.url.params.get("$where")
        found = sorted(self.rows, key=lambda r: r[1], reverse=True)
        if where:
            day = re.search(r"vigenciadesde <= '(\d{4}-\d{2}-\d{2})", where)[1]
            found = [r for r in found if r[1] <= day <= r[2]]
        limit = int(request.url.params.get("$limit", 1000))
        return httpx.Response(200, json=[self.row(*r) for r in found[:limit]])


@pytest.fixture
def soda(monkeypatch):
    service = Soda([("3950.50", "2026-11-20", "2026-11-23"), ("3940.25", "2026-11-24", "2026-11-24")])
    monkeypatch.setattr(fx, "new_client", lambda config: TrmClient(config.trm_base_url, config.trm_timeout_seconds, httpx.MockTransport(service.handler)))
    return service


@pytest.fixture
def firefly(clients):
    ff = clients[0]
    ff.accounts.return_value = [*ff.accounts.return_value, {"id": "20", "name": "Credito Libre", "type": "liabilities"}]
    ff.account_balance.return_value = {"type": "liabilities", "balance": Decimal("-5000000.00")}
    ff.budget_progress.return_value = [{"name": "Comida", "spent": Decimal("100000.00"), "limit": Decimal("800000.00")}]
    return ff


async def trm(client, day=None):
    return await client.get(f"{API}/fx/trm", headers=ADMIN, params={"date": day} if day else None)


# --- TRM --------------------------------------------------------------------------------------------------------------

async def test_trm_is_read_from_soda_and_cached(client, soda, db):
    response = await trm(client, "2026-11-24")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["date"], body["rate"], body["source"], body["stale"]) == ("2026-11-24", "3940.25", "datos.gov.co", False)
    assert datetime.fromisoformat(body["fetched_at"]).tzinfo is not None
    assert len(soda.calls) == 1 and "vigenciadesde <= '2026-11-24T00:00:00.000'" in soda.calls[0]["$where"]
    assert (await trm(client, "2026-11-24")).json()["rate"] == "3940.25" and len(soda.calls) == 1  # cache
    # Friday's rate governs the weekend: stored once with its validity, so Sunday needs no new request
    assert (await trm(client, "2026-11-20")).json()["rate"] == "3950.50"
    assert (await trm(client, "2026-11-22")).json()["rate"] == "3950.50" and len(soda.calls) == 2
    row = await db.get(FxRate, date(2026, 11, 20))
    assert (row.valid_until, row.rate, row.source) == (date(2026, 11, 23), Decimal("3950.5000"), "datos.gov.co")


async def test_without_network_the_last_known_rate_is_stale(client, soda):
    await trm(client, "2026-11-24")
    soda.online = False
    old = await trm(client, "2026-11-24")
    assert old.json()["stale"] is False  # still inside its validity: the cache answers, no request is made
    stale = (await trm(client, "2026-12-01")).json()
    assert (stale["rate"], stale["stale"], stale["date"]) == ("3940.25", True, "2026-12-01")


async def test_without_network_and_without_cache_it_is_a_503(client, soda):
    soda.online = False
    response = await trm(client, "2026-11-24")
    assert response.status_code == 503 and response.json()["code"] == "trm_unavailable"
    soda.online = True
    soda.rows.clear()
    assert (await trm(client, "2026-11-24")).status_code == 503  # the dataset has nothing and nothing is cached


async def test_trm_defaults_to_today_and_validates_the_date(client, soda, monkeypatch):
    monkeypatch.setattr(debts, "today", lambda settings: date(2026, 11, 24))
    assert (await trm(client)).json()["date"] == "2026-11-24"
    assert (await client.get(f"{API}/fx/trm", headers=ADMIN, params={"date": "nope"})).status_code == 422


async def test_daily_job_stores_the_latest_rates_and_survives_an_outage(db, settings, soda):
    assert await fx.refresh(db, settings) == {"stored": 2, "latest": "2026-11-24"}
    assert {r.valid_from for r in (await db.scalars(select(FxRate))).all()} == {date(2026, 11, 20), date(2026, 11, 24)}
    soda.online = False
    assert await fx.refresh(db, settings) == {"stored": 0, "error": "unavailable"}
    assert len((await db.scalars(select(FxRate))).all()) == 2  # the cache keeps serving


async def test_the_job_is_scheduled_daily():
    from app.api import routes

    assert callable(routes.run_fx_trm)
    import inspect

    from app import main

    assert '"fx_trm": (routes.run_fx_trm, "0 8 * * *")' in inspect.getsource(main.lifespan)


# --- USD movements ----------------------------------------------------------------------------------------------------

async def pending(service, **changes):
    values = dict(source="email", source_ref=str(uuid4()), sender="alertas@nequi.com.co", raw_text="Pagaste", subject_or_title="Pago",
                  occurred_at=datetime(2026, 11, 22, 15, 0, tzinfo=UTC), status="pending")
    values.update(changes)
    row = await service.inbox.add(**values)
    await service.session.commit()
    return row


def parsed(extraction, monkeypatch, **changes):
    changed = replace(extraction, **changes)
    monkeypatch.setattr(pipeline, "parse", lambda *args, **kwargs: changed)


async def test_usd_movement_is_converted_with_the_trm_of_its_day(service, firefly, extraction, soda, monkeypatch, db):
    parsed(extraction, monkeypatch, currency="USD", amount=Decimal("20.00"), merchant="STEAM")
    item = await service.process(await pending(service))  # Sunday 22nd: Friday's TRM 3950.50
    assert item.status == "created", item.review_reason
    assert (item.amount, item.currency, item.foreign_amount, item.foreign_currency) == (Decimal("79010.00"), "COP", Decimal("20.00"), "USD")
    split = firefly.create_transaction.await_args.args[0]["transactions"][0]
    assert (split["amount"], split["currency_code"], split["foreign_amount"], split["foreign_currency_code"]) == ("79010.00", "COP", "20.00", "USD")
    events = (await db.scalars(select(InboxEvent).where(InboxEvent.inbox_id == item.id, InboxEvent.action == "fx_converted"))).all()
    assert [e.detail for e in events] == ["20.00 USD x 3950.50 (TRM 2026-11-20) = 79010.00 COP"]


async def test_the_amount_cap_applies_to_the_converted_amount(service, extraction, soda, monkeypatch):
    parsed(extraction, monkeypatch, currency="USD", amount=Decimal("600.00"), merchant="STEAM")  # 600 x 3950.50 > 2.000.000
    item = await service.process(await pending(service))
    assert item.status == "needs_review" and "amount over auto-approval limit" in item.review_reason
    assert (item.amount, item.currency) == (Decimal("2370300.00"), "COP")


async def test_usd_movement_without_a_trm_goes_to_review_as_before(service, firefly, extraction, soda, monkeypatch):
    soda.online = False
    parsed(extraction, monkeypatch, currency="USD", amount=Decimal("20.00"), merchant="STEAM")
    item = await service.process(await pending(service))
    assert item.status == "needs_review" and "foreign currency requires confirmed COP amount" in item.review_reason
    assert (item.amount, item.currency, item.foreign_amount) == (Decimal("20.00"), "USD", None)
    firefly.create_transaction.assert_not_awaited()


async def test_usd_movement_uses_the_previous_trm_but_not_a_very_old_one(service, extraction, soda, monkeypatch):
    soda.rows[:] = [("3900.00", "2026-11-13", "2026-11-13")]  # published only for the 13th
    parsed(extraction, monkeypatch, currency="USD", amount=Decimal("10.00"), merchant="STEAM")
    near = await service.process(await pending(service, occurred_at=datetime(2026, 11, 16, 15, 0, tzinfo=UTC)))  # 3 days later
    assert (near.status, near.amount) == ("created", Decimal("39000.00"))
    far = await service.process(await pending(service, occurred_at=datetime(2026, 11, 22, 15, 0, tzinfo=UTC)))  # 9 days later
    assert far.status == "needs_review" and far.currency == "USD"


async def test_cop_movements_never_touch_the_trm(service, extraction, soda, monkeypatch):
    parsed(extraction, monkeypatch)
    assert (await service.process(await pending(service))).status == "created"
    assert soda.calls == []


# --- incomes ----------------------------------------------------------------------------------------------------------

SALARY = {"name": "Salario", "kind": "fixed", "currency": "COP", "amount": "1500000.00", "day_of_month": 30, "match": ["EMPRESA SAS"]}
RETAINER = {"name": "Cliente USD", "kind": "fixed", "currency": "USD", "amount": "100.00", "day_of_month": 5, "match": ["PAYONEER"]}
FREELANCE = {"name": "Freelance", "kind": "variable", "currency": "COP", "amount": "400000.00", "day_of_month": None, "match": ["CLIENTE X"]}


async def test_income_crud_and_scopes(client):
    created = await client.post(f"{API}/incomes", headers=ADMIN, json=SALARY)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body == {"id": 1, "name": "Salario", "kind": "fixed", "currency": "COP", "amount": "1500000.00", "day_of_month": 30,
                    "match": ["EMPRESA SAS"], "active": True}
    updated = await client.put(f"{API}/incomes/1", headers=ADMIN, json={**SALARY, "amount": "1600000.00"})
    assert updated.json()["amount"] == "1600000.00"
    assert (await client.get(f"{API}/incomes", headers=ADMIN)).json() == {"items": [updated.json()]}
    for changes in ({"kind": "x"}, {"currency": "EUR"}, {"amount": "0"}, {"day_of_month": 32}, {"unknown": 1}):
        assert (await client.post(f"{API}/incomes", headers=ADMIN, json={**SALARY, **changes})).status_code == 422

    reader = auth((await enroll_key(client, ["read"], "reader"))["key"])
    ingest = auth((await enroll_key(client, ["ingest"], "capture"))["key"])
    assert (await client.get(f"{API}/incomes", headers=reader)).status_code == 200
    assert (await client.get(f"{API}/incomes", headers=ingest)).status_code == 403
    for method, url in (("POST", f"{API}/incomes"), ("PUT", f"{API}/incomes/1"), ("DELETE", f"{API}/incomes/1")):
        assert (await client.request(method, url, headers=reader, json=SALARY)).status_code == 403
    assert (await client.get(f"{API}/fx/trm", headers=ingest)).status_code == 403
    assert (await client.get(f"{API}/plan/month", headers=ingest)).status_code == 403

    assert (await client.delete(f"{API}/incomes/1", headers=ADMIN)).json() == {"deleted": True}
    assert (await client.get(f"{API}/incomes", headers=ADMIN)).json() == {"items": []}
    assert (await client.put(f"{API}/incomes/1", headers=ADMIN, json={"name": "x"})).json()["code"] == "income_not_found"


async def deposit(service, extraction, monkeypatch, merchant, amount):
    parsed(extraction, monkeypatch, kind="deposit", merchant=merchant, amount=Decimal(amount))
    item = await service.process(await pending(service, occurred_at=datetime.now(UTC)))
    assert item.status == "created", item.review_reason
    return item


async def alerts(db):
    return list((await db.scalars(select(Alert).where(Alert.type == "extra_income"))).all())


async def test_a_deposit_matching_a_fixed_income_is_fixed_and_raises_no_alert(client, service, firefly, extraction, monkeypatch, db):
    await client.post(f"{API}/incomes", headers=ADMIN, json=SALARY)
    await deposit(service, extraction, monkeypatch, "EMPRESA SAS", "1500000.00")
    assert await alerts(db) == []
    month = datetime.now(ZONE).strftime("%Y-%m")
    plan = (await client.get(f"{API}/plan/month", headers=ADMIN, params={"month": month})).json()
    assert plan["income_received"] == {"fixed": "1500000.00", "variable": "0.00", "total": "1500000.00"}


async def test_an_unmatched_deposit_above_the_threshold_is_an_extra_with_an_alert_and_a_suggestion(client, service, firefly, extraction, monkeypatch, db):
    db.add(Debt(kind="loan", name="Credito Libre", firefly_account_id="20", firefly_account="Credito Libre", rate_ea=Decimal("28"),
                installment=Decimal("397290"), payment_day=5, payment_match=[], active=True))
    await db.commit()
    item = await deposit(service, extraction, monkeypatch, "AMIGO PEREZ", "300000.00")
    [alert] = await alerts(db)
    assert (alert.title, alert.item_id, alert.amount, alert.currency) == ("Ingreso extra recibido", item.id, Decimal("300000.00"), "COP")
    assert "$300.000 COP" in alert.body and "Sugerencia" in alert.body and "«Credito Libre»" in alert.body
    # the same movement never raises it twice
    await service._finish(item, "pending")
    assert len(await alerts(db)) == 1
    plan = (await client.get(f"{API}/plan/month", headers=ADMIN)).json()
    assert plan["income_received"]["variable"] == "300000.00" and plan["extra_received_unallocated"] == "300000.00"


async def test_small_unmatched_deposits_are_neither_fixed_nor_extra(service, firefly, extraction, monkeypatch, db, client):
    await deposit(service, extraction, monkeypatch, "AMIGO PEREZ", "200000.00")  # equal to the threshold is not above it
    await deposit(service, extraction, monkeypatch, "TIENDA", "150000.00")
    assert await alerts(db) == []
    assert (await client.get(f"{API}/plan/month", headers=ADMIN)).json()["income_received"]["total"] == "0.00"


async def test_the_threshold_is_a_runtime_setting(client, service, firefly, extraction, monkeypatch, db):
    assert (await client.get(f"{API}/settings/extra-income", headers=ADMIN)).json() == {"amount_cop": "200000.00", "default_cop": "200000.00"}
    changed = await client.put(f"{API}/settings/extra-income", headers=ADMIN, json={"amount_cop": "100000.00"})
    assert changed.json()["amount_cop"] == "100000.00"
    await deposit(service, extraction, monkeypatch, "TIENDA", "150000.00")
    assert len(await alerts(db)) == 1
    assert (await client.put(f"{API}/settings/extra-income", headers=ADMIN, json={"amount_cop": "0"})).status_code == 422
    reader = auth((await enroll_key(client, ["read"], "reader"))["key"])
    assert (await client.get(f"{API}/settings/extra-income", headers=reader)).status_code == 200
    assert (await client.put(f"{API}/settings/extra-income", headers=reader, json={"amount_cop": "5"})).status_code == 403


async def test_a_deposit_matching_a_variable_income_is_variable_whatever_its_size(client, service, firefly, extraction, monkeypatch, db):
    await client.post(f"{API}/incomes", headers=ADMIN, json=FREELANCE)
    await deposit(service, extraction, monkeypatch, "CLIENTE X", "50000.00")
    assert len(await alerts(db)) == 1
    assert (await client.get(f"{API}/plan/month", headers=ADMIN)).json()["income_received"]["variable"] == "50000.00"


# --- plan of the month ------------------------------------------------------------------------------------------------

async def test_month_plan_adds_fixed_income_usd_at_the_trm_and_subtracts_budget_and_installments(client, service, firefly, extraction, soda, monkeypatch, db):
    today = datetime.now(ZONE).date()
    soda.rows[:] = [("4000.00", (today - timedelta(days=10)).isoformat(), (today + timedelta(days=10)).isoformat())]
    for income in (SALARY, RETAINER, FREELANCE):
        await client.post(f"{API}/incomes", headers=ADMIN, json=income)
    db.add(Debt(kind="loan", name="Credito Libre", firefly_account_id="20", firefly_account="Credito Libre", rate_ea=Decimal("28"),
                installment=Decimal("397290"), insurance_monthly=Decimal("15000"), payment_day=5, payment_match=[], active=True))
    await db.commit()
    await deposit(service, extraction, monkeypatch, "EMPRESA SAS", "1500000.00")
    await deposit(service, extraction, monkeypatch, "AMIGO PEREZ", "300000.00")

    response = await client.get(f"{API}/plan/month", headers=ADMIN, params={"month": today.strftime("%Y-%m")})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["month"] == today.strftime("%Y-%m")
    # expected counts only FIXED incomes: 1.500.000 COP + 100 USD x 4.000 = 1.900.000
    assert plan["income_expected"] == {"fixed_cop": "1500000.00", "fixed_usd": {"usd": "100.00", "rate": "4000.00", "cop": "400000.00"}, "total": "1900000.00"}
    assert plan["income_received"] == {"fixed": "1500000.00", "variable": "300000.00", "total": "1800000.00"}
    assert plan["budget_total"] == "800000.00" and plan["installments_total"] == "412290.00"
    assert plan["available_for_extra"] == "687710.00"  # 1.900.000 - 800.000 - 412.290
    assert plan["extra_received_unallocated"] == "300000.00"
    suggestion = plan["suggestion"]
    assert suggestion["strategy"] in {"avalanche", "snowball"}
    assert [(a["debt_id"], a["amount"]) for a in suggestion["allocations"]] == [(1, "987710.00")]  # available + the extra
    assert suggestion["allocations"][0]["reason"]


async def test_month_plan_can_be_negative_and_validates_the_month(client, firefly, soda):
    await client.post(f"{API}/incomes", headers=ADMIN, json=SALARY)
    firefly.budget_progress.return_value = [{"name": "Todo", "spent": Decimal("0"), "limit": Decimal("2000000.00")}]
    plan = (await client.get(f"{API}/plan/month", headers=ADMIN)).json()
    assert plan["available_for_extra"] == "-500000.00" and plan["suggestion"] == {"strategy": "avalanche", "allocations": []}
    assert plan["income_expected"]["fixed_usd"] == {"usd": "0.00", "rate": "0.00", "cop": "0.00"}
    assert soda.calls == []  # no USD income, no TRM request
    for bad in ("2026-13", "2026-1", "nope"):
        assert (await client.get(f"{API}/plan/month", headers=ADMIN, params={"month": bad})).status_code == 422


async def test_month_plan_without_a_trm_counts_the_usd_income_as_zero(client, firefly, soda):
    soda.online = False
    await client.post(f"{API}/incomes", headers=ADMIN, json=RETAINER)
    plan = (await client.get(f"{API}/plan/month", headers=ADMIN)).json()
    assert plan["income_expected"]["fixed_usd"] == {"usd": "100.00", "rate": "0.00", "cop": "0.00"} and plan["income_expected"]["total"] == "0.00"
