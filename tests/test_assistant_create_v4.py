"""Assistant actions that create things: new movements, debt payments, debts, incomes and budget renames (Firefly simulated).

Every action goes proposal -> owner approval (``/apply``) -> the owner's own service. The batch below mixes what the owner
asks in one sentence («registra mis deudas y mis ingresos, paga el crédito y mueve plata entre mis cuentas»).
"""
from datetime import date
from decimal import Decimal

import pytest

from app.db.models_assistant import Proposal
from app.services import debt_math as m
from app.services import debts
from tests.test_assistant_actions_v4 import API, World, act, batch_state, by_id, when
from tests.test_assistant_bulk_v4 import bulk, entry
from tests.test_assistant_v4 import ADMIN, apply, device, open_batch

TODAY = date(2026, 11, 24)


class Bank:
    """The debt side of the simulated Firefly: liabilities with a balance, created the way ``create_liability`` does."""

    def __init__(self, world: World):
        self.world, self.firefly = world, world.firefly
        self.balances = {}
        self._next = 90
        self.add({"id": "9", "name": "Visa Oro", "type": "asset"}, "-2000000.00")
        self.firefly.account_balance.side_effect = lambda account_id: self.balances.get(str(account_id))
        self.firefly.create_liability.side_effect = self.create_liability
        self.firefly.rename_budget.side_effect = self.rename_budget

    def add(self, account: dict, balance: str | None = None) -> dict:
        """The account exists for the ledger (``accounts()`` and the stand-in's own table), with a Firefly balance when it has one."""
        self.firefly.accounts.return_value.append(account)
        self.world.ledger.accounts[account["id"]] = account
        if balance is not None:
            self.balances[account["id"]] = {"type": account["type"], "balance": Decimal(balance)}
        return account

    async def create_liability(self, name, kind, rate, balance, on):
        self._next += 1
        return self.add({"id": str(self._next), "name": name, "type": "liabilities"}, f"-{balance}")

    async def rename_budget(self, budget_id, name):
        self.world.ledger.budgets[budget_id] = name
        return {"id": budget_id, "name": name}

    def creations(self):
        return [payload["transactions"][0] for _, _, payload in self.world.ledger.writes("create")]


@pytest.fixture
def world(clients):
    return World(clients[0])


@pytest.fixture
def bank(world, monkeypatch):
    monkeypatch.setattr(debts, "today", lambda settings: TODAY)
    return Bank(world)


LOAN = {
    "kind": "loan", "name": "Crédito Libre", "lender": "Banco Uno", "balance": "10000000.00", "rate_ea": "28.00",
    "installment": "397290.00", "insurance_monthly": "15000.00", "payment_day": 5,
}


async def make_debt(client, body=None):
    response = await client.post("/api/v1/debts", headers=ADMIN, json=body or LOAN)
    assert response.status_code == 201, response.text
    return response.json()


def paid(payment, balance="10000000.00", rate="28.00", insurance="15000.00"):
    """What the common function says a payment divides into."""
    return m.payment_split(Decimal(balance), Decimal(rate), Decimal(insurance), Decimal(payment))


def movement(**changes):
    return {
        "type": "withdrawal", "amount": "85000.00", "date": when(3), "description": "Almuerzo", "source_account": "Nequi",
        "destination_account": "SUPERMERCADO CENTRAL", **changes,
    }


def budget_id_of(world, name):
    return next(ident for ident, label in world.ledger.budgets.items() if label == name)


async def mark_failed(db, proposal_id):
    """The state a proposal is left in when the server died after writing: it will be resumed, not applied from scratch."""
    row = await db.get(Proposal, proposal_id, populate_existing=True)
    row.status, row.error = "failed", "El servidor se reinició; aplica de nuevo"
    await db.commit()


# --- ledger_create ------------------------------------------------------------------------------------------------------


