"""Debts: CRUD over Firefly liabilities (simulated), schedule, plan, installment payments as transfers, scopes.

Firefly assumptions the client code makes (checked against the official API docs, see docs/contract-additions/DebtsIncomes.md):
a liability is ``POST accounts`` with ``type`` liability, ``liability_type`` loan/debt, ``liability_direction`` debit («I owe this»),
``interest`` + ``interest_period`` yearly and ``opening_balance`` (negative) with its date; the balance is ``current_balance``.
"""
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.clients.firefly import FireflyClient
from app.core.exceptions import PermanentError
from app.db.models import InboxEvent, OwnAccountRow
from app.db.models_debts import Debt
from app.services import debt_math as m
from app.services import debts
from tests.test_devices_v4 import API, auth, enroll_key

ADMIN = auth("test-admin-key-01234567890123456789")
TODAY = date(2026, 11, 24)
URL = f"{API}/debts"

ACCOUNTS = [
    {"id": "1", "name": "Nequi", "type": "asset"},
    {"id": "2", "name": "SUPERMERCADO CENTRAL", "type": "expense"},
    {"id": "4", "name": "Bancolombia", "type": "asset"},
    {"id": "9", "name": "Visa Oro", "type": "asset"},
]
LIABILITY = {"id": "20", "name": "Credito Libre", "type": "liabilities"}
LOAN = {
    "kind": "loan", "name": "Credito Libre", "lender": "Banco Uno", "balance": "10000000.00", "original_amount": "10000000.00",
    "rate_ea": "28.00", "term_months": 36, "start_date": "2026-11-05", "installment": "397290.00", "payment_day": 5,
    "insurance_monthly": "15000.00", "payment_match": ["BANCO UNO"],
}


@pytest.fixture(autouse=True)
def calendar(monkeypatch):
    monkeypatch.setattr(debts, "today", lambda settings: TODAY)


@pytest.fixture
def firefly(clients):
    ff = clients[0]
    ff.account_list = list(ACCOUNTS)
    ff.accounts.side_effect = lambda: list(ff.account_list)

    async def create_liability(*args):
        ff.account_list.append(LIABILITY)  # Firefly lists it from now on
        return LIABILITY

    ff.create_liability.side_effect = create_liability
    ff.balances = {"20": {"type": "liabilities", "balance": Decimal("-9810557.00")}, "9": {"type": "asset", "balance": Decimal("-2000000.00")}}
    ff.account_balance.side_effect = lambda account_id: ff.balances.get(str(account_id))
    return ff


async def make_loan(client, **changes):
    response = await client.post(URL, headers=ADMIN, json={**LOAN, **changes})
    assert response.status_code == 201, response.text
    return response.json()


# --- Firefly client ---------------------------------------------------------------------------------------------------

def http_client(handler):
    client = FireflyClient(SimpleNamespace(firefly_base_url="http://firefly", firefly_api_token=SecretStr("token")))
    client._client = httpx.AsyncClient(base_url="http://firefly/api/v1/", transport=httpx.MockTransport(handler))
    return client


async def test_create_liability_sends_the_liability_fields_firefly_documents():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"data": {"id": "20", "attributes": {"name": "Credito Libre", "type": "liabilities"}}})

    ff = http_client(handler)
    account = await ff.create_liability("Credito Libre", "loan", Decimal("28.5"), Decimal("10000000"), TODAY)
    assert account == {"id": "20", "name": "Credito Libre", "type": "liabilities"}
    method, path, body = seen[0]
    assert (method, path) == ("POST", "/api/v1/accounts")
    assert body == {
        "name": "Credito Libre", "type": "liability", "liability_type": "loan", "liability_direction": "debit",
        "interest": "28.50", "interest_period": "yearly", "opening_balance": "-10000000.00", "opening_balance_date": "2026-11-24",
        "currency_code": "COP", "active": True, "include_net_worth": True,
    }
    await ff.create_liability("Visa", "debt", Decimal("30"), Decimal("1"), TODAY)
    assert seen[1][2]["liability_type"] == "debt"
    with pytest.raises(PermanentError):
        await ff.create_liability("X", "mortgage", Decimal("1"), Decimal("1"), TODAY)
    # the merchant-only restriction of create_account is untouched
    with pytest.raises(PermanentError, match="Only merchant"):
        await ff.create_account("Credito", "liability")
    assert len(seen) == 2


