"""Contract fixtures of the finance plan: debts, incomes, TRM and the month plan (``FinancePlanApi`` of the app).

Everything is deterministic: the calendar is pinned to 2026-11-06, the TRM comes from a simulated SODA service and Firefly is the
shared mock with a liability and a card added. ``record(env)`` is called by ``test_contract_fixtures``.
"""
from datetime import date
from decimal import Decimal

import httpx

from app.clients.trm import TrmClient
from app.services import debts, fx

TODAY = date(2026, 11, 6)
LIABILITY = {"id": "20", "name": "Crédito libre inversión", "type": "liabilities"}
CARD = {"id": "9", "name": "Visa Oro", "type": "asset"}
LOAN = {
    "kind": "loan", "name": "Crédito libre inversión", "lender": "Banco Uno", "balance": "10000000.00", "original_amount": "10000000.00",
    "rate_ea": "28.50", "term_months": 36, "start_date": "2026-11-05", "installment": "401500.00", "payment_day": 5,
    "insurance_monthly": "15000.00", "payment_match": ["BANCO UNO"],
}
CARD_DEBT = {
    "kind": "credit_card", "name": "Visa Oro", "lender": "Banco Dos", "firefly_account_id": "9", "rate_ea": "32.00", "credit_limit": "5000000.00",
    "minimum_payment": "120000.00", "payment_day": 20, "payment_match": ["VISA ORO"],
}
SALARY = {"name": "Salario", "kind": "fixed", "currency": "COP", "amount": "4500000.00", "day_of_month": 30, "match": ["EMPRESA SAS"]}
RETAINER = {"name": "Cliente en dólares", "kind": "fixed", "currency": "USD", "amount": "500.00", "day_of_month": 5, "match": ["PAYONEER"]}
FREELANCE = {"name": "Proyectos", "kind": "variable", "currency": "COP", "amount": "800000.00", "day_of_month": None, "match": ["CLIENTE X"]}


def _soda(request):
    day = request.url.params["$where"].split("'")[1][:10] if "$where" in request.url.params else None
    row = {"valor": "3920.75", "unidad": "COP", "vigenciadesde": "2026-11-06T00:00:00.000", "vigenciahasta": "2026-11-09T00:00:00.000"}
    return httpx.Response(200, json=[row] if day is None or "2026-11-06" <= day <= "2026-11-09" else [])


async def record(env):
    rec, reviewer, firefly = env.rec, env.reviewer, env.clients[0]
    book = [*await firefly.accounts(), CARD]
    balances = {"20": {"type": "liabilities", "balance": Decimal("-9810557.00")}, "9": {"type": "asset", "balance": Decimal("-1850000.00")}}

    async def create_liability(*args):
        book.append(LIABILITY)
        return LIABILITY

    with env.monkeypatch.context() as patch:
        patch.setattr(debts, "today", lambda settings: TODAY)
        patch.setattr(fx, "new_client", lambda config: TrmClient(config.trm_base_url, config.trm_timeout_seconds, httpx.MockTransport(_soda)))
        patch.setattr(firefly.accounts, "side_effect", lambda: list(book))
        patch.setattr(firefly.create_liability, "side_effect", create_liability)
        patch.setattr(firefly.account_balance, "side_effect", lambda account_id: balances.get(str(account_id)))

        loan = await rec.call("create_debt", "POST", "/debts", reviewer, status=201, json=LOAN)
        card = await rec.call(None, "POST", "/debts", reviewer, status=201, json=CARD_DEBT)
        listing = await rec.call("list_debts", "GET", "/debts", reviewer)
        assert listing["total"] == 2 and listing["totals"]["balance"] == "11660557.00", listing
        await rec.call("update_debt", "PUT", f"/debts/{loan['id']}", reviewer, json={**LOAN, "lender": "Banco Uno S.A."})
        schedule = await rec.call(
            "get_debt_schedule", "GET", f"/debts/{loan['id']}/schedule", reviewer,
            params={"extra_monthly": "200000.00", "extra_once": "1000000.00", "extra_once_month": 3, "mode": "term"},
        )
        assert schedule["summary"]["months_saved"] > 0 and schedule["rows"][2]["extra"] == "1200000.00", schedule["summary"]
        plan = await rec.call("plan_debts", "POST", "/debts/plan", reviewer, json={"extra_monthly": "300000.00", "extra_once": "500000.00", "strategy": "both"})
        assert set(plan) == {"avalanche", "snowball"}

        salary = await rec.call("create_income", "POST", "/incomes", reviewer, status=201, json=SALARY)
        await rec.call(None, "POST", "/incomes", reviewer, status=201, json=RETAINER)
        project = await rec.call(None, "POST", "/incomes", reviewer, status=201, json=FREELANCE)
        await rec.call("list_incomes", "GET", "/incomes", reviewer)
        await rec.call("update_income", "PUT", f"/incomes/{salary['id']}", reviewer, json={**SALARY, "amount": "4600000.00"})
        trm = await rec.call("get_trm", "GET", "/fx/trm", reviewer, params={"date": "2026-11-08"})
        assert trm["rate"] == "3920.75" and trm["stale"] is False, trm
        month = await rec.call("get_month_plan", "GET", "/plan/month", reviewer, params={"month": "2026-11"})
        assert month["income_expected"]["fixed_usd"]["cop"] == "1960375.00", month
        await rec.call("delete_income", "DELETE", f"/incomes/{project['id']}", reviewer)
        await rec.call("delete_debt", "DELETE", f"/debts/{card['id']}", reviewer)