async def test_ledger_create_an_expense_and_a_transfer_between_own_accounts_with_a_budget(client, world, bank, db):
    batch_id = (await open_batch(client))["id"]
    expense = await act(client, batch_id, "ledger_create", None, movement(budget="mercado", category="alimentacion", tags=["almuerzo", "almuerzo"]))
    transfer = await act(client, batch_id, "ledger_create", None, movement(type="transfer", amount="300000.00", description="Ahorro", destination_account="Bancolombia"))
    assert (expense["risky"], expense["target_id"], expense["target_ref"]) == (False, None, None)
    assert expense["subject"].startswith("Almuerzo · 85000.00 · ") and expense["old_value"] is None
    assert expense["new_value"]["budget"] == "Mercado" and expense["new_value"]["category"] == "Alimentación"  # Firefly's spelling
    assert expense["new_value"]["tags"] == ["almuerzo"] and expense["new_value"]["amount"] == "85000.00"
    assert world.ledger.writes("create") == []  # proposing writes nothing

    body = (await apply(client, batch_id, [expense["id"], transfer["id"]])).json()
    assert body["failed"] == [] and body["applied"] == [expense["id"], transfer["id"]] and body["batch"]["status"] == "applied"
    first, second = bank.creations()
    assert (first["type"], first["amount"], first["source_id"], first["budget_id"], first["category_name"], first["tags"]) == (
        "withdrawal", "85000.00", "1", "11", "Alimentación", ["almuerzo"])
    assert (second["type"], second["amount"], second["source_id"], second["destination_id"]) == ("transfer", "300000.00", "1", "4")
    applied = by_id(body["batch"], expense["id"])
    assert applied["status"] == "applied" and [row["amount"] for row in applied["result"]["movements"]] == ["85000.00"]
    assert len(world.ledger.groups) == 2

    # Applying again does not duplicate, nor does retrying a proposal whose server died after writing.
    await apply(client, batch_id, [expense["id"], transfer["id"]])
    await mark_failed(db, expense["id"])
    again = (await apply(client, batch_id, [expense["id"], transfer["id"]])).json()
    assert again["failed"] == [] and len(bank.creations()) == 2 and len(world.ledger.groups) == 2

    # "Deshacer lote" removes what was created.
    reverted = (await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)).json()
    assert sorted(reverted["reverted"]) == sorted([expense["id"], transfer["id"]]) and reverted["failed"] == []
    assert world.ledger.groups == {} and reverted["batch"]["status"] == "reverted"


async def test_ledger_create_validation_fails_visibly(client, world, bank):
    batch_id = (await open_batch(client))["id"]
    for amount in ("0", "0.00", "-5.00", "abc", None):
        await act(client, batch_id, "ledger_create", None, movement(amount=amount), status=422)
    for bad in (
        movement(source_account="Cuenta fantasma"), movement(destination_account="Cuenta fantasma", type="transfer"),
        movement(source_account="SUPERMERCADO CENTRAL"), movement(type="transfer", destination_account="Nequi"),
        movement(budget="Inexistente"), movement(category="Inexistente"), movement(type="refund"),
        movement(description=""), movement(extra="x"),
    ):
        response = await act(client, batch_id, "ledger_create", None, bad, status=422)
        assert response["code"] == ("unknown_category" if bad.get("category") == "Inexistente" else "validation_error"), response
    outside = await act(client, batch_id, "ledger_create", None, movement(date=when(24 * 30)), status=422)
    assert outside["code"] == "outside_window"
    await act(client, batch_id, "ledger_create", "123", movement(), status=422)
    ok = await act(client, batch_id, "ledger_create", None, movement())
    await act(client, batch_id, "ledger_create", None, movement(), status=409)
    assert [proposal["id"] for proposal in (await batch_state(client, batch_id))["proposals"]] == [ok["id"]]

    response = await client.post(f"{API}/{batch_id}/proposals", headers=ADMIN, json={"field": "ledger_create", "new_value": movement(amount="0"), "reason": "x"})
    assert response.json() == {"detail": "Invalid new_value for ledger_create at amount: Input should be greater than 0", "code": "validation_error"}
    missing = await client.post(f"{API}/{batch_id}/proposals", headers=ADMIN, json={"field": "ledger_create", "new_value": movement(source_account="Fantasma"), "reason": "x"})
    assert missing.json() == {"detail": "La cuenta «Fantasma» no existe en Firefly.", "code": "validation_error"}