async def test_update_account_and_balance_reading():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.content))
        if request.method == "GET" and request.url.path.endswith("/accounts/404"):
            return httpx.Response(404, json={"message": "Resource not found"})
        return httpx.Response(200, json={"data": {"id": "20", "attributes": {"name": "C", "type": "liabilities", "current_balance": "-9810557.00"}}})

    ff = http_client(handler)
    await ff.update_account("20", {"interest": "30.00", "interest_period": "yearly"})
    assert seen[0][:2] == ("PUT", "/api/v1/accounts/20") and json.loads(seen[0][2]) == {"interest": "30.00", "interest_period": "yearly"}
    assert await ff.account_balance("20") == {"type": "liabilities", "balance": Decimal("-9810557.00")}
    assert await ff.account_balance("404") is None


# --- CRUD -------------------------------------------------------------------------------------------------------------

async def test_create_loan_makes_the_liability_and_registers_the_own_account(client, firefly, db):
    body = await make_loan(client)
    firefly.create_liability.assert_awaited_once_with("Credito Libre", "loan", Decimal("28.00"), Decimal("10000000.00"), TODAY)
    assert body["balance"] == "9810557.00"  # read back from Firefly, not the opening balance sent
    assert (body["id"], body["kind"], body["firefly_account_id"], body["currency"], body["rate_ea"]) == (1, "loan", "20", "COP", "28.00")
    assert (body["installment"], body["insurance_monthly"], body["payment_day"], body["payment_match"], body["active"]) == (
        "397290.00", "15000.00", 5, ["BANCO UNO"], True)
    assert body["credit_limit"] is None and body["minimum_payment"] is None
    own = (await db.scalars(select(OwnAccountRow).where(OwnAccountRow.firefly_account == "Credito Libre"))).one()
    assert (own.kind, own.bank) == ("loan", "Banco Uno")

    listing = (await client.get(URL, headers=ADMIN)).json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == body["id"]
    assert listing["totals"] == {"balance": "9810557.00", "installments_monthly": "412290.00"}  # installment + insurance


async def test_create_validations(client, firefly):
    assert (await client.post(URL, headers=ADMIN, json={**LOAN, "balance": None})).json()["code"] == "balance_required"
    firefly.create_liability.assert_not_awaited()
    for changes in ({"rate_ea": "101"}, {"kind": "mortgage"}, {"payment_day": 32}, {"unknown": 1}, {"name": ""}, {"installment": "-1"}):
        response = await client.post(URL, headers=ADMIN, json={**LOAN, **changes})
        assert response.status_code == 422 and response.json()["code"] == "validation_error", changes
    await make_loan(client)
    again = await client.post(URL, headers=ADMIN, json={**LOAN, "firefly_account_id": "20"})
    assert again.status_code == 409 and again.json()["code"] == "debt_exists"
    firefly.account_list.append({"id": "21", "name": "Otro", "type": "liabilities"})
    taken = await client.post(URL, headers=ADMIN, json={**LOAN, "name": "Otro"})
    assert taken.status_code == 409 and taken.json()["code"] == "firefly_account_exists"
    missing = await client.post(URL, headers=ADMIN, json={**LOAN, "name": "Nuevo", "firefly_account_id": "999"})
    assert missing.status_code == 422 and missing.json()["code"] == "firefly_account_missing"


async def test_card_links_an_existing_asset_account(client, firefly, db):
    card = {"kind": "credit_card", "name": "Visa", "lender": "Banco Dos", "firefly_account_id": "9", "rate_ea": "32.5", "credit_limit": "5000000.00",
            "minimum_payment": "120000.00", "payment_day": 20, "payment_match": ["VISA ORO"]}
    response = await client.post(URL, headers=ADMIN, json=card)
    assert response.status_code == 201, response.text
    body = response.json()
    firefly.create_liability.assert_not_awaited()
    assert body["balance"] == "2000000.00"  # an asset card owes what its negative balance says
    assert (body["credit_limit"], body["minimum_payment"], body["rate_ea"], body["insurance_monthly"]) == ("5000000.00", "120000.00", "32.50", "0.00")
    own = (await db.scalars(select(OwnAccountRow).where(OwnAccountRow.firefly_account == "Visa Oro"))).one()
    assert own.kind == "credit"
    firefly.balances["9"]["balance"] = Decimal("350000.00")  # credit in favour: nothing owed
    assert (await client.get(URL, headers=ADMIN)).json()["items"][0]["balance"] == "0.00"


