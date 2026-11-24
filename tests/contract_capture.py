"""Contract section ``capture``: receipts by photo and utility bills (phases 15 and 17).

The vision model is faked (``vision.read_receipt``), the receipt background task runs on the test session, Firefly is the
stateful ``FireflyLedger`` and the bills clock is pinned, so every body is deterministic. Dates are fixed 2026 values far
from the run date (instants close to "now" would be frozen by the recorder).
"""
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

from app.services import receipts
from tests.test_capture_v4 import jpeg, png
from tests.test_ledger_v4 import FireflyLedger, make_inbox

BILLS_TODAY = date(2026, 11, 24)
RECEIPT_DAY = date(2026, 11, 9)
PAID_AT = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)  # 12:00 in Bogota
LEDGER_CALLS = ("get_transaction", "create_transaction", "update_transaction", "delete_transaction", "find_by_external_id", "create_account")


def parsed(**changes):
    values = {
        "merchant": "MERCADO LA ESTRELLA", "nit": "9001234567", "date": RECEIPT_DAY, "total": Decimal("40000.00"),
        "tax": Decimal("3000.00"), "tip": None, "payment_method": "efectivo",
        "items": [
            {"description": "Leche", "quantity": Decimal("2.000"), "amount": Decimal("30000.00"), "category": "Alimentación"},
            {"description": "Entrada cine", "quantity": Decimal("1.000"), "amount": Decimal("10000.00"), "category": "Ocio"},
        ],
    }
    return values | changes


@contextmanager
def own_ledger(firefly, accounts):
    """A stateful Firefly for this section; the mock goes back to what the previous section left."""
    saved = {name: getattr(firefly, name).side_effect for name in LEDGER_CALLS}
    returns = {name: getattr(firefly, name).return_value for name in ("budgets", "bills")}
    ledger = FireflyLedger(firefly, accounts, budgets={"11": "Mercado", "12": "Ocio"}, bills={"21": "Internet", "22": "Arriendo"})
    try:
        yield ledger
    finally:
        for name, effect in saved.items():
            getattr(firefly, name).side_effect = effect
        for name, value in returns.items():
            getattr(firefly, name).return_value = value


async def record(env):
    firefly = env.clients[0]
    firefly.categories.return_value = [*dict.fromkeys([*firefly.categories.return_value, "Alimentación", "Ocio"])]
    await env.service.sync(force=True)  # the processor caches the category list
    with env.monkeypatch.context() as patch, own_ledger(firefly, list(await env.service.accounts())) as ledger:
        patch.setattr(firefly.categories, "return_value", ["Alimentación", "Transporte", "Ocio"])
        await _receipts(env, patch, ledger)
        await _bills(env, patch, ledger)