async def test_ledger_create_that_names_a_missing_account_fails_when_applied_and_the_rest_goes_on(client, world, bank):
    batch_id = (await open_batch(client))["id"]
    # «Pendiente» is a liability the batch is supposed to create; without that proposal the movement fails and says why.
    debt = await act(client, batch_id, "create_debt", None, {**LOAN, "name": "Pendiente"})
    lost = await act(client, batch_id, "ledger_create", None, movement(type="transfer", destination_account="pendiente"))
    fine = await act(client, batch_id, "ledger_create", None, movement(description="Café"))
    body = (await apply(client, batch_id, [lost["id"], fine["id"]])).json()
    assert body["applied"] == [fine["id"]]
    assert body["failed"] == [{"id": lost["id"], "detail": "La cuenta «pendiente» no existe en Firefly."}]
    assert by_id(await batch_state(client, batch_id), lost["id"])["status"] == "failed"
    assert [row["description"] for row in bank.creations()] == ["Café"]
    # With the creation in the same run, it goes through (and the creation runs first).
    body = (await apply(client, batch_id, [lost["id"], debt["id"]])).json()
    assert body["failed"] == [] and body["applied"] == [debt["id"], lost["id"]]
    assert bank.creations()[-1]["destination_id"] == "91"


# --- debt_payment -------------------------------------------------------------------------------------------------------


async def test_loan_payment_is_two_movements_with_the_common_breakdown_and_a_preview(client, world, bank, db):
    debt = await make_debt(client)
    batch_id = (await open_batch(client))["id"]
    value = {"amount": "400000.00", "date": when(5), "source_account": "Nequi"}
    proposal = await act(client, batch_id, "debt_payment", str(debt["id"]), value)
    split = paid("400000.00")
    assert proposal["old_value"] == {"capital": f"{split.capital:.2f}", "interest_and_insurance": f"{split.expense:.2f}"}
    assert (proposal["subject"], proposal["target_ref"], proposal["risky"]) == ("Crédito Libre", str(debt["id"]), False)
    assert split.capital > 0 and split.expense == split.interest + split.insurance > 0 and split.capital + split.expense == 400000
    assert bank.creations() == []

    body = (await apply(client, batch_id, [proposal["id"]])).json()
    assert body["failed"] == [] and body["batch"]["status"] == "applied"
    capital, expense = bank.creations()
    assert (capital["type"], capital["amount"], capital["source_id"], capital["destination_id"]) == ("transfer", f"{split.capital:.2f}", "1", debt["firefly_account_id"])
    assert (expense["type"], expense["amount"], expense["source_id"]) == ("withdrawal", f"{split.expense:.2f}", "1")
    assert expense["tags"] == ["intereses"] and expense["description"] == "Intereses y seguro de Crédito Libre"
    assert world.ledger.accounts[expense["destination_id"]]["name"] == "Intereses y seguro · Banco Uno"
    result = by_id(body["batch"], proposal["id"])["result"]
    assert (result["capital"], result["interest_and_insurance"], len(result["movements"])) == (f"{split.capital:.2f}", f"{split.expense:.2f}", 2)

    # Repeating the apply does not duplicate; a proposal that died after writing resumes with the same figures although the balance dropped.
    await apply(client, batch_id, [proposal["id"]])
    assert len(bank.creations()) == 2
    bank.balances[debt["firefly_account_id"]]["balance"] += split.capital
    await mark_failed(db, proposal["id"])
    again = (await apply(client, batch_id, [proposal["id"]])).json()
    assert again["failed"] == [] and len(bank.creations()) == 2 and len(world.ledger.groups) == 2
    assert by_id(again["batch"], proposal["id"])["result"]["capital"] == f"{split.capital:.2f}"

    # "Deshacer lote" removes both movements, and doing it twice is harmless.
    reverted = (await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)).json()
    assert reverted["reverted"] == [proposal["id"]] and reverted["failed"] == [] and world.ledger.groups == {}
    assert (await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)).json()["reverted"] == []