async def test_update_pushes_a_new_rate_and_ignores_the_balance(client, firefly):
    body = await make_loan(client)
    url = f"{URL}/{body['id']}"
    response = await client.put(url, headers=ADMIN, json={**LOAN, "rate_ea": "30.00", "balance": "1.00", "lender": "Banco Uno S.A."})
    assert response.status_code == 200, response.text
    updated = response.json()
    assert (updated["rate_ea"], updated["lender"], updated["balance"]) == ("30.00", "Banco Uno S.A.", "9810557.00")
    firefly.update_account.assert_awaited_once_with("20", {"interest": "30.00", "interest_period": "yearly"})
    firefly.update_account.reset_mock()
    await client.put(url, headers=ADMIN, json={"installment": "400000.00"})
    firefly.update_account.assert_not_awaited()  # same rate: Firefly is not touched
    assert (await client.put(url, headers=ADMIN, json={"kind": "credit_card"})).json()["code"] == "debt_kind_locked"
    assert (await client.put(f"{URL}/99", headers=ADMIN, json={"lender": "x"})).json()["code"] == "debt_not_found"
    assert (await client.put(url, headers=ADMIN, json={"rate_ea": None})).status_code == 422


async def test_delete_deactivates_and_keeps_the_liability(client, firefly):
    body = await make_loan(client)
    response = await client.delete(f"{URL}/{body['id']}", headers=ADMIN)
    assert response.status_code == 200 and response.json() == {"deleted": True}
    assert (await client.get(URL, headers=ADMIN)).json() == {"items": [], "total": 0, "totals": {"balance": "0.00", "installments_monthly": "0.00"}}
    assert (await client.delete(f"{URL}/{body['id']}", headers=ADMIN)).status_code == 404
    firefly.delete_account.assert_not_awaited()
    # the name is free again
    assert (await client.post(URL, headers=ADMIN, json={**LOAN, "firefly_account_id": "20"})).status_code == 201


# --- schedule ---------------------------------------------------------------------------------------------------------

async def test_schedule_follows_the_balance_with_insurance_and_extras(client, firefly):
    firefly.balances["20"]["balance"] = Decimal("-10000000.00")
    body = await make_loan(client)
    response = await client.get(f"{URL}/{body['id']}/schedule", headers=ADMIN)
    assert response.status_code == 200, response.text
    data = response.json()
    first = data["rows"][0]
    assert first == {"n": 1, "date": "2026-12-05", "installment": "397290.00", "interest": "207847.00", "principal": "189443.00",
                     "insurance": "15000.00", "extra": "0.00", "balance": "9810557.00"}
    summary = data["summary"]
    assert summary["remaining_installments"] == 36 and summary["payoff_date"] == "2029-11-05" and summary["months_saved"] == 0
    assert summary["baseline"]["payoff_date"] == "2029-11-05" and summary["interest_saved"] == "0.00"
    assert summary["new_installment"] == "397290.00"

    by_term = (await client.get(f"{URL}/{body['id']}/schedule", headers=ADMIN, params={"extra_once": "2000000.00"})).json()["summary"]
    by_installment = (await client.get(f"{URL}/{body['id']}/schedule", headers=ADMIN, params={"extra_once": "2000000.00", "mode": "installment"})).json()["summary"]
    assert by_term["remaining_installments"] == 27 and by_term["months_saved"] == 9 and by_term["new_installment"] == "397290.00"
    assert by_installment["remaining_installments"] == 36 and by_installment["months_saved"] == 0
    assert Decimal(by_installment["new_installment"]) < Decimal("397290")
    assert Decimal(by_term["interest_saved"]) > Decimal(by_installment["interest_saved"]) > 0

    monthly = (await client.get(f"{URL}/{body['id']}/schedule", headers=ADMIN, params={"extra_monthly": "200000.00"})).json()
    assert monthly["rows"][0]["extra"] == "200000.00" and monthly["summary"]["months_saved"] > 0
    later = (await client.get(f"{URL}/{body['id']}/schedule", headers=ADMIN, params={"extra_once": "1000000.00", "extra_once_month": 6})).json()
    assert [r["n"] for r in later["rows"] if r["extra"] != "0.00"] == [6]
    assert (await client.get(f"{URL}/{body['id']}/schedule", headers=ADMIN, params={"mode": "x"})).status_code == 422
    assert (await client.get(f"{URL}/99/schedule", headers=ADMIN)).json()["code"] == "debt_not_found"