async def _receipts(env, patch, ledger):
    rec, reviewer = env.rec, env.reviewer
    answers = [
        parsed(),
        parsed(merchant="PAPELERIA EL LAPIZ", total=Decimal("15750.00"), tax=None, payment_method="tarjeta_credito", items=[
            {"description": "Cuaderno", "quantity": Decimal("1"), "amount": Decimal("5000.00"), "category": "Ocio"}]),
        parsed(merchant="TIENDA DE LA ESQUINA", total=Decimal("22000.00"), tax=None, payment_method="tarjeta_debito", items=[]),
    ]

    async def read(image, mime, categories, cycle_id, today, usage=None, **kwargs):
        if usage is not None:
            usage["calls"] = 1
        return answers.pop(0), "deepseek-v4.1-flash"

    firefly = env.clients[0]

    async def background(receipt_id, image, mime):
        await receipts.process(env.db, firefly, receipt_id, image, mime)

    patch.setattr("app.services.receipts.vision.read_receipt", read)
    patch.setattr(receipts.ai_budget, "record", AsyncMock())
    patch.setattr("app.api.routers.receipts.process_background", background)

    async def expense(ref, merchant, amount):
        group = ledger.seed(amount=amount, date=f"{RECEIPT_DAY}T12:00:00-05:00", external_id=f"fa:{ref}", category="Alimentación")
        return await make_inbox(
            env.service, source_ref=ref, merchant=merchant, amount=Decimal(amount),
            occurred_at=datetime(2026, 11, 9, 17, 0, tzinfo=UTC), firefly_transaction_id=group,
        )

    market = await expense("receipt-contract-market", "MERCADO LA ESTRELLA", "40000.00")
    corner = await expense("receipt-contract-corner", "TIENDA DE LA ESQUINA", "22000.00")

    # A: matched by itself to the market expense; B: lines do not add up; C: linked by the owner.
    def files(data, name="recibo.jpg", mime="image/jpeg"):
        return {"file": (name, data, mime)}

    uploaded = await rec.call("upload_receipt", "POST", "/receipts", reviewer, status=202, files=files(jpeg()))
    assert uploaded["status"] == "processing" and set(uploaded) == {"id", "status"}, uploaded
    first = uploaded["id"]
    second = (await rec.call(None, "POST", "/receipts", reviewer, status=202, files=files(png(), "recibo.png", "image/png")))["id"]
    third = (await rec.call(None, "POST", "/receipts", reviewer, status=202, files=files(jpeg(orientation=8)), data={"item_id": str(corner.id)}))["id"]
    assert not answers, "every upload must have been read exactly once"
    # The same image again is idempotent: same id, no second reading.
    again = await rec.call(None, "POST", "/receipts", reviewer, status=202, files=files(jpeg()))
    assert again["id"] == first and again["status"] == "ready", again

    detail = await rec.call("get_receipt", "GET", f"/receipts/{first}", reviewer)
    assert (detail["status"], detail["reason"], detail["match_kind"], detail["item_id"], detail["applied"]) == ("ready", None, "matched", market.id, False), detail
    assert detail["match"]["item"]["id"] == market.id and detail["match"]["candidates"] == [], detail
    assert detail["total"] == "40000.00" and detail["date"] == RECEIPT_DAY.isoformat() and detail["items_check"]["ok"] is True, detail
    assert [(row["category"], row["amount"]) for row in detail["items"]] == [("Alimentación", "30000.00"), ("Ocio", "10000.00")], detail
    assert detail["items"][0]["quantity"] == "2" and detail["suggested_category"] is None, detail
    assert [(row["category"], row["amount"], row["items"]) for row in detail["split_suggestion"]["splits"]] == [
        ("Alimentación", "30000.00", [0]), ("Ocio", "10000.00", [1])], detail
    assert (await rec.call(None, "GET", f"/receipts/{second}", reviewer))["reason"] == "items_total_mismatch"

    listing = await rec.call("list_receipts", "GET", "/receipts", reviewer, params={"limit": 50, "offset": 0})
    rows = {row["id"]: row for row in listing["items"]}
    assert listing["total"] >= 3 and {first, second, third} <= rows.keys() and "items" not in rows[first], listing
    assert (rows[second]["status"], rows[second]["match_kind"]) == ("needs_review", None), rows[second]
    assert (rows[third]["status"], rows[third]["match_kind"], rows[third]["item_id"]) == ("ready", "linked", corner.id), rows[third]
    only_review = await rec.call(None, "GET", "/receipts", reviewer, params={"status": "needs_review"})
    assert [row["id"] for row in only_review["items"]] == [second], only_review

    applied = await rec.call("apply_receipt", "POST", f"/receipts/{first}/apply", reviewer, json={"split": True})
    assert applied["created"] is None and isinstance(applied["change_id"], int), applied
    result = applied["receipt"]
    assert result["applied"] is True and result["split_suggestion"] is None and result["applied_result"]["split"] is True, result
    assert result["applied_result"]["change_id"] == applied["change_id"] and result["applied_result"]["create_cash"] is False, result
    assert sorted((journal["category_name"], Decimal(journal["amount"])) for journal in ledger.journals(market.firefly_transaction_id)) == [
        ("Alimentación", Decimal("30000")), ("Ocio", Decimal("10000"))]
    again = await rec.call(None, "POST", f"/receipts/{first}/apply", reviewer, status=409, json={"split": True})
    assert again["code"] == "already_applied", again

    assert await rec.call("delete_receipt", "DELETE", f"/receipts/{third}", reviewer) == {"deleted": True}
    missing = await rec.call(None, "GET", f"/receipts/{third}", reviewer, status=404)
    assert missing["code"] == "receipt_not_found", missing