async def test_a_loan_payment_that_does_not_cover_the_interest_is_one_expense_and_an_extra_one_is_all_capital(client, world, bank):
    debt = await make_debt(client)
    batch_id = (await open_batch(client))["id"]
    small = await act(client, batch_id, "debt_payment", str(debt["id"]), {"amount": "100000.00", "date": when(5), "source_account": "Nequi"})
    assert small["old_value"] == {"capital": "0.00", "interest_and_insurance": "100000.00"}
    flat = await make_debt(client, {**LOAN, "name": "Sin tasa", "rate_ea": "0.00", "insurance_monthly": "0.00"})
    extra = await act(client, batch_id, "debt_payment", str(flat["id"]), {"amount": "50000.00", "date": when(6), "source_account": "Nequi"})
    assert extra["old_value"] == {"capital": "50000.00", "interest_and_insurance": "0.00"}
    body = (await apply(client, batch_id, [small["id"], extra["id"]])).json()
    assert body["failed"] == []
    only_expense, only_capital = bank.creations()
    assert (only_expense["type"], only_expense["amount"]) == ("withdrawal", "100000.00")
    assert (only_capital["type"], only_capital["amount"], only_capital["description"]) == ("transfer", "50000.00", "Pago de Sin tasa")


async def test_card_payment_is_one_transfer(client, world, bank):
    card = await make_debt(client, {"kind": "credit_card", "name": "Visa Oro", "rate_ea": "30.00", "firefly_account_id": "9", "minimum_payment": "50000.00"})
    batch_id = (await open_batch(client))["id"]
    proposal = await act(client, batch_id, "debt_payment", str(card["id"]), {"amount": "250000.00", "date": when(5), "source_account": "Nequi"})
    assert proposal["old_value"] == {"capital": "250000.00", "interest_and_insurance": "0.00"} and proposal["subject"] == "Visa Oro"
    body = (await apply(client, batch_id, [proposal["id"]])).json()
    assert body["failed"] == []
    [transfer] = bank.creations()
    assert (transfer["type"], transfer["amount"], transfer["source_id"], transfer["destination_id"]) == ("transfer", "250000.00", "1", "9")
    result = by_id(body["batch"], proposal["id"])["result"]
    assert (result["capital"], result["interest_and_insurance"], len(result["movements"])) == ("250000.00", "0.00", 1)


async def test_debt_payment_validation(client, world, bank):
    debt = await make_debt(client)
    batch_id = (await open_batch(client))["id"]
    value = {"amount": "400000.00", "date": when(5), "source_account": "Nequi"}
    assert (await act(client, batch_id, "debt_payment", "999", value, status=422))["code"] == "unknown_debt"
    assert (await act(client, batch_id, "debt_payment", None, value, status=422))["code"] == "validation_error"
    for bad in ({**value, "amount": "0"}, {**value, "amount": "-1"}, {**value, "source_account": "Fantasma"}, {**value, "source_account": "SUPERMERCADO CENTRAL"},
                {**value, "date": when(24 * 40)}, {**value, "extra": 1}, {"amount": "1.00"}):
        await act(client, batch_id, "debt_payment", str(debt["id"]), bad, status=422)
    await act(client, batch_id, "debt_payment", str(debt["id"]), value)
    await act(client, batch_id, "debt_payment", "CRÉDITO LIBRE", value, status=409)  # by name too: the same payment
    await act(client, batch_id, "debt_payment", str(debt["id"]), value, status=409)


# --- debts and incomes in one batch ----------------------------------------------------------------------------------------

DEBTS = [
    {"kind": "loan", "name": "Lulo", "lender": "Lulo Bank", "balance": "1500000.00", "rate_ea": "24.95", "term_months": 24, "installment": "120000.00", "payment_day": 10},
    {"kind": "loan", "name": "Caja", "lender": "Caja Social", "balance": "8000000.00", "rate_ea": "19.50", "installment": "350000.00", "insurance_monthly": "12000.00"},
    {"kind": "credit_card", "name": "Rappi", "lender": "Rappi", "balance": "500000.00", "rate_ea": "32.00", "credit_limit": "3000000.00", "minimum_payment": "40000.00", "payment_day": 20},
    {"kind": "credit_card", "name": "Visa Oro", "lender": "Banco Uno", "rate_ea": "30.00", "firefly_account_id": "9", "minimum_payment": "50000.00", "credit_limit": "5000000.00"},
]
INCOMES = [
    {"name": "Salario", "kind": "fixed", "currency": "COP", "amount": "3600000.00", "day_of_month": 30, "match": ["NOMINA"]},
    {"name": "Clientes USD", "kind": "variable", "currency": "USD", "amount": "800.00"},
]