async def test_remaining_installments_are_computed_from_the_balance_when_the_dates_do_not_add_up(client, firefly):
    """Term 36 from 2026-11-05 says 36 are left, but 5.000.000 are owed and 397.290 are paid: 15 payments finish it."""
    firefly.balances["20"]["balance"] = Decimal("-5000000.00")
    body = await make_loan(client)
    summary = (await client.get(f"{URL}/{body['id']}/schedule", headers=ADMIN)).json()["summary"]
    assert summary["remaining_installments"] == m.term_for(5_000_000, 28, 397_290) == 15


async def test_installment_is_derived_from_the_balance_and_the_calendar_when_missing(client, firefly):
    firefly.balances["20"]["balance"] = Decimal("-8000000.00")
    body = await make_loan(client, installment=None, start_date="2026-01-15", term_months=36, payment_day=None)
    data = (await client.get(f"{URL}/{body['id']}/schedule", headers=ADMIN)).json()
    left = m.calendar_remaining(36, date(2026, 1, 15), TODAY)
    assert left == 26 and data["rows"][0]["installment"] == f"{m.french_installment(8_000_000, 28, left):.2f}"
    assert data["summary"]["remaining_installments"] == left and data["rows"][0]["date"] == "2026-12-15"
    assert (await client.get(URL, headers=ADMIN)).json()["totals"]["installments_monthly"] == f"{m.french_installment(8_000_000, 28, left) + 15000:.2f}"


async def test_schedule_errors_are_readable(client, firefly):
    firefly.balances["20"]["balance"] = Decimal("-10000000.00")
    stuck = await make_loan(client, installment="100000.00")
    error = await client.get(f"{URL}/{stuck['id']}/schedule", headers=ADMIN)
    assert error.status_code == 422 and error.json()["code"] == "debt_not_amortizing" and "Credito Libre" in error.json()["detail"]
    bare = await client.post(URL, headers=ADMIN, json={"kind": "credit_card", "name": "Visa", "firefly_account_id": "9", "rate_ea": "30"})
    error = await client.get(f"{URL}/{bare.json()['id']}/schedule", headers=ADMIN)
    assert error.status_code == 422 and error.json()["code"] == "debt_without_payment"


async def test_card_schedule_uses_the_minimum_payment(client, firefly):
    card = await client.post(URL, headers=ADMIN, json={"kind": "credit_card", "name": "Visa", "firefly_account_id": "9", "rate_ea": "30", "minimum_payment": "100000.00", "payment_day": 20})
    data = (await client.get(f"{URL}/{card.json()['id']}/schedule", headers=ADMIN)).json()
    assert Decimal(data["rows"][0]["interest"]) + Decimal(data["rows"][0]["principal"]) == Decimal("100000.00") and data["rows"][0]["date"] == "2026-12-20"


# --- plan -------------------------------------------------------------------------------------------------------------

async def three_debts(client, firefly):
    firefly.account_list += [{"id": "31", "name": "Pequeno", "type": "liabilities"}, {"id": "32", "name": "Caro", "type": "liabilities"}, {"id": "33", "name": "Medio", "type": "liabilities"}]
    firefly.balances.update({
        "31": {"type": "liabilities", "balance": Decimal("-800000")}, "32": {"type": "liabilities", "balance": Decimal("-6000000")},
        "33": {"type": "liabilities", "balance": Decimal("-3000000")},
    })
    ids = {}
    for name, account, rate, installment in (("Pequeno", "31", "10", "80000"), ("Caro", "32", "35", "200000"), ("Medio", "33", "20", "120000")):
        response = await client.post(URL, headers=ADMIN, json={"kind": "loan", "name": name, "firefly_account_id": account, "rate_ea": rate, "installment": installment, "payment_day": 5})
        ids[name] = response.json()["id"]
    return ids


