"""Phase 5 A7: quick entry, suggestions, auto-approval pause and widget summary (contract 13, 14)."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.config import OwnAccount
from app.db.models import InboxItem
from app.services import flags, quick, suggestions

ADMIN_KEY = "test-admin-key-01234567890123456789"
PHONE_KEY = "test-phone-key-01234567890123456789"
HEADERS = {"X-API-Key": ADMIN_KEY}
ACCOUNTS = [OwnAccount(firefly_account="Nequi", bank="nequi", kind="wallet", aliases=["nequi"])]
_refs = count()


async def row(db, merchant="TIENDA", category=None, status="created", source="manual", **changes):
    values = dict(
        source=source, source_ref=f"test:{next(_refs)}:{uuid4().hex}", sender="owner", subject_or_title="",
        status=status, merchant=merchant, category=category, amount=Decimal("12000.00"), currency="COP",
        asset_account="Nequi", transaction_type="withdrawal", method="manual", occurred_at=datetime.now(UTC),
    )
    values.update(changes)
    item = InboxItem(**values)
    db.add(item)
    await db.commit()
    return item


# --- quick.parse -----------------------------------------------------------------------------

@pytest.mark.parametrize(("text", "amount", "merchant", "account"), [
    ("25k taxi", "25000.00", "TAXI", None),
    ("taxi 25.000", "25000.00", "TAXI", None),
    ("$1.2m arriendo", "1200000.00", "ARRIENDO", None),
    ("25000 almuerzo nequi", "25000.00", "ALMUERZO", "Nequi"),
    ("1,5m arriendo", "1500000.00", "ARRIENDO", None),
    ("25 mil taxi", "25000.00", "TAXI", None),
    ("2 millones arriendo", "2000000.00", "ARRIENDO", None),
    ("12.500,50 cafe", "12500.50", "CAFE", None),
    ("cena 2 personas 80k", "80000.00", "CENA 2 PERSONAS", None),
    ("  $ 4.500   tinto  ", "4500.00", "TINTO", None),
    ("mercado 150000 nequi", "150000.00", "MERCADO", "Nequi"),
])
def test_parse_cases(text, amount, merchant, account):
    entry = quick.parse(text, ACCOUNTS)
    assert entry.amount == Decimal(amount)
    assert entry.merchant == merchant
    assert entry.account == account


def test_parse_amount_only_keeps_original_text_as_description():
    entry = quick.parse("1,5m", ACCOUNTS)
    assert entry.amount == Decimal("1500000.00")
    assert entry.description == "1,5m" and entry.merchant is None


def test_parse_description_is_the_remaining_text():
    assert quick.parse("taxi 25.000 nequi", ACCOUNTS).description == "taxi"


@pytest.mark.parametrize("text", ["taxi", "", "   ", "0 taxi", "almuerzo en el centro", "taxi 25kilos"])
def test_parse_without_amount_fails(text):
    with pytest.raises(quick.UnparseableError):
        quick.parse(text, ACCOUNTS)


# --- POST /transactions/quick ------------------------------------------------------------------

async def test_quick_is_idempotent_by_client_ref(client, clients, db):
    body = {"text": "25k taxi", "client_ref": "abc-1"}
    first = await client.post("/api/v1/transactions/quick", headers=HEADERS, json=body)
    second = await client.post("/api/v1/transactions/quick", headers=HEADERS, json=body)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    data = first.json()
    assert Decimal(str(data["amount"])) == Decimal("25000") and data["merchant"] == "TAXI"
    assert data["status"] == "created" and data["source_ref"] == "manual:quick:abc-1"
    clients[0].create_transaction.assert_awaited_once()
    split = clients[0].create_transaction.call_args.args[0]["transactions"][0]
    assert split["source_id"] == "1"  # MANUAL_DEFAULT_ACCOUNT (Nequi)
    assert len((await db.scalars(select(InboxItem))).all()) == 1


async def test_quick_uses_known_category_and_account_alias(client, clients, db):
    await row(db, merchant="TAXI", category="Transporte")
    response = await client.post("/api/v1/transactions/quick", headers=HEADERS, json={"text": "taxi 30k nequi", "client_ref": "abc-2"})
    assert response.status_code == 201
    assert response.json()["category"] == "Transporte" and response.json()["asset_account"] == "Nequi"
    split = clients[0].create_transaction.call_args.args[0]["transactions"][0]
    assert split["category_name"] == "Transporte"


async def test_quick_skips_category_missing_in_firefly(client, clients, db):
    await row(db, merchant="TAXI", category="Categoria inexistente")
    response = await client.post("/api/v1/transactions/quick", headers=HEADERS, json={"text": "taxi 30k", "client_ref": "abc-3"})
    assert response.status_code == 201 and response.json()["category"] is None


async def test_quick_never_uses_overall_category_fallback(client, clients, db):
    await row(db, merchant="OTRO", category="Transporte")
    response = await client.post("/api/v1/transactions/quick", headers=HEADERS, json={"text": "taxi 30k", "client_ref": "abc-7"})
    assert response.status_code == 201 and response.json()["category"] is None
    assert "category_name" not in clients[0].create_transaction.call_args.args[0]["transactions"][0]


async def test_quick_without_amount_is_unparseable(client, clients, db):
    response = await client.post("/api/v1/transactions/quick", headers=HEADERS, json={"text": "taxi", "client_ref": "abc-4"})
    assert response.status_code == 422 and response.json()["code"] == "unparseable"
    clients[0].create_transaction.assert_not_awaited()
    assert (await db.scalars(select(InboxItem))).all() == []


async def test_quick_firefly_failure_is_502_like_manual(client, clients, service):
    clients[0].create_transaction.side_effect = OSError("offline")
    response = await client.post("/api/v1/transactions/quick", headers=HEADERS, json={"text": "25k taxi", "client_ref": "abc-5"})
    assert response.status_code == 502
    body = response.json()
    assert body["status"] == "failed" and body["code"] == "upstream_unavailable" and body["detail"]
    assert (await service.inbox.get(body["inbox_id"])).status == "failed"


@pytest.mark.parametrize(("text", "amount"), [("pago $45.000 ref 2000000", "45000.00"), ("pago 45k ref 2000000", "45000.00"), ("pago 45.000", "45000.00")])
def test_parse_marked_amount_wins_over_other_numbers(text, amount):
    assert quick.parse(text, ACCOUNTS).amount == Decimal(amount)


@pytest.mark.parametrize("text", ["pago 45.000 ref 2000000", "cena 2 personas 80000", "7 8"])
def test_parse_several_bare_numbers_asks_for_dollar_or_k(text):
    with pytest.raises(quick.UnparseableError, match=r"Indica el monto con \$ o k"):
        quick.parse(text, ACCOUNTS)


async def test_quick_with_several_bare_numbers_is_unparseable_and_writes_nothing(client, clients, db):
    response = await client.post("/api/v1/transactions/quick", headers=HEADERS, json={"text": "pago 45.000 ref 2000000", "client_ref": "amb-1"})
    assert response.status_code == 422 and response.json()["code"] == "unparseable"
    clients[0].create_transaction.assert_not_awaited()
    assert (await db.scalars(select(InboxItem))).all() == []


@pytest.mark.parametrize(("text", "status"), [("2m arriendo", "created"), ("2,1m arriendo", "needs_review")])
async def test_quick_above_review_threshold_waits_for_approval(client, clients, service, text, status):
    response = await client.post("/api/v1/transactions/quick", headers=HEADERS, json={"text": text, "client_ref": "big-1"})
    assert response.status_code == 201 and response.json()["status"] == status
    assert clients[0].create_transaction.await_count == (status == "created")
    if status == "needs_review":
        data = response.json()
        assert data["review_reason"] and data["firefly_transaction_id"] is None
        replay = await client.post("/api/v1/transactions/quick", headers=HEADERS, json={"text": text, "client_ref": "big-1"})
        assert replay.json()["id"] == data["id"] and replay.json()["status"] == "needs_review"
        approved = await service.review(await service.inbox.get(data["id"]), "approve")
        assert approved.status == "created"
        clients[0].create_transaction.assert_awaited_once()


async def test_quick_requires_review_scope(client):
    response = await client.post("/api/v1/transactions/quick", headers={"X-API-Key": PHONE_KEY}, json={"text": "25k taxi", "client_ref": "abc-6"})
    assert response.status_code == 403


# --- suggestions -------------------------------------------------------------------------------

async def test_suggest_orders_by_merchant_frequency_then_overall(db):
    for category in ["Alimentación", "Alimentación", "Alimentación", "Transporte"]:
        await row(db, merchant="TIENDA", category=category)
    for _ in range(5):
        await row(db, merchant="OTRO", category="Salud")
    await row(db, merchant="TIENDA", category="Ocio", status="rejected")
    target = await row(db, merchant="TIENDA", status="needs_review")
    assert await suggestions.suggest(db, target) == ["Alimentación", "Transporte", "Salud"]
    assert await suggestions.suggest(db, target, limit=1) == ["Alimentación"]


async def test_suggest_puts_rules_first_without_duplicates(db):
    from app.services import rules

    if not hasattr(rules, "create_rule"):
        pytest.skip("rules service not available")
    for _ in range(2):
        await row(db, merchant="TIENDA", category="Alimentación")
    await rules.create_rule(db, match="merchant_exact", pattern="TIENDA", category="Hogar")
    await rules.create_rule(db, match="merchant_contains", pattern="tien", category="Alimentación")
    await db.commit()
    target = await row(db, merchant="TIENDA", status="needs_review")
    assert await suggestions.suggest(db, target) == ["Hogar", "Alimentación"]


async def test_question_for_needs_review_item_without_category(db):
    await row(db, merchant="TIENDA", category="Alimentación")
    target = await row(db, merchant="TIENDA", status="needs_review", amount=Decimal("12000.00"))
    question = await suggestions.question_for(db, target)
    assert question == {"question": "¿Este cobro de TIENDA por $12.000 es Alimentación?", "options": ["Alimentación"]}


async def test_question_for_returns_none_when_not_applicable(db):
    await row(db, merchant="TIENDA", category="Alimentación")
    assert await suggestions.question_for(db, await row(db, merchant="TIENDA", status="created")) is None
    assert await suggestions.question_for(db, await row(db, merchant="TIENDA", status="needs_review", category="Salud")) is None
    assert await suggestions.question_for(db, await row(db, merchant="TIENDA", status="needs_review", asset_account=None)) is None
    assert await suggestions.question_for(db, await row(db, merchant=None, status="needs_review")) is None
    assert await suggestions.question_for(db, await row(db, merchant="TIENDA", status="needs_review", amount=None)) is None


async def test_suggestions_endpoint(client, db):
    await row(db, merchant="TIENDA", category="Alimentación")
    target = await row(db, merchant="TIENDA", status="needs_review")
    response = await client.get(f"/api/v1/inbox/{target.id}/suggestions", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"item_id": target.id, "categories": ["Alimentación"]}
    assert (await client.get("/api/v1/inbox/999999/suggestions", headers=HEADERS)).status_code == 404


# --- auto-approval pause -------------------------------------------------------------------------

async def test_pause_forces_review_and_resume_restores_it(service, clients, db):
    async def notification(ref):
        item = await service.inbox.add(
            source="notification", source_ref=ref, sender="com.nequi.MobileApp", app="com.nequi.MobileApp",
            raw_text="Pagaste $12.000 en SUPERMERCADO CENTRAL", subject_or_title="Compra",
            occurred_at=datetime.now(UTC), status="pending",
        )
        await db.commit()
        return item

    suspect = await row(db, status="needs_review")
    await flags.mark_suspicious(db, suspect)
    await db.commit()
    assert (await flags.status(db))["auto_approve_paused"] is True

    paused = await service.process(await notification("n:paused"))
    assert paused.status == "needs_review"
    assert "auto-approval paused after suspicious report" in paused.review_reason
    clients[0].create_transaction.assert_not_awaited()

    await flags.resume(db)
    await db.commit()
    assert (await flags.status(db)) == {"auto_approve_paused": False, "paused_at": None, "paused_reason": None}
    resumed = await service.process(await notification("n:resumed"))
    assert resumed.status == "created"


async def test_policy_reasons_neutral_for_manual_items_and_when_not_paused(db):
    notification = await row(db, source="notification")
    manual = await row(db, source="manual")
    assert await flags.policy_reasons(db, notification) == []
    await flags.mark_suspicious(db, notification)
    await db.commit()
    assert await flags.policy_reasons(db, notification) == ["auto-approval paused after suspicious report"]
    assert await flags.policy_reasons(db, manual) == []


async def test_mark_suspicious_records_pause_time_and_reason(db):
    item = await row(db, source="notification")
    await flags.mark_suspicious(db, item)
    await db.commit()
    state = await flags.status(db)
    assert state["auto_approve_paused"] is True and state["paused_reason"]
    assert datetime.fromisoformat(state["paused_at"]).tzinfo is not None


async def test_flags_endpoints(client, db):
    assert (await client.get("/api/v1/flags", headers=HEADERS)).json() == {"auto_approve_paused": False, "paused_at": None, "paused_reason": None}
    await flags.mark_suspicious(db, await row(db, source="notification"))
    await db.commit()
    assert (await client.get("/api/v1/flags", headers=HEADERS)).json()["auto_approve_paused"] is True
    assert (await client.post("/api/v1/flags/auto-approve/resume", headers={"X-API-Key": PHONE_KEY})).status_code == 403
    resumed = await client.post("/api/v1/flags/auto-approve/resume", headers=HEADERS)
    assert resumed.status_code == 200 and resumed.json() == {"paused": False}
    assert (await client.get("/api/v1/flags", headers=HEADERS)).json()["auto_approve_paused"] is False


# --- GET /widgets/summary -------------------------------------------------------------------------

FORECAST = {
    "month": "2026-11", "as_of": "2026-11-22T10:00:00-05:00", "currency": "COP", "income": "5000000.00",
    "spent": "1250000.50", "balance": "3749999.50", "projected_spent": "2000000.00", "projected_balance": "3000000.00",
    "days_elapsed": 22, "days_left": 8, "daily_available": "150000.00", "spent_today": "20000.00",
}


@pytest.fixture
def firefly_budgets(monkeypatch):
    from app.api.routers import widgets

    async def forecast(session, settings, day=None, tag=None):
        return FORECAST

    state = SimpleNamespace(rows=[
        {"name": "Mercado", "spent": Decimal("90000"), "limit": Decimal("100000")},
        {"name": "Ocio", "spent": Decimal("10000"), "limit": Decimal("100000")},
        {"name": "Transporte", "spent": Decimal("60000"), "limit": Decimal("100000")},
        {"name": "Salud", "spent": Decimal("30000"), "limit": Decimal("100000")},
        {"name": "Sin tope", "spent": Decimal("5000"), "limit": Decimal("0")},
    ], error=None)

    async def budget_progress(start, end):
        if state.error:
            raise state.error
        return state.rows

    monkeypatch.setattr("app.services.reports.forecast", forecast)
    monkeypatch.setattr(widgets, "get_firefly", lambda: SimpleNamespace(budget_progress=budget_progress))
    return state


async def summary(client):
    response = await client.get("/api/v1/widgets/summary", headers=HEADERS)
    assert response.status_code == 200
    return response.json()


async def test_summary_has_every_contract_key(client, db, firefly_budgets):
    for index in range(6):
        await row(db, merchant=f"M{index}", category="Salud", occurred_at=datetime.now(UTC) - timedelta(hours=index))
    await row(db, merchant="PENDIENTE", status="needs_review")
    data = await summary(client)
    assert set(data) == {"generated_at", "currency", "month", "daily", "pending_review", "budgets", "recent", "system"}
    assert datetime.fromisoformat(data["generated_at"]).tzinfo is not None and data["currency"] == "COP"
    assert data["month"] == {"income": "5000000.00", "expenses": "1250000.50", "balance": "3749999.50"}
    assert data["daily"] == {"available": "150000.00", "spent_today": "20000.00", "days_left": 8}
    assert data["pending_review"] == 1
    assert [budget["name"] for budget in data["budgets"]] == ["Mercado", "Transporte", "Salud"]
    assert data["budgets"][0] == {"name": "Mercado", "spent": "90000.00", "limit": "100000.00", "pct": 90.0}
    assert len(data["recent"]) == 5
    assert [entry["merchant"] for entry in data["recent"]] == ["M0", "M1", "M2", "M3", "M4"]
    assert set(data["recent"][0]) == {"id", "occurred_at", "merchant", "amount", "currency", "transaction_type", "category"}
    assert data["recent"][0]["amount"] == "12000.00"
    assert set(data["system"]) == {"status", "last_event_at"}


async def test_summary_budgets_fall_back_to_empty_when_firefly_fails(client, firefly_budgets):
    firefly_budgets.error = OSError("offline")
    assert (await summary(client))["budgets"] == []


async def test_summary_system_green_when_empty(client, firefly_budgets):
    assert (await summary(client))["system"] == {"status": "green", "last_event_at": None}


async def test_summary_system_red_on_failed_item(client, db, firefly_budgets):
    await row(db, status="failed")
    assert (await summary(client))["system"]["status"] == "red"


async def test_summary_system_red_on_phone_silence(client, db, firefly_budgets):
    silent_since = datetime.now(UTC) - timedelta(hours=30)
    await row(db, source="sms", created_at=silent_since)
    system = (await summary(client))["system"]
    assert system["status"] == "red" and datetime.fromisoformat(system["last_event_at"]) == silent_since


async def test_summary_system_yellow_on_stale_review(client, db, firefly_budgets):
    await row(db, source="sms", status="needs_review", created_at=datetime.now(UTC) - timedelta(hours=3))
    assert (await summary(client))["system"]["status"] == "yellow"


async def test_summary_recent_review_is_still_green(client, db, firefly_budgets):
    await row(db, source="sms", status="needs_review", created_at=datetime.now(UTC) - timedelta(minutes=30))
    assert (await summary(client))["system"]["status"] == "green"


async def test_summary_requires_read_scope(client, firefly_budgets):
    assert (await client.get("/api/v1/widgets/summary", headers={"X-API-Key": PHONE_KEY})).status_code == 403