async def test_one_batch_creates_four_debts_and_two_incomes_and_the_server_lets_the_model_find_them(client, world, bank, db):
    batch_id = (await open_batch(client))["id"]
    created = await bulk(client, batch_id, [entry("create_debt", None, debt) for debt in DEBTS] + [entry("create_income", None, income) for income in INCOMES])
    assert (created["created"], created["failed"]) == (6, 0)
    proposals = [row["proposal"] for row in created["items"]]
    assert all(not proposal["risky"] and proposal["target_id"] is None for proposal in proposals)
    assert proposals[0]["subject"] == "Crédito Lulo · saldo $1.500.000 · 24,95 % EA · cuota $120.000"
    assert proposals[2]["subject"] == "Tarjeta Rappi · saldo $500.000 · 32,00 % EA · cupo $3.000.000 · pago mínimo $40.000"
    assert proposals[3]["subject"] == "Tarjeta Visa Oro · 30,00 % EA · cupo $5.000.000 · pago mínimo $50.000"
    assert proposals[4]["subject"] == "Salario · $3.600.000 · fijo · día 30"
    assert proposals[5]["subject"] == "Clientes USD · $800 USD · variable"
    assert world.firefly.create_liability.await_count == 0

    body = (await apply(client, batch_id, [proposal["id"] for proposal in proposals])).json()
    assert body["failed"] == [] and sorted(body["applied"]) == sorted(proposal["id"] for proposal in proposals) and body["batch"]["status"] == "applied"
    assert world.firefly.create_liability.await_count == 3  # the card linked to the own account creates nothing in Firefly
    assert all(by_id(body["batch"], proposal["id"])["result"] for proposal in proposals[:4])

    # What the model reads to find them: debts, incomes and own accounts, with the read scope alone.
    reader = await device(db, "reader", ["read"])
    listing = (await client.get("/api/v1/debts", headers=reader)).json()
    assert [(row["name"], row["kind"]) for row in listing["items"]] == [("Lulo", "loan"), ("Caja", "loan"), ("Rappi", "credit_card"), ("Visa Oro", "credit_card")]
    assert {row["name"]: row["firefly_account_id"] for row in listing["items"]}["Visa Oro"] == "9"
    assert all({"id", "balance", "rate_ea", "installment", "minimum_payment", "firefly_account_id"} <= row.keys() for row in listing["items"])
    assert {row["name"]: row["balance"] for row in listing["items"]} == {"Lulo": "1500000.00", "Caja": "8000000.00", "Rappi": "500000.00", "Visa Oro": "2000000.00"}
    earned = (await client.get("/api/v1/incomes", headers=reader)).json()["items"]
    assert [(row["name"], row["currency"], row["amount"], row["kind"]) for row in earned] == [
        ("Salario", "COP", "3600000.00", "fixed"), ("Clientes USD", "USD", "800.00", "variable")]
    own = {row["firefly_account"] for row in (await client.get("/api/v1/own-accounts", headers=reader)).json()["items"]}
    assert {"Lulo", "Caja", "Rappi", "Visa Oro"} <= own

    # Nothing in this batch has an automatic undo.
    assert (await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)).status_code == 409


async def test_debt_and_income_proposals_are_validated(client, world, bank):
    await make_debt(client)
    batch_id = (await open_batch(client))["id"]
    cases = [
        ({**DEBTS[0], "name": "Crédito Libre"}, 409, "debt_exists"),  # the analyzer already tracks that name
        ({**DEBTS[0], "balance": None}, 422, "balance_required"),
        ({**DEBTS[0], "firefly_account_id": "999"}, 422, "firefly_account_missing"),
        ({**DEBTS[0], "name": "Visa Oro"}, 409, "firefly_account_exists"),
        ({**DEBTS[0], "rate_ea": "101"}, 422, "validation_error"),
        ({**DEBTS[0], "extra": 1}, 422, "validation_error"),
        ({**DEBTS[0], "kind": "mortgage"}, 422, "validation_error"),
    ]
    for value, status, code in cases:
        response = await act(client, batch_id, "create_debt", None, value, status=status)
        assert response["code"] == code, (value, response)
    first = await act(client, batch_id, "create_debt", None, DEBTS[0])
    await act(client, batch_id, "create_debt", None, {**DEBTS[0], "name": "LULO"}, status=409)
    assert first["old_value"] is None
    for value in ({**INCOMES[0], "amount": "0"}, {**INCOMES[0], "currency": "EUR"}, {**INCOMES[0], "kind": "otro"}, {"name": "X"}):
        await act(client, batch_id, "create_income", None, value, status=422)
    await act(client, batch_id, "create_income", None, INCOMES[0])
    await act(client, batch_id, "create_income", None, {**INCOMES[0], "name": "salario"}, status=409)
    await act(client, batch_id, "create_income", None, {**INCOMES[0], "currency": "USD"})