async def test_plan_compares_avalanche_and_snowball(client, firefly):
    ids = await three_debts(client, firefly)
    response = await client.post(f"{URL}/plan", headers=ADMIN, json={"extra_monthly": "300000.00", "strategy": "both"})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert set(plan) == {"avalanche", "snowball"}
    for result in plan.values():
        assert set(result) == {"order", "debts", "debt_free_date", "total_interest", "interest_saved_vs_minimum"}
        assert sorted(result["order"]) == sorted(ids.values()) and {d["debt_id"] for d in result["debts"]} == set(ids.values())
        assert result["debt_free_date"] == max(d["payoff_date"] for d in result["debts"])
        assert Decimal(result["interest_saved_vs_minimum"]) > 0
    assert plan["snowball"]["order"][0] == ids["Pequeno"]
    assert Decimal(plan["avalanche"]["total_interest"]) < Decimal(plan["snowball"]["total_interest"])
    assert plan["avalanche"]["debt_free_date"] <= plan["snowball"]["debt_free_date"]

    one = (await client.post(f"{URL}/plan", headers=ADMIN, json={"extra_monthly": "0", "extra_once": "1000000.00", "strategy": "snowball"})).json()
    assert set(one) == {"snowball"} and Decimal(one["snowball"]["interest_saved_vs_minimum"]) > 0
    # no extra at all still redirects the freed installments (snowball effect): it beats paying each debt alone
    assert Decimal((await client.post(f"{URL}/plan", headers=ADMIN, json={"extra_monthly": "0", "strategy": "avalanche"})).json()["avalanche"]["interest_saved_vs_minimum"]) > 0
    assert (await client.post(f"{URL}/plan", headers=ADMIN, json={"strategy": "x"})).status_code == 422


async def test_plan_without_debts_is_empty(client, firefly):
    plan = (await client.post(f"{URL}/plan", headers=ADMIN, json={"extra_monthly": "100000"})).json()
    assert plan["avalanche"] == {"order": [], "debts": [], "debt_free_date": None, "total_interest": "0.00", "interest_saved_vs_minimum": "0.00"}


# --- installment payments are transfers to the liability --------------------------------------------------------------

async def pending(service, **changes):
    values = dict(source="email", source_ref=str(uuid4()), sender="alertas@nequi.com.co", raw_text="Pagaste", subject_or_title="Pago",
                  occurred_at=datetime.now(UTC), status="pending")
    values.update(changes)
    row = await service.inbox.add(**values)
    await service.session.commit()
    return row


def outgoing(extraction, monkeypatch, merchant, amount="397290.00", kind="withdrawal", hint=None):
    from dataclasses import replace

    from app.services import processor as pipeline

    changed = replace(extraction, merchant=merchant, amount=Decimal(amount), kind=kind, counterparty_hint=hint)
    monkeypatch.setattr(pipeline, "parse", lambda *args, **kwargs: changed)


@pytest.fixture
def ledger(firefly):
    """Firefly's transaction store: external ids are findable once written and gone once deleted (a real re-import would see that)."""
    firefly.written, firefly.deleted = {}, []

    async def create(payload):
        split = payload["transactions"][0]
        firefly.written[split["external_id"]] = {"id": f"tx-{len(firefly.written) + 1}", **split}
        return firefly.written[split["external_id"]]["id"]

    async def find(external_id):
        found = firefly.written.get(external_id)
        return found["id"] if found else None

    async def delete(transaction_id):
        keys = [k for k, v in firefly.written.items() if v["id"] == transaction_id]
        if not keys:
            raise PermanentError("Firefly 404: not found")
        firefly.deleted.append(transaction_id)
        for key in keys:
            del firefly.written[key]

    firefly.create_transaction.side_effect = create
    firefly.find_by_external_id.side_effect = find
    firefly.delete_transaction.side_effect = delete
    return firefly


INTEREST = Decimal("203910.00")  # 9.810.557 owed x 28 % EA monthly rate (0.0207847285) = 203.909,8 -> 203.910
CHARGES = "218910.00"  # + the 15.000 insurance


async def paid(client, service, extraction, monkeypatch, amount="397290.00", **loan):
    await make_loan(client, **loan)
    outgoing(extraction, monkeypatch, "BANCO UNO", amount=amount)
    return await service.process(await pending(service))


