"""AI budget guard, AI suggestions for the review queue and rules that learn by themselves (Firefly and LLM mocked)."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.api.dependencies import hash_secret
from app.api.routers import inbox as inbox_router
from app.clients.llm import LLMClient
from app.core.timezone import now_utc
from app.db.models import AiSuggestion, AiUsage, MerchantCorrection
from app.db.models_assistant import Audit
from app.db.models_devices import Device
from app.db.models_rules import Rule
from app.services import ai_budget, ai_review, rules

ADMIN = {"X-API-Key": "test-admin-key-01234567890123456789"}
API = "/api/v1"


# --- helpers ----------------------------------------------------------------------------------------------------------


async def review_item(service, **changes):
    """An automatic item waiting for a human decision."""
    values = dict(
        source="sms", source_ref=str(uuid4()), sender="85954", subject_or_title="Compra", raw_text="Compraste $12.000 en TIENDA UNO",
        occurred_at=now_utc() - timedelta(hours=1), status="needs_review", method="parser", transaction_type="withdrawal",
        amount=Decimal("12000.00"), currency="COP", merchant="TIENDA UNO", asset_account="Nequi", review_reason="merchant is not clean",
    )
    values.update(changes)
    row = await service.inbox.add(**values)
    await service.session.commit()
    return row


async def created_item(service, **changes):
    values = dict(status="created", review_reason=None, firefly_transaction_id=str(uuid4()), category="Alimentación") | changes
    return await review_item(service, **values)


async def device(db, name, scopes):
    key = f"{name}-key-" + "0123456789" * 3
    db.add(Device(name=name, key_hash=hash_secret(key), scopes=scopes))
    await db.commit()
    return {"X-API-Key": key}


async def audits(db, action):
    return list((await db.scalars(select(Audit).where(Audit.action == action).order_by(Audit.id))).all())


def answer(**fields):
    return {
        "action": "approve", "firefly_account": "Nequi", "category": "Alimentación", "merchant": "Tienda Uno",
        "transaction_type": "withdrawal", "amount": None, "duplicate_of_id": None, "confidence": 0.9, "reason": "Comercio habitual",
    } | fields


@pytest.fixture(autouse=True)
def isolated_budget_settings(settings, monkeypatch):
    """The guard reads the server's LLM key from the settings: never from a developer's real environment."""
    monkeypatch.setattr(ai_budget, "get_settings", lambda: settings)


@pytest.fixture
def background(monkeypatch, db, service):
    """Run the owner-requested background job on the test session and the mocked clients."""
    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(inbox_router, "get_db_session", session)
    monkeypatch.setattr(inbox_router, "get_firefly", lambda: service.firefly)
    monkeypatch.setattr(inbox_router, "get_llm", lambda: service.llm)
    inbox_router._suggest_task = None
    yield
    inbox_router._suggest_task = None


async def suggest(client):
    response = await client.post(f"{API}/inbox/ai-suggest", headers=ADMIN)
    assert response.status_code == 202, response.text
    if inbox_router._suggest_task is not None:
        await inbox_router._suggest_task
    return response.json()


async def listing(client, **params):
    response = await client.get(f"{API}/inbox/ai-suggestions", headers=ADMIN, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def settings_with_key(monkeypatch, key="go-secret"):
    config = SimpleNamespace(timezone="America/Bogota", llm_api_key=SecretStr(key), llm_user_agent="finance-analyzer/3.0")
    monkeypatch.setattr(ai_budget, "get_settings", lambda: config)
    return config


# --- 1. budget guard --------------------------------------------------------------------------------------------------


async def test_record_counts_by_day_and_kind_with_tokens_and_allowance(db):
    await ai_budget.record(db, "extract", 2, {"total_tokens": 100, "allowance": {"remaining": "0.9"}})
    await ai_budget.record(db, "extract", 1, {"prompt_tokens": 7, "completion_tokens": 3})
    await ai_budget.record(db, "ai_suggest", 4, None, auto=False)
    await db.commit()
    rows = {row.kind: row for row in (await db.scalars(select(AiUsage))).all()}
    assert (rows["extract"].calls, rows["extract"].auto_calls, rows["extract"].tokens) == (3, 3, 110)
    assert rows["extract"].allowance == {"remaining": "0.9"}
    assert (rows["ai_suggest"].calls, rows["ai_suggest"].auto_calls) == (4, 0)
    assert rows["extract"].day == ai_budget.today()


async def test_daily_limit_brakes_only_automatic_calls(db, client):
    assert (await client.get(f"{API}/settings/ai-budget", headers=ADMIN)).json()["daily_calls"] == 400
    await ai_budget.record(db, "extract", 3, None)
    await ai_budget.record(db, "ai_suggest", 50, None, auto=False)  # on demand: never counts against the limit
    await db.commit()
    assert await ai_budget.allow(db, "ai_suggest", 1)
    response = await client.put(f"{API}/settings/ai-budget", headers=ADMIN, json={"daily_calls": 4})
    assert response.status_code == 200 and response.json()["daily_calls"] == 4
    assert await ai_budget.allow(db, "ai_suggest", 1)
    assert not await ai_budget.allow(db, "ai_suggest", 2)
    await ai_budget.record(db, "extract", 1, None)
    assert not await ai_budget.allow(db, "ai_suggest", 1)
    usage = (await client.get(f"{API}/ai/usage", headers=ADMIN)).json()
    assert usage["throttled"] is True and usage["throttle_reason"] == "daily_limit" and usage["daily_limit"] == 4
    entry = (await audits(db, "settings.ai_budget"))[0]
    assert entry.detail == {"daily_calls": 4, "previous": 400}


async def test_ai_budget_settings_validation_and_scopes(client, db):
    assert (await client.put(f"{API}/settings/ai-budget", headers=ADMIN, json={"daily_calls": -1})).status_code == 422
    assert (await client.put(f"{API}/settings/ai-budget", headers=ADMIN, json={"daily_calls": 5, "extra": 1})).status_code == 422
    reader = await device(db, "reader", ["read"])
    reviewer = await device(db, "reviewer", ["review"])
    assert (await client.get(f"{API}/settings/ai-budget", headers=reader)).status_code == 200
    assert (await client.get(f"{API}/ai/usage", headers=reader)).status_code == 200
    assert (await client.put(f"{API}/settings/ai-budget", headers=reader, json={"daily_calls": 5})).status_code == 403
    assert (await client.put(f"{API}/settings/ai-budget", headers=reviewer, json={"daily_calls": 5})).status_code == 200


async def test_usage_report_today_and_month_by_kind(client, db):
    await ai_budget.record(db, "extract", 5, {"total_tokens": 50})
    await ai_budget.record(db, "ai_suggest", 2, {"allowance": {"left": 1}}, auto=False)
    # yesterday's row belongs to the month (when it is the same month) but not to today
    day = ai_budget.today()
    if day.day > 1:
        db.add(AiUsage(day=day - timedelta(days=1), kind="extract", calls=10, auto_calls=10, tokens=0))
    await db.commit()
    body = (await client.get(f"{API}/ai/usage", headers=ADMIN)).json()
    assert body["today"]["day"] == day.isoformat()
    assert body["today"]["by_kind"] == {"ai_suggest": {"calls": 2, "auto_calls": 0, "tokens": 0}, "extract": {"calls": 5, "auto_calls": 5, "tokens": 50}}
    assert body["today"]["calls"] == 7 and body["today"]["auto_calls"] == 5
    assert body["month"]["month"] == day.strftime("%Y-%m")
    assert body["month"]["calls"] == 7 + (10 if day.day > 1 else 0)
    assert body["allowance"] == {"left": 1}
    assert body["throttled"] is False and body["throttle_reason"] is None and body["go_usage"] is None


async def test_go_usage_brakes_at_rolling_60_or_weekly_70_and_is_fetched_every_15_minutes(db, client, monkeypatch):
    settings_with_key(monkeypatch)
    calls = []
    document = {"rolling": {"percent": 12}, "weekly": {"percent": 30}}

    async def fake(settings):
        calls.append(settings.llm_api_key.get_secret_value())
        return document

    monkeypatch.setattr(ai_budget, "fetch_go_usage", fake)
    assert await ai_budget.allow(db, "ai_suggest")
    assert await ai_budget.allow(db, "ai_suggest")
    assert calls == ["go-secret"]  # the second check reuses the stored document

    async def expire():
        stored = await ai_budget._flag(db, ai_budget.GO_USAGE_KEY)
        await ai_budget._write_flag(db, ai_budget.GO_USAGE_KEY, {**stored, "fetched_at": (now_utc() - timedelta(minutes=16)).isoformat()})

    await expire()
    document.update(rolling={"percent": 60}, weekly={"percent": 30})
    assert not await ai_budget.allow(db, "ai_suggest")
    assert len(calls) == 2
    body = (await client.get(f"{API}/ai/usage", headers=ADMIN)).json()
    assert body["throttled"] is True and body["throttle_reason"] == "go_rolling"
    assert body["go_usage"]["data"] == document and body["go_usage"]["error"] is None
    await expire()
    document.update(rolling={"percent": 59.9}, weekly={"percent": 70})
    assert not await ai_budget.allow(db, "ai_suggest")
    assert (await client.get(f"{API}/ai/usage", headers=ADMIN)).json()["throttle_reason"] == "go_weekly"
    await expire()
    document.update(rolling={"percent": 59.9}, weekly={"percent": 69.9})
    assert await ai_budget.allow(db, "ai_suggest")
    assert len(calls) == 4


async def test_go_usage_failure_never_blocks_and_is_not_retried_for_15_minutes(db, monkeypatch):
    settings_with_key(monkeypatch)
    calls = []

    async def failing(settings):
        calls.append(1)
        raise httpx.ConnectError("down")

    monkeypatch.setattr(ai_budget, "fetch_go_usage", failing)
    assert await ai_budget.allow(db, "ai_suggest")
    assert await ai_budget.allow(db, "ai_suggest")
    assert len(calls) == 1
    stored = await ai_budget._flag(db, ai_budget.GO_USAGE_KEY)
    assert stored["data"] is None and stored["error"] == "ConnectError"


async def test_go_usage_without_a_key_is_never_fetched(db, monkeypatch):
    async def never(settings):
        raise AssertionError("no key, no request")

    monkeypatch.setattr(ai_budget, "fetch_go_usage", never)
    assert await ai_budget.allow(db, "ai_suggest")


async def test_fetch_go_usage_asks_the_usage_endpoint_with_the_server_key(monkeypatch):
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers["authorization"]))
        return httpx.Response(200, json={"rolling": {"percent": 5}})

    real = httpx.AsyncClient
    monkeypatch.setattr(ai_budget.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler)))
    config = settings_with_key(monkeypatch, "server-key")
    assert await ai_budget.fetch_go_usage(config) == {"rolling": {"percent": 5}}
    assert seen == [("https://opencode.ai/inference/go/v1/usage", "Bearer server-key")]


def llm_settings():
    return SimpleNamespace(
        llm_base_url="https://llm.test/v1", llm_api_key=SecretStr("primary"), llm_models="cheap-a,cheap-b", llm_user_agent="finance-analyzer/3.0",
        llm_fallback_base_url="", llm_fallback_api_key=SecretStr(""), llm_fallback_model="",
    )


async def with_transport(client, handler):
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_llm_client_captures_usage_calls_tokens_and_allowance():
    client = LLMClient(llm_settings())

    def handler(request):
        model = json.loads(request.content)["model"]
        content = {"kind": "nonsense"} if model == "cheap-a" else {"kind": "review"}
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "opencode": {"allowance": {"remaining": 0.8}},
        })

    await with_transport(client, handler)
    try:
        usage = {}
        await client.analyze("texto", "cycle", usage=usage)
        assert usage == {"calls": 2, "prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30, "allowance": {"remaining": 0.8}, "model": "cheap-b"}
        assert await client.complete_json("prompt", {"x": 1}, "cycle") == {"kind": "nonsense"}  # any JSON object; the caller validates
    finally:
        await client.close()


async def test_llm_client_usage_survives_an_exhausted_chain():
    client = LLMClient(llm_settings())
    await with_transport(client, lambda request: httpx.Response(500))
    try:
        usage = {}
        with pytest.raises(RuntimeError):
            await client.complete_json("prompt", {}, "cycle", usage=usage)
        assert usage == {"calls": 2}
    finally:
        await client.close()


# --- 2. AI suggestions ------------------------------------------------------------------------------------------------


async def test_suggest_lists_proposals_with_redacted_evidence_and_20_similar(client, service, clients, db, background):
    _, llm = clients
    for _ in range(25):
        await created_item(service)
    item = await review_item(
        service, raw_text="Pagaste $12.000 a MARIA LOPEZ con cuenta 12345678901 correo ana@example.com", sender="85954",
        subject_or_title="Aviso a ana@example.com",
    )
    item_id = item.id
    sent = []

    async def fake(prompt, content, cycle_id, usage=None):
        sent.append(content)
        usage.update(calls=1, total_tokens=40, model="deepseek")
        return answer()

    llm.complete_json.side_effect = fake
    assert await suggest(client) == {"queued": 1}
    assert len(sent) == 1
    wire = json.dumps(sent[0], ensure_ascii=False)
    assert "12345678901" not in wire and "ana@example.com" not in wire and "MARIA LOPEZ" not in wire
    assert "[EMAIL]" in wire and "[NOMBRE]" in wire
    similar = sent[0]["UNTRUSTED_DATA"]["similar"]
    assert len(similar) == 20 and item_id not in {row["id"] for row in similar}
    assert [row["id"] for row in similar] == sorted((row["id"] for row in similar), reverse=True)
    context = sent[0]["CONTEXT"]
    assert [a["name"] for a in context["own_accounts"]] == ["Nequi"] and context["categories"] == ["Alimentación", "Transporte"]
    assert "UNTRUSTED_DATA" in llm.complete_json.call_args.args[0]

    body = await listing(client)
    assert body["total"] == 1
    entry = body["items"][0]
    assert entry["status"] == "pending" and entry["confidence"] == 0.9 and entry["reason"] == "Comercio habitual" and entry["model"] == "deepseek"
    assert entry["item"]["id"] == item_id and entry["item"]["status"] == "needs_review"
    assert entry["proposal"] == {
        "action": "approve", "firefly_account": "Nequi", "category": "Alimentación", "merchant": "TIENDA UNO",
        "transaction_type": "withdrawal", "amount": None, "duplicate_of_id": None,
    }
    usage = (await db.scalars(select(AiUsage).where(AiUsage.kind == "ai_suggest"))).one()
    assert (usage.calls, usage.auto_calls, usage.tokens) == (1, 0, 40)  # owner-requested: counted, never braked

    assert await suggest(client) == {"queued": 0}  # a current suggestion exists
    assert len(sent) == 1


async def test_suggest_skips_manual_items_and_failed_sender_authentication(client, service, clients, background):
    _, llm = clients
    await review_item(service, source="manual", source_ref="manual:1")
    await review_item(service, review_reason="sender_auth_failed")
    await review_item(service, method="review")
    await created_item(service)
    assert await suggest(client) == {"queued": 0}
    llm.complete_json.assert_not_called()


async def test_proposal_is_checked_against_own_accounts_categories_and_the_message(client, service, clients, background):
    _, llm = clients
    await review_item(service, amount=None, raw_text="Pago por $45.000 en TIENDA UNO")
    llm.complete_json.return_value = answer(firefly_account="Cuenta Inventada", category="Inventada", amount=45000, transaction_type="gift", merchant="x" * 80)
    await suggest(client)
    proposal = (await listing(client))["items"][0]["proposal"]
    assert proposal["firefly_account"] is None and proposal["category"] is None and proposal["transaction_type"] is None
    assert proposal["merchant"] is None  # not a clean merchant name
    assert proposal["amount"] == "45000.00"  # the message really says 45.000

    second = await review_item(service, amount=None, raw_text="Pago por $45.000 en TIENDA DOS", merchant="TIENDA DOS")
    llm.complete_json.return_value = answer(amount=99999)
    await suggest(client)
    entry = next(row for row in (await listing(client))["items"] if row["item"]["id"] == second.id)
    assert entry["proposal"]["amount"] is None  # not in the text: dropped


async def test_incomplete_approvals_never_look_certain(client, service, clients, background):
    _, llm = clients
    await review_item(service, asset_account=None)
    llm.complete_json.return_value = answer(firefly_account=None, confidence=0.99)
    await suggest(client)
    assert (await listing(client))["items"][0]["confidence"] == ai_review.INCOMPLETE_CONFIDENCE


async def test_unusable_model_answers_become_error_rows_and_are_retried_on_demand(client, service, clients, db, background):
    _, llm = clients
    first, second = await review_item(service), await review_item(service)
    llm.complete_json.side_effect = [{"action": "approve", "confidence": 7}, RuntimeError("LLM chain exhausted")]
    assert await suggest(client) == {"queued": 2}
    assert (await listing(client))["total"] == 0
    errors = (await listing(client, status="error"))["items"]
    assert {entry["item"]["id"] for entry in errors} == {first.id, second.id}
    assert {entry["error"] for entry in errors} == {"El modelo devolvió una respuesta no válida.", "El modelo no está disponible."}
    usage = (await db.scalars(select(AiUsage).where(AiUsage.kind == "ai_suggest"))).one()
    assert usage.calls == 2
    llm.complete_json.side_effect = None
    llm.complete_json.return_value = answer()
    assert await suggest(client) == {"queued": 2}  # the owner's run retries errors
    assert (await listing(client))["total"] == 2


async def test_apply_by_id_registers_through_the_review_path(client, service, clients, db, background):
    firefly, llm = clients
    item = await review_item(service, category=None)
    item_id = item.id
    llm.complete_json.return_value = answer(category="alimentación")
    await suggest(client)
    suggestion_id = (await listing(client))["items"][0]["id"]
    response = await client.post(f"{API}/inbox/ai-suggestions/apply", headers=ADMIN, json={"ids": [suggestion_id, 9999]})
    assert response.status_code == 200
    assert response.json() == {"applied": [suggestion_id], "failed": [{"id": 9999, "detail": "La sugerencia no existe."}]}
    split = firefly.create_transaction.call_args.args[0]["transactions"][0]
    assert split["category_name"] == "Alimentación"
    saved = await service.inbox.get(item_id)
    assert saved.status == "created" and saved.category == "Alimentación" and saved.method == "parser"
    approval = (await audits(db, "inbox.approve"))[0]
    assert approval.detail["changes"]["category"] == {"old": None, "new": "Alimentación"}
    applied = (await audits(db, "ai_suggestion.apply"))[0]
    assert applied.target_id == str(suggestion_id) and applied.detail["via"] == "ids" and applied.detail["item_id"] == item_id
    assert (await listing(client))["total"] == 0 and (await listing(client, status="applied"))["total"] == 1
    again = await client.post(f"{API}/inbox/ai-suggestions/apply", headers=ADMIN, json={"ids": [suggestion_id]})
    assert again.json()["failed"] == [{"id": suggestion_id, "detail": "La sugerencia ya no está vigente."}]


async def test_apply_all_above_never_applies_amount_or_void_proposals(client, service, clients, db, background):
    firefly, llm = clients
    sure = await review_item(service, merchant="TIENDA UNO", raw_text="Compra TIENDA UNO")
    unsure = await review_item(service, merchant="CAFE DOS", raw_text="Compra CAFE DOS")
    void = await review_item(service, merchant="PROMO", raw_text="Promoción")
    amount = await review_item(service, merchant="TIENDA TRES", amount=None, raw_text="Pago por $45.000 en TIENDA TRES")
    ids = {"sure": sure.id, "unsure": unsure.id, "void": void.id, "amount": amount.id}
    by_merchant = {
        "TIENDA UNO": answer(confidence=0.95), "CAFE DOS": answer(confidence=0.7, merchant="Cafe Dos"),
        "PROMO": answer(action="ignore", confidence=0.99, reason="Es publicidad"),
        "TIENDA TRES": answer(amount=45000, confidence=0.99, merchant="Tienda Tres"),
    }

    async def fake(prompt, content, cycle_id, usage=None):
        return by_merchant[content["UNTRUSTED_DATA"]["item"]["merchant"]]

    llm.complete_json.side_effect = fake
    assert await suggest(client) == {"queued": 4}
    body = await listing(client)
    assert [entry["item"]["id"] for entry in body["items"]][0] in {void.id, amount.id}  # most confident first
    suggestion = {entry["item"]["id"]: entry["id"] for entry in body["items"]}
    assert (await client.post(f"{API}/inbox/ai-suggestions/apply", headers=ADMIN, json={})).status_code == 422
    assert (await client.post(f"{API}/inbox/ai-suggestions/apply", headers=ADMIN, json={"all_above": 1.5})).status_code == 422

    response = await client.post(f"{API}/inbox/ai-suggestions/apply", headers=ADMIN, json={"all_above": 0.9})
    assert response.json() == {"applied": [suggestion[ids["sure"]]], "failed": []}
    statuses = {name: (await service.inbox.get(item_id)).status for name, item_id in ids.items()}
    assert statuses == {"sure": "created", "unsure": "needs_review", "void": "needs_review", "amount": "needs_review"}
    assert (await audits(db, "ai_suggestion.apply"))[0].detail["via"] == "all_above"

    # By id the owner can apply the annulment and the amount proposal.
    by_id = await client.post(f"{API}/inbox/ai-suggestions/apply", headers=ADMIN, json={"ids": [suggestion[ids["void"]], suggestion[ids["amount"]]]})
    assert by_id.json() == {"applied": [suggestion[ids["void"]], suggestion[ids["amount"]]], "failed": []}
    assert (await service.inbox.get(ids["void"])).status == "rejected"
    corrected = await service.inbox.get(ids["amount"])
    assert corrected.status == "created" and corrected.amount == Decimal("45000.00")
    assert firefly.create_transaction.call_count == 2


async def test_duplicate_suggestion_rejects_the_item_and_keeps_the_original_reference(client, service, clients, background):
    _, llm = clients
    original = await created_item(service)
    item = await review_item(service)
    original_id, item_id = original.id, item.id
    llm.complete_json.return_value = answer(action="duplicate", duplicate_of_id=original_id, confidence=0.8, reason="Mismo cobro")
    await suggest(client)
    entry = (await listing(client))["items"][0]
    assert entry["proposal"]["action"] == "duplicate" and entry["proposal"]["duplicate_of_id"] == original_id
    assert (await client.post(f"{API}/inbox/ai-suggestions/apply", headers=ADMIN, json={"all_above": 0})).json()["applied"] == []
    assert (await client.post(f"{API}/inbox/ai-suggestions/apply", headers=ADMIN, json={"ids": [entry["id"]]})).json()["applied"] == [entry["id"]]
    saved = await service.inbox.get(item_id)
    assert saved.status == "rejected" and saved.duplicate_of_id == original_id


async def test_a_duplicate_id_outside_the_similar_movements_is_dropped(client, service, clients, background):
    _, llm = clients
    await review_item(service)
    llm.complete_json.return_value = answer(action="duplicate", duplicate_of_id=424242)
    await suggest(client)
    assert (await listing(client))["items"][0]["proposal"]["duplicate_of_id"] is None


async def test_apply_reports_review_failures_without_stopping_the_batch(client, service, clients, background):
    firefly, llm = clients
    good, bad = await review_item(service, merchant="TIENDA UNO"), await review_item(service, merchant="CAFE DOS", source_ref="bad")
    bad_id = bad.id

    async def fake(prompt, content, cycle_id, usage=None):
        return answer(merchant="Cafe Dos", firefly_account=None) if content["UNTRUSTED_DATA"]["item"]["merchant"] == "CAFE DOS" else answer()

    llm.complete_json.side_effect = fake
    await suggest(client)
    entries = (await listing(client))["items"]
    suggestion = {entry["item"]["id"]: entry["id"] for entry in entries}
    saved_bad = await service.inbox.get(bad_id)
    saved_bad.asset_account = None  # the owner's own account is still unknown: review refuses it
    await service.session.commit()
    response = await client.post(f"{API}/inbox/ai-suggestions/apply", headers=ADMIN, json={"ids": [suggestion[bad_id], suggestion[good.id]]})
    body = response.json()
    assert body["applied"] == [suggestion[good.id]]
    assert body["failed"] == [{"id": suggestion[bad_id], "detail": "Review requires amount, merchant and own account"}]
    assert (await service.inbox.get(bad_id)).status == "needs_review"
    assert firefly.create_transaction.call_count == 1


async def test_discard_then_automatic_runs_leave_it_alone_but_the_owner_can_ask_again(client, service, clients, db, background):
    _, llm = clients
    item = await review_item(service)
    llm.complete_json.return_value = answer()
    await suggest(client)
    suggestion_id = (await listing(client))["items"][0]["id"]
    response = await client.post(f"{API}/inbox/ai-suggestions/{suggestion_id}/discard", headers=ADMIN)
    assert response.status_code == 200 and response.json()["status"] == "discarded" and response.json()["item"]["id"] == item.id
    assert (await audits(db, "ai_suggestion.discard"))[0].target_id == str(suggestion_id)
    again = await client.post(f"{API}/inbox/ai-suggestions/{suggestion_id}/discard", headers=ADMIN)
    assert again.status_code == 409 and again.json()["code"] == "suggestion_not_pending"
    assert (await client.post(f"{API}/inbox/ai-suggestions/9999/discard", headers=ADMIN)).status_code == 404
    assert (await listing(client))["total"] == 0
    assert await ai_review.eligible_ids(db, auto=True) == []
    assert await ai_review.eligible_ids(db, auto=False) == [item.id]


async def test_suggestions_of_items_that_left_review_become_stale(client, service, clients, background):
    _, llm = clients
    item = await review_item(service)
    llm.complete_json.return_value = answer()
    await suggest(client)
    assert (await listing(client))["total"] == 1
    saved = await service.inbox.get(item.id)
    saved.status = "rejected"
    await service.session.commit()
    assert (await listing(client))["total"] == 0
    assert (await listing(client, status="stale"))["total"] == 1


async def test_a_suggestion_older_than_the_last_change_of_its_item_is_suggested_again(client, service, clients, db, background):
    _, llm = clients
    item = await review_item(service)
    llm.complete_json.return_value = answer()
    await suggest(client)
    saved = await service.inbox.get(item.id)
    saved.review_reason = "changed afterwards"
    saved.updated_at = now_utc() + timedelta(minutes=1)
    await service.session.commit()
    assert await ai_review.eligible_ids(db, auto=True) == [item.id]


async def test_automatic_suggestions_after_a_cycle_respect_the_budget_guard(service, clients, db):
    _, llm = clients
    llm.complete_json.return_value = answer()
    first = await review_item(service)
    second = await review_item(service)
    await ai_budget.set_policy(db, 0)
    await db.commit()
    result = await ai_review.after_cycle(db, service.firefly, llm, service.settings)
    assert result["suggested"] == 0 and result["budget_stopped"] is True
    llm.complete_json.assert_not_called()
    await ai_budget.set_policy(db, 1)
    await db.commit()
    result = await ai_review.after_cycle(db, service.firefly, llm, service.settings)
    assert result["suggested"] == 1 and result["budget_stopped"] is True  # the second call would pass the daily limit
    assert llm.complete_json.call_count == 1
    usage = (await db.scalars(select(AiUsage).where(AiUsage.kind == "ai_suggest"))).one()
    assert (usage.calls, usage.auto_calls) == (1, 1)
    assert {row.item_id for row in (await db.scalars(select(AiSuggestion))).all()} <= {first.id, second.id}
    await ai_budget.set_policy(db, 400)
    await db.commit()
    result = await ai_review.after_cycle(db, service.firefly, llm, service.settings)
    assert result["suggested"] == 1 and len((await db.scalars(select(AiSuggestion))).all()) == 2
    assert await ai_review.after_cycle(db, service.firefly, llm, service.settings) == {"suggested": 0, "errors": 0, "skipped": 0, "budget_stopped": False}


async def test_the_processing_cycle_suggests_pending_items_and_a_failure_never_breaks_it(service, clients, db):
    _, llm = clients
    item = await review_item(service)
    llm.complete_json.return_value = answer()
    await service.cycle()
    assert (await db.scalar(select(AiSuggestion).where(AiSuggestion.item_id == item.id))).status == "pending"
    await db.execute(AiSuggestion.__table__.delete())
    await db.commit()
    llm.complete_json.side_effect = ValueError("boom")
    await service.cycle()  # the error row is stored; the cycle finishes
    assert (await db.scalar(select(AiSuggestion).where(AiSuggestion.item_id == item.id))).status == "error"


async def test_ai_review_routes_need_the_review_scope(client, db):
    reader = await device(db, "reader", ["read"])
    checks = [
        ("POST", f"{API}/inbox/ai-suggest", None), ("GET", f"{API}/inbox/ai-suggestions", None),
        ("POST", f"{API}/inbox/ai-suggestions/apply", {"ids": [1]}), ("POST", f"{API}/inbox/ai-suggestions/1/discard", None),
    ]
    for method, url, body in checks:
        response = await client.request(method, url, headers=reader, json=body)
        assert response.status_code == 403, url
        assert (await client.request(method, url, json=body)).status_code == 401, url


async def test_only_one_owner_run_at_a_time(client, service, clients, background):
    _, llm = clients
    await review_item(service)
    gate = asyncio.Event()

    async def slow(prompt, content, cycle_id, usage=None):
        await gate.wait()
        return answer()

    llm.complete_json.side_effect = slow
    first = await client.post(f"{API}/inbox/ai-suggest", headers=ADMIN)
    assert first.status_code == 202
    busy = await client.post(f"{API}/inbox/ai-suggest", headers=ADMIN)
    assert busy.status_code == 409 and busy.json()["code"] == "ai_suggest_running"
    gate.set()
    await inbox_router._suggest_task
    assert (await listing(client))["total"] == 1


# --- 3. rules that learn ----------------------------------------------------------------------------------------------


async def approve_with(client, item, category):
    response = await client.post(f"{API}/inbox/{item.id}/approve", headers=ADMIN, json={"category": category})
    assert response.status_code == 200, response.text
    return response.json()


async def learned_rules(db):
    return list((await db.scalars(select(Rule).order_by(Rule.id))).all())


async def test_two_corrections_to_the_same_category_create_a_learned_rule(client, service, db):
    await approve_with(client, await review_item(service, category=None), "Transporte")
    assert await learned_rules(db) == []
    streak = await db.scalar(select(MerchantCorrection))
    assert (streak.merchant, streak.category, streak.streak) == ("TIENDA UNO", "Transporte", 1)
    await approve_with(client, await review_item(service, category=None), "transporte")
    [rule] = await learned_rules(db)
    assert (rule.match, rule.pattern, rule.category, rule.source, rule.active, rule.merchant) == ("merchant_exact", "TIENDA UNO", "Transporte", "learned", True, "TIENDA UNO")
    entry = (await audits(db, "rule.learn"))[0]
    assert entry.target_id == str(rule.id) and entry.actor == "admin"
    assert entry.detail["category"] == "Transporte" and entry.detail["source"] == "learned" and entry.detail["pattern"] == "TIENDA UNO"
    listed = (await client.get(f"{API}/rules", headers=ADMIN)).json()
    assert [row["source"] for row in listed["items"]] == ["learned"]
    # The rule now categorizes the next item of that merchant by itself.
    fresh = await review_item(service, category=None)
    await rules.apply(db, fresh)
    assert fresh.category == "Transporte"


async def test_corrections_to_different_categories_never_add_up(client, service, db):
    for category in ("Transporte", "Alimentación", "Transporte", "Alimentación"):
        await approve_with(client, await review_item(service, category=None), category)
    assert await learned_rules(db) == []
    assert (await db.scalar(select(MerchantCorrection))).streak == 1


async def test_confirming_the_existing_category_is_not_a_correction(client, service, db):
    for _ in range(3):
        await approve_with(client, await review_item(service, category="Transporte"), "Transporte")
    assert await learned_rules(db) == [] and await db.scalar(select(MerchantCorrection)) is None


async def test_contradicting_a_learned_rule_deactivates_it_and_two_more_corrections_relearn_it(client, service, db):
    for _ in range(2):
        await approve_with(client, await review_item(service, category=None), "Transporte")
    [rule] = await learned_rules(db)
    rule_id = rule.id
    await approve_with(client, await review_item(service, category="Transporte"), "Alimentación")
    rule = await db.get(Rule, rule_id, populate_existing=True)
    assert rule.active is False and rule.category == "Transporte"
    entry = (await audits(db, "rule.deactivate"))[0]
    assert entry.target_id == str(rule_id) and entry.detail["contradicted_by"] == "Alimentación"
    assert (await client.get(f"{API}/rules", headers=ADMIN)).json()["total"] == 0  # inactive rules are not listed
    fresh = await review_item(service, category=None)
    await rules.apply(db, fresh)
    assert fresh.category is None  # an inactive rule no longer categorizes

    await approve_with(client, await review_item(service, category=None), "Alimentación")
    rules_now = await learned_rules(db)
    assert [row.id for row in rules_now] == [rule_id]  # the same row, reactivated with the new category
    assert rules_now[0].active is True and rules_now[0].category == "Alimentación" and rules_now[0].source == "learned"
    assert len(await audits(db, "rule.learn")) == 2


async def test_a_manual_rule_is_never_learned_over_or_deactivated(client, service, db):
    manual = await rules.create_rule(db, match="merchant_exact", pattern="TIENDA UNO", category="Transporte", source="manual")
    await db.commit()
    manual_id = manual.id
    for _ in range(3):
        await approve_with(client, await review_item(service, category="Transporte"), "Alimentación")
    [rule] = await learned_rules(db)
    assert rule.id == manual_id and rule.source == "manual" and rule.active is True and rule.category == "Transporte"
    assert await audits(db, "rule.learn") == [] and await audits(db, "rule.deactivate") == []


async def test_editing_the_category_of_created_items_counts_too(client, service, clients, db):
    firefly, _ = clients
    for _ in range(2):
        item = await created_item(service, category="Alimentación")
        response = await client.patch(f"{API}/inbox/{item.id}", headers=ADMIN, json={"category": "Transporte"})
        assert response.status_code == 200, response.text
    [rule] = await learned_rules(db)
    assert (rule.pattern, rule.category, rule.source) == ("TIENDA UNO", "Transporte", "learned")
    assert firefly.update_transaction.call_count == 2


async def test_applied_suggestions_feed_the_learning_too(client, service, clients, db, background):
    _, llm = clients
    for _ in range(2):
        await review_item(service, category=None)
    llm.complete_json.return_value = answer(category="Transporte")
    await suggest(client)
    ids = [entry["id"] for entry in (await listing(client))["items"]]
    assert (await client.post(f"{API}/inbox/ai-suggestions/apply", headers=ADMIN, json={"ids": ids})).json()["applied"] == ids
    [rule] = await learned_rules(db)
    assert (rule.pattern, rule.category, rule.source) == ("TIENDA UNO", "Transporte", "learned")
    assert (await audits(db, "rule.learn"))[0].detail["item_id"] is not None


async def test_a_failed_learning_step_never_fails_the_review(client, service, db, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(rules, "learn_from_correction", broken)
    body = await approve_with(client, await review_item(service, category=None), "Transporte")
    assert body["status"] == "created" and body["category"] == "Transporte"