async def _bills(env, patch, ledger):
    rec, reviewer = env.rec, env.reviewer
    patch.setattr("app.services.bills.local_today", lambda: BILLS_TODAY)

    async def add(entity, service, amount, due, **extra):
        body = {"entity": entity, "service": service, "amount": amount, "due_date": due, **extra}
        return await rec.call(None, "POST", "/bills", reviewer, status=201, json=body)

    async def pay_by_hand(bill):
        paid = await rec.call(None, "POST", f"/bills/{bill['id']}/mark-paid", reviewer, json={})
        assert paid["status"] == "paid" and paid["paid_item_id"] is None, paid

    water = await rec.call(
        "create_bill", "POST", "/bills", reviewer, status=201,
        json={"entity": "Aguas de Cartagena S.A. E.S.P.", "service": "agua", "amount": "85400.00", "due_date": "2026-11-29", "reference": "1234567890", "period": "2026-11", "issued_on": None},
    )
    assert (water["entity"], water["reference"], water["status"], water["days_to_due"], water["source"]) == ("AGUAS DE CARTAGENA", "****7890", "pending", 5, "manual"), water
    assert water["amount"] == "85400.00" and water["currency"] == "COP" and water["issued_on"] == "2026-10-30" and water["paid_item_id"] is None, water
    duplicate = await rec.call(None, "POST", "/bills", reviewer, status=409, json={"entity": "Aguas de Cartagena", "service": "agua", "amount": "85400.00", "due_date": "2026-11-29"})
    assert duplicate["code"] == "bill_exists", duplicate

    # Recurrence: Claro billed Sep-Nov (variable), Gas Natural Oct-Nov (stable); the Nov ones stay unpaid and overdue.
    for due, amount in (("2026-09-12", "50000.00"), ("2026-10-12", "60000.00")):
        await pay_by_hand(await add("Claro", "telefonia", amount, due))
    await add("Claro", "telefonia", "70000.00", "2026-11-12")
    await pay_by_hand(await add("Gas Natural", "gas", "40000.00", "2026-10-20"))
    await add("Gas Natural", "gas", "40000.00", "2026-11-20")
    light = await add("Afinia", "luz", "120000.00", "2026-12-05")

    listing = await rec.call("list_bills", "GET", "/bills", reviewer, params={"limit": 100, "offset": 0})
    assert listing["total"] == 7 and [row["due_date"] for row in listing["items"]] == sorted(row["due_date"] for row in listing["items"]), listing
    assert {row["status"] for row in listing["items"]} == {"paid", "overdue", "pending"}, listing
    november = await rec.call(None, "GET", "/bills", reviewer, params={"month": "2026-11", "status": "overdue"})
    assert [row["entity"] for row in november["items"]] == ["CLARO", "GAS NATURAL"], november

    upcoming = await rec.call("get_upcoming_bills", "GET", "/bills/upcoming", reviewer, params={"days": 30})
    assert upcoming["as_of"] == "2026-11-24" and upcoming["days"] == 30, upcoming
    assert [row["entity"] for row in upcoming["items"]] == ["CLARO", "GAS NATURAL", "AGUAS DE CARTAGENA", "AFINIA"], upcoming
    assert [(row["entity"], row["expected_amount"], row["expected_due_date"], row["based_on"], row["variability"], row["last_amount"]) for row in upcoming["expected"]] == [
        ("CLARO", "60000.00", "2026-12-12", 3, "variable", "70000.00"),
        ("GAS NATURAL", "40000.00", "2026-12-20", 2, "stable", "40000.00"),
    ], upcoming
    assert upcoming["totals"] == {"overdue": "110000.00", "pending": "205400.00", "expected": "100000.00", "all": "415400.00"}, upcoming

    updated = await rec.call(
        "update_bill", "PATCH", f"/bills/{light['id']}", reviewer,
        json={"amount": "125000.00", "due_date": "2026-12-06", "reference": "CONTRATO-77665544", "period": "2026-11"},
    )
    assert (updated["amount"], updated["due_date"], updated["reference"], updated["period"], updated["days_to_due"]) == ("125000.00", "2026-12-06", "****5544", "2026-11", 12), updated
    assert updated["id"] == light["id"] and updated["status"] == "pending", updated

    group = ledger.seed(amount="85400.00", date="2026-11-09T12:00:00-05:00", external_id="fa:bill-contract-water", category="Servicios")
    payment = await make_inbox(
        env.service, source_ref="bill-contract-water", merchant="AGUAS DE CARTAGENA", amount=Decimal("85400.00"),
        occurred_at=PAID_AT, firefly_transaction_id=group,
    )
    paid = await rec.call("mark_bill_paid", "POST", f"/bills/{water['id']}/mark-paid", reviewer, json={"item_id": payment.id})
    assert (paid["status"], paid["paid_item_id"], paid["id"]) == ("paid", payment.id, water["id"]) and paid["paid_at"], paid
    twice = await rec.call(None, "POST", f"/bills/{water['id']}/mark-paid", reviewer, status=409, json={})
    assert twice["code"] == "already_paid", twice

    assert await rec.call("delete_bill", "DELETE", f"/bills/{light['id']}", reviewer) == {"deleted": True}
    gone = await rec.call(None, "DELETE", f"/bills/{light['id']}", reviewer, status=404)
    assert gone["code"] == "bill_not_found", gone