async def test_loan_installment_is_two_movements_capital_transfer_and_interest_expense(client, ledger, service, extraction, monkeypatch, db):
    ledger.categories.return_value = ["Alimentación", "Comisiones e impuestos"]
    service.invalidate_categories()
    item = await paid(client, service, extraction, monkeypatch)
    assert item.status == "created", item.review_reason
    assert (item.transaction_type, item.asset_account, item.destination_account) == ("transfer", "Nequi", "Credito Libre")
    capital, interest = ledger.written[f"fa:{item.source_ref}:capital"], ledger.written[f"fa:{item.source_ref}:interes"]
    # 397.290 - 203.910 interest (9.810.557 x monthly EA rate) - 15.000 insurance = 178.380 of capital
    assert (capital["type"], capital["source_id"], capital["destination_id"], capital["amount"]) == ("transfer", "1", "20", "178380.00")
    assert (interest["type"], interest["source_id"], interest["amount"], interest["tags"]) == ("withdrawal", "1", CHARGES, ["intereses"])
    assert interest["category_name"] == "Comisiones e impuestos" and interest["description"] == "Intereses y seguro de Credito Libre"
    ledger.create_account.assert_awaited_once_with("Intereses y seguro · Banco Uno", "expense")
    assert interest["destination_id"] == "5"
    assert Decimal(capital["amount"]) + Decimal(interest["amount"]) == Decimal("397290.00")
    assert item.firefly_transaction_id == capital["id"]  # the item keeps the principal
    event = (await db.scalars(select(InboxEvent).where(InboxEvent.inbox_id == item.id, InboxEvent.action == "debt_payment_split"))).one()
    assert json.loads(event.detail) == {"debt": "Credito Libre", "payment": "397290.00", "capital": "178380.00", "interest": "203910.00",
                                         "insurance": "15000.00", "expense": CHARGES}
    assert "BANCO" not in event.detail and "raw" not in event.detail
    assert ledger.written[f"fa:{item.source_ref}:capital"]["external_id"] == f"fa:{item.source_ref}:capital"


async def test_without_the_commissions_category_the_expense_has_none(client, ledger, service, extraction, monkeypatch):
    item = await paid(client, service, extraction, monkeypatch)
    assert "category_name" not in ledger.written[f"fa:{item.source_ref}:interes"]


async def test_the_interest_account_is_reused_when_it_exists(client, ledger, service, extraction, monkeypatch):
    ledger.account_list.append({"id": "77", "name": "Intereses y seguro · Banco Uno", "type": "expense"})
    item = await paid(client, service, extraction, monkeypatch)
    assert ledger.written[f"fa:{item.source_ref}:interes"]["destination_id"] == "77"
    ledger.create_account.assert_not_awaited()


async def test_a_payment_above_the_installment_sends_the_whole_excess_to_capital(client, ledger, service, extraction, monkeypatch):
    item = await paid(client, service, extraction, monkeypatch, amount="1000000.00")
    capital, interest = ledger.written[f"fa:{item.source_ref}:capital"], ledger.written[f"fa:{item.source_ref}:interes"]
    assert (capital["amount"], interest["amount"]) == ("781090.00", CHARGES)  # 1.000.000 - 218.910: the extra 602.710 is all capital


async def test_a_payment_below_interest_and_insurance_is_all_expense(client, ledger, service, extraction, monkeypatch):
    item = await paid(client, service, extraction, monkeypatch, amount="100000.00")
    assert f"fa:{item.source_ref}:capital" not in ledger.written
    interest = ledger.written[f"fa:{item.source_ref}:interes"]
    assert (interest["amount"], interest["type"]) == ("100000.00", "withdrawal")
    assert item.firefly_transaction_id == interest["id"]  # with no capital, the expense is what the item keeps


async def test_a_loan_without_insurance_splits_only_interest(client, ledger, service, extraction, monkeypatch):
    item = await paid(client, service, extraction, monkeypatch, insurance_monthly="0.00")
    assert (ledger.written[f"fa:{item.source_ref}:capital"]["amount"], ledger.written[f"fa:{item.source_ref}:interes"]["amount"]) == ("193380.00", "203910.00")


async def test_reimporting_duplicates_nothing_and_a_crash_between_the_two_completes_with_the_same_figures(client, ledger, service, extraction, monkeypatch, db):
    item = await paid(client, service, extraction, monkeypatch)
    assert ledger.create_transaction.await_count == 2
    first_id = item.firefly_transaction_id

    # Re-import: the row goes back to pending and Firefly already has both movements.
    item.status, item.firefly_transaction_id = "pending", None
    await db.commit()
    again = await service.process(item)
    assert again.status == "created" and again.firefly_transaction_id == first_id
    assert ledger.create_transaction.await_count == 2 and len(ledger.written) == 2

    # Crash: only the capital transfer reached Firefly; the balance has dropped since, but the stored figures are reused.
    del ledger.written[f"fa:{item.source_ref}:interes"]
    ledger.balances["20"]["balance"] = Decimal("-9632177.00")
    item.status, item.firefly_transaction_id = "pending", None
    await db.commit()
    healed = await service.process(item)
    assert healed.status == "created" and healed.firefly_transaction_id == first_id
    assert ledger.create_transaction.await_count == 3 and ledger.written[f"fa:{item.source_ref}:interes"]["amount"] == CHARGES
    events = (await db.scalars(select(InboxEvent).where(InboxEvent.inbox_id == item.id, InboxEvent.action == "debt_payment_split"))).all()
    assert len(events) == 1