async def test_update_rename_and_delete(client, world, bank, db):
    loan, card = [await make_debt(client, body) for body in (DEBTS[0], DEBTS[3])]
    income = (await client.post("/api/v1/incomes", headers=ADMIN, json=INCOMES[0])).json()
    other = (await client.post("/api/v1/incomes", headers=ADMIN, json=INCOMES[1])).json()
    batch_id = (await open_batch(client))["id"]

    # update_debt: only the minimum payment changes.
    update = await act(client, batch_id, "update_debt", str(card["id"]), {"minimum_payment": "60000.00"})
    assert (update["old_value"], update["new_value"], update["subject"], update["risky"]) == (
        {"minimum_payment": "50000.00"}, {"minimum_payment": "60000.00"}, "Visa Oro", False)
    for value, status in (({}, 422), ({"balance": "1.00"}, 422), ({"active": False}, 422), ({"name": None}, 422), ({"rate_ea": None}, 422),
                          ({"kind": "loan"}, 422), ({"name": "lulo"}, 409), ({"nope": 1}, 422)):
        await act(client, batch_id, "update_debt", str(card["id"]), value, status=status)
    await act(client, batch_id, "update_debt", "999", {"minimum_payment": "1.00"}, status=404)
    await act(client, batch_id, "update_debt", "Visa Oro", {"minimum_payment": "1.00"}, status=422)
    new_income = await act(client, batch_id, "update_income", str(income["id"]), {"amount": "3800000.00", "day_of_month": 28})
    assert new_income["old_value"] == {"amount": "3600000.00", "day_of_month": 30} and new_income["subject"] == "Salario"
    for value in ({}, {"amount": "0"}, {"currency": None}, {"active": False}):
        await act(client, batch_id, "update_income", str(income["id"]), value, status=422)

    # rename_budget: the new name in an object or as plain text; a name that exists is a 409.
    rename = await act(client, batch_id, "rename_budget", "ocio", {"name": "Entretenimiento"})
    assert (rename["target_ref"], rename["old_value"], rename["new_value"], rename["subject"], rename["risky"]) == (
        "Ocio", "Ocio", {"name": "Entretenimiento"}, "Ocio → Entretenimiento", False)
    clash = await act(client, batch_id, "rename_budget", "Mercado", {"name": "ocio"}, status=409)
    assert clash["code"] == "conflict" and "Ocio" in clash["detail"]
    assert (await act(client, batch_id, "rename_budget", "Inexistente", {"name": "Otro"}, status=422))["code"] == "unknown_budget"
    await act(client, batch_id, "rename_budget", "Mercado", "Mercado", status=422)
    await act(client, batch_id, "rename_budget", "Mercado", {"name": ""}, status=422)
    await act(client, batch_id, "rename_budget", "Mercado", "Entretenimiento", status=409)  # the batch already renames one to that
    text = await act(client, batch_id, "rename_budget", "Mercado", "Supermercado")
    assert text["new_value"] == {"name": "Supermercado"}
    # A rename whose target name appears in Firefly after the proposal fails visibly when applied.
    await world.create_budget("Presupuesto")
    late = await act(client, batch_id, "rename_budget", "Presupuesto", {"name": "Viajes"})
    await world.create_budget("Viajes")

    # Deletions are risky: one at a time.
    drop_debt = await act(client, batch_id, "delete_debt", str(loan["id"]))
    drop_income = await act(client, batch_id, "delete_income", str(other["id"]))
    assert drop_debt["risky"] and drop_income["risky"] and drop_debt["new_value"] is None and drop_debt["subject"] == "Lulo"
    await act(client, batch_id, "delete_debt", str(loan["id"]), status=409)
    await act(client, batch_id, "delete_debt", str(loan["id"]), {"x": 1}, status=422)
    together = await apply(client, batch_id, [drop_debt["id"], drop_income["id"]])
    assert together.status_code == 422 and together.json()["code"] == "risky_requires_single"

    body = (await apply(client, batch_id, [update["id"], new_income["id"], rename["id"], text["id"], late["id"]])).json()
    assert body["failed"] == [{"id": late["id"], "detail": "A budget named «Viajes» already exists"}]
    assert sorted(body["applied"]) == sorted([update["id"], new_income["id"], rename["id"], text["id"]])
    assert {"Entretenimiento", "Supermercado", "Presupuesto", "Viajes"} == set(world.ledger.budgets.values())
    listing = {row["name"]: row for row in (await client.get("/api/v1/debts", headers=ADMIN)).json()["items"]}
    assert listing["Visa Oro"]["minimum_payment"] == "60000.00"
    incomes_now = {row["name"]: row for row in (await client.get("/api/v1/incomes", headers=ADMIN)).json()["items"]}
    assert (incomes_now["Salario"]["amount"], incomes_now["Salario"]["day_of_month"]) == ("3800000.00", 28)

    for proposal in (drop_debt, drop_income):
        assert (await apply(client, batch_id, [proposal["id"]])).json()["failed"] == []
    assert [row["name"] for row in (await client.get("/api/v1/debts", headers=ADMIN)).json()["items"]] == ["Visa Oro"]
    assert [row["name"] for row in (await client.get("/api/v1/incomes", headers=ADMIN)).json()["items"]] == ["Salario"]
    # Applying the deletion again is harmless: it is already applied.
    assert (await apply(client, batch_id, [drop_debt["id"]])).json()["failed"] == []
    assert (await batch_state(client, batch_id))["status"] == "partially_applied"  # the failed rename is still there