async def test_voiding_the_item_voids_both_movements_and_reconcile_keeps_them(client, ledger, service, extraction, monkeypatch):
    item = await paid(client, service, extraction, monkeypatch)
    ledger.list_auto_transactions.return_value = []
    assert (await service.reconcile())["voided"] == []  # the split item is found through its suffixed external ids
    capital_id = item.firefly_transaction_id
    voided = await service.review(item, "void")
    assert voided.status == "voided" and len(ledger.deleted) == 2 and capital_id in ledger.deleted and ledger.written == {}
    again = await service.review(item, "void")  # idempotent
    assert again.status == "voided" and len(ledger.deleted) == 2


async def test_a_split_payment_cannot_be_edited(client, ledger, service, extraction, monkeypatch):
    item = await paid(client, service, extraction, monkeypatch)
    with pytest.raises(ValueError, match="split into capital and interest"):
        await service.review(item, "edit", {"category": "Alimentación"})
    ledger.update_transaction.assert_not_awaited()


async def test_a_card_payment_stays_one_full_transfer(client, ledger, service, extraction, monkeypatch):
    card = {"kind": "credit_card", "name": "Visa", "firefly_account_id": "9", "rate_ea": "32", "minimum_payment": "120000.00", "payment_match": ["VISA ORO PAGO"]}
    assert (await client.post(URL, headers=ADMIN, json=card)).status_code == 201
    outgoing(extraction, monkeypatch, "VISA ORO PAGO", amount="250000.00")
    item = await service.process(await pending(service))
    assert item.status == "created" and item.destination_account == "Visa Oro"
    assert list(ledger.written) == [f"fa:{item.source_ref}"]
    only = ledger.written[f"fa:{item.source_ref}"]
    assert (only["type"], only["amount"], only["destination_id"]) == ("transfer", "250000.00", "9")


async def test_a_manual_transfer_to_a_loan_is_not_split(client, ledger, service, extraction, monkeypatch):
    await make_loan(client)
    from app.models.schemas import ManualTransaction

    payload = ManualTransaction(client_ref="m-1", amount=Decimal("50000"), type="transfer", account="Nequi", destination_account="Credito Libre", description="abono")
    item = await service.manual(payload)
    assert item.status == "created" and list(ledger.written) == [f"fa:{item.source_ref}"]


async def test_installment_payment_is_recorded_as_a_transfer_to_the_liability(client, ledger, service, extraction, monkeypatch, db):
    item = await paid(client, service, extraction, monkeypatch)
    assert item.status == "created", item.review_reason
    ledger.create_account.assert_awaited_once()  # BANCO UNO never became a merchant: only the interest account was created
    actions = (await db.scalars(select(InboxEvent.action).where(InboxEvent.inbox_id == item.id))).all()
    assert "debt_payment" in actions and "debt_payment_split" in actions


async def test_payment_match_accepts_wildcards_masks_and_counterparty_hint(client, firefly, service, extraction, monkeypatch):
    await make_loan(client, payment_match=["banco*uno", "PAU BET"])
    for merchant, hint in (("BANCO DE UNO S.A.", None), ("PAU*** BET***", None), ("PSE", "Pago Banco Uno")):
        firefly.create_transaction.reset_mock()
        outgoing(extraction, monkeypatch, merchant, hint=hint)
        item = await service.process(await pending(service))
        assert (item.status, item.transaction_type, item.destination_account) == ("created", "transfer", "Credito Libre"), (merchant, item.review_reason)


async def test_other_movements_stay_expenses_and_declined_ones_are_never_payments(client, firefly, service, extraction, monkeypatch):
    await make_loan(client)
    outgoing(extraction, monkeypatch, "SUPERMERCADO CENTRAL", amount="12000.00")
    item = await service.process(await pending(service))
    assert (item.transaction_type, item.destination_account) == ("withdrawal", None)
    outgoing(extraction, monkeypatch, "BANCO UNO")
    declined = await service.process(await pending(service, raw_text="Pago rechazado de $397.290 a BANCO UNO"))
    assert declined.status == "ignored" and declined.review_reason == "declined"
    assert firefly.create_transaction.await_count == 1