async def test_creations_run_before_the_payments_and_movements_that_name_them(client, world, bank):
    batch_id = (await open_batch(client))["id"]
    value = {"amount": "120000.00", "date": when(5), "source_account": "Nequi"}
    # What the payment and the movement name has to be proposed first (they are checked against the batch) but they are approved first here.
    debt_proposal = await act(client, batch_id, "create_debt", None, DEBTS[0])
    budget = await act(client, batch_id, "create_budget", None, {"name": "Viajes"})
    category = await act(client, batch_id, "create_category", None, "Viajes")
    income = await act(client, batch_id, "create_income", None, INCOMES[0])
    payment = await act(client, batch_id, "debt_payment", "lulo", value)
    split = m.payment_split(Decimal("1500000.00"), Decimal("24.95"), Decimal(0), Decimal("120000.00"))
    assert (payment["target_ref"], payment["subject"]) == ("Lulo", "Lulo")
    assert payment["old_value"] == {"capital": f"{split.capital:.2f}", "interest_and_insurance": f"{split.expense:.2f}"}
    spend = await act(client, batch_id, "ledger_create", None, movement(budget="Viajes", category="Viajes", destination_account="Lulo", type="transfer", description="Extra"))
    ids = [payment["id"], spend["id"], debt_proposal["id"], budget["id"], category["id"], income["id"]]

    body = (await apply(client, batch_id, ids)).json()
    assert body["failed"] == [], body
    assert set(body["run"]["approved_ids"][:4]) == {debt_proposal["id"], budget["id"], category["id"], income["id"]}
    assert set(body["run"]["approved_ids"][4:]) == {payment["id"], spend["id"]}
    capital, expense, extra = bank.creations()
    assert (capital["amount"], expense["amount"]) == (f"{split.capital:.2f}", f"{split.expense:.2f}")
    assert (extra["budget_id"], extra["category_name"], extra["destination_id"]) == (budget_id_of(world, "Viajes"), "Viajes", capital["destination_id"])
    assert world.ledger.budgets[extra["budget_id"]] == "Viajes"

    # Undo the batch: the payment and the movement go away; the debt, income, budget and category stay.
    reverted = (await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)).json()
    assert sorted(reverted["reverted"]) == sorted([payment["id"], spend["id"]]) and world.ledger.groups == {}
    assert "Viajes" in world.categories and "Viajes" in world.ledger.budgets.values()