async def test_an_inactive_debt_no_longer_matches_and_the_card_itself_is_never_the_source(client, firefly, service, extraction, monkeypatch, db):
    body = await make_loan(client)
    await client.delete(f"{URL}/{body['id']}", headers=ADMIN)
    outgoing(extraction, monkeypatch, "BANCO UNO")
    item = await service.process(await pending(service))
    assert (item.transaction_type, item.destination_account) == ("withdrawal", None)
    # a purchase paid WITH the debt's own account is not a payment of it
    db.add(Debt(kind="credit_card", name="Visa", firefly_account_id="9", firefly_account="Nequi", rate_ea=Decimal("30"), payment_match=["VISA"], active=True))
    await db.commit()
    outgoing(extraction, monkeypatch, "VISA STORE", amount="50000.00")
    item = await service.process(await pending(service))
    assert (item.transaction_type, item.destination_account) == ("withdrawal", None)


# --- scopes -----------------------------------------------------------------------------------------------------------

async def test_scopes_read_gets_review_writes_ingest_nothing(client, firefly):
    body = await make_loan(client)
    ingest = auth((await enroll_key(client, ["ingest"], "capture"))["key"])
    reader = auth((await enroll_key(client, ["read"], "reader"))["key"])
    reviewer = auth((await enroll_key(client, ["read", "review"], "owner"))["key"])
    one = f"{URL}/{body['id']}"
    reads = [("GET", URL), ("GET", f"{one}/schedule")]
    plan = ("POST", f"{URL}/plan")
    writes = [("POST", URL, {**LOAN, "name": "Otro"}), ("PUT", one, {"lender": "x"}), ("DELETE", one, None)]
    assert (await client.get(URL)).status_code == 401
    for method, url in reads:
        assert (await client.request(method, url, headers=ingest)).status_code == 403
        assert (await client.request(method, url, headers=reader)).status_code == 200
    assert (await client.post(plan[1], headers=ingest, json={})).status_code == 403
    assert (await client.post(plan[1], headers=reader, json={})).status_code == 200
    for method, url, payload in writes:
        for headers in (ingest, reader):
            denied = await client.request(method, url, headers=headers, json=payload)
            assert denied.status_code == 403 and denied.json()["code"] == "insufficient_scope", (method, url)
    assert (await client.put(one, headers=reviewer, json={"lender": "Nuevo"})).status_code == 200
    assert (await client.delete(one, headers=reviewer)).status_code == 200


# --- month close ------------------------------------------------------------------------------------------------------

async def test_month_close_section_has_balance_installments_paid_and_estimated_interest(client, firefly, service, extraction, monkeypatch, db, settings):
    from zoneinfo import ZoneInfo

    await make_loan(client)
    outgoing(extraction, monkeypatch, "BANCO UNO")
    assert (await service.process(await pending(service))).status == "created"
    day = datetime.now(ZoneInfo(settings.timezone)).date()
    summary = await debts.month_summary(db, firefly, settings, day.replace(day=1), day.replace(day=28))
    assert summary["total_balance"] == "9810557.00" and summary["installments_paid"] == 1 and summary["installments_paid_amount"] == "397290.00"
    assert summary["estimated_interest"] == f"{m.peso(Decimal('9810557') * m.monthly_rate(28)):.2f}"
    assert summary["debts"] == [{"debt_id": 1, "name": "Credito Libre", "balance": "9810557.00", "installments_paid": 1}]
    empty = await debts.month_summary(db, firefly, settings, date(2020, 1, 1), date(2020, 1, 31))
    assert empty["installments_paid"] == 0 and empty["total_balance"] == "9810557.00"


async def test_the_agent_month_close_report_carries_the_debts_section(client, firefly, db, settings, clients):
    from unittest.mock import AsyncMock
    from zoneinfo import ZoneInfo

    from app.core.timezone import now_utc
    from app.db.models import AgentTask
    from app.services import agent, snapshots

    await make_loan(client)
    firefly.budget_progress.return_value = []
    firefly.bills_status.return_value = []
    clients[1].complete_json = AsyncMock(return_value={"action": "finish", "summary": "Cierre revisado."})
    month = snapshots.previous_month(snapshots.month_of(now_utc().astimezone(ZoneInfo(settings.timezone)).date()))
    task = await agent.create_task(db, settings, instruction="cierre", month=month, run_now=False, kind="month_close", auto=True)
    await db.commit()
    await agent.run_task(db, firefly, clients[1], settings, task.id)
    row = await db.get(AgentTask, task.id, populate_existing=True)
    assert row.status == "done"
    section = json.loads(row.report)["debts"]
    assert section["total_balance"] == "9810557.00" and section["installments_paid"] == 0
    assert [d["name"] for d in section["debts"]] == ["Credito Libre"]
