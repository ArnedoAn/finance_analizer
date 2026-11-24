"""Owner-approved AI functions: search, categorization/tagging, narrated report, anomaly explanations, budget proposals,
subscription verdicts and their switches. The LLM and Firefly are simulated (Firefly with the stateful ledger stand-in)."""
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from app.core.exceptions import FireflyError
from app.core.timezone import now_utc
from app.db.models import AiItemSuggestion, AiNarrative, AiNote, AiUsage, InboxItem
from app.db.models_alerts import Alert
from app.db.models_assistant import Audit
from app.db.models_ledger import LedgerChange
from app.db.models_rules import Merchant, Rule
from app.services import (
    ai_anomalies,
    ai_budget,
    ai_categorize,
    ai_features,
    ai_insights,
    ai_narrative,
    ai_search,
    ai_subscriptions,
    anomalies,
    budget_suggest,
)
from tests.test_devices_v4 import API, auth, enroll_key
from tests.test_ledger_v4 import FireflyLedger

ADMIN = auth("test-admin-key-01234567890123456789")
MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "20261124_12_ai_insights.py"


@pytest.fixture
def fake(clients):
    firefly, _ = clients
    firefly.accounts.return_value = [*firefly.accounts.return_value, {"id": "6", "name": "Tarjeta Nu", "type": "liabilities"}]
    firefly.categories.return_value = ["Alimentación", "Transporte", "Ocio"]
    firefly.budget_progress.return_value = []
    return FireflyLedger(firefly, firefly.accounts.return_value, budgets={"11": "Mercado"}, bills={"21": "Internet"})


@pytest.fixture
def llm(clients):
    ai_search.clear_cache()
    clients[1].complete_json = AsyncMock(return_value={"items": []})
    return clients[1]


def sent(llm, index=-1):
    """The ``content`` dict of a ``complete_json`` call."""
    return llm.complete_json.await_args_list[index].args[1]


async def add_item(db, fake, ref, *, merchant="SUPERMERCADO CENTRAL", category=None, tags=None, amount="12000.00", age_days=1,
                   kind="withdrawal", firefly=True, when=None):
    fid = fake.seed(external_id=f"fa:{ref}", tags=["auto", "sms"], category_name=category, amount=amount) if firefly else None
    item = InboxItem(
        source="sms", source_ref=ref, sender="Bancolombia", subject_or_title="", raw_text="x", status="created", amount=Decimal(amount), currency="COP",
        merchant=merchant, category=category, asset_account="Nequi", transaction_type=kind, firefly_transaction_id=fid, tags=list(tags or []),
        occurred_at=when or now_utc() - timedelta(days=age_days),
    )
    db.add(item)
    await db.commit()
    return item


async def rows(db, model=AiItemSuggestion):
    return list((await db.scalars(select(model).order_by(*sa.inspect(model).primary_key))).all())


# --- Switches ---------------------------------------------------------------------------------------------------------------


async def test_ai_features_defaults_update_and_validation(client):
    first = (await client.get(f"{API}/settings/ai-features", headers=ADMIN)).json()
    assert first == {
        "features": {"categorize": True, "narrative": True, "anomalies": True, "subscriptions": True, "tags": True, "month_close": True, "coach": True},
        "confidence_threshold": 0.85, "tags": ["viaje", "trabajo", "salud", "hogar", "mascotas", "educación", "regalos", "ocio"],
        "second_opinion": {"enabled": False, "model": "mimo-v2.6-flash", "min_amount_cop": "1000000.00", "verified_max_amount_cop": None}
    }
    response = await client.put(f"{API}/settings/ai-features", headers=ADMIN, json={"features": {"tags": False}, "confidence_threshold": 0.9, "tags": ["Viaje ", "trabajo", "viaje"]})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "features": {"categorize": True, "narrative": True, "anomalies": True, "subscriptions": True, "tags": False, "month_close": True, "coach": True},
        "confidence_threshold": 0.9, "tags": ["viaje", "trabajo"],
        "second_opinion": {"enabled": False, "model": "mimo-v2.6-flash", "min_amount_cop": "1000000.00", "verified_max_amount_cop": None}
    }
    assert (await client.get(f"{API}/settings/ai-features", headers=ADMIN)).json() == response.json()
    partial = (await client.put(f"{API}/settings/ai-features", headers=ADMIN, json={"features": {"narrative": False}})).json()
    assert partial["features"]["narrative"] is False and partial["features"]["tags"] is False and partial["confidence_threshold"] == 0.9
    for bad in ({"confidence_threshold": 0.2}, {"confidence_threshold": 1.5}, {"features": {"unknown": True}}, {"extra": 1}):
        assert (await client.put(f"{API}/settings/ai-features", headers=ADMIN, json=bad)).status_code == 422
    empty = await client.put(f"{API}/settings/ai-features", headers=ADMIN, json={"tags": []})
    assert empty.status_code == 422 and empty.json()["code"] == "invalid_tags"
    assert (await client.put(f"{API}/settings/ai-features", headers=ADMIN, json={"tags": ["x" * 41]})).json()["code"] == "invalid_tags"


async def test_ai_features_scopes_and_audit(client, db):
    reader = auth((await enroll_key(client, ["read"], "reader"))["key"])
    assert (await client.get(f"{API}/settings/ai-features", headers=reader)).status_code == 200
    assert (await client.put(f"{API}/settings/ai-features", headers=reader, json={})).status_code == 403
    await client.put(f"{API}/settings/ai-features", headers=ADMIN, json={"features": {"anomalies": False}})
    entry = await db.scalar(select(Audit).where(Audit.action == "settings.ai_features"))
    assert entry is not None and entry.detail["features"]["anomalies"] is False


# --- Natural-language search ------------------------------------------------------------------------------------------------


async def test_search_parse_validates_against_real_vocabulary_and_caches(client, db, llm, fake):
    db.add(Merchant(name="RAPPI", count=5))
    await add_item(db, fake, "t1", tags=["vacaciones"], firefly=False)
    await db.commit()
    llm.complete_json.return_value = {
        "filters": {
            "from": "2026-10-31", "to": "2026-10-01", "account": "nequi", "category": "Alimentación", "tag": "vacaciones", "merchant": "rappi",
            "kind": "withdrawal", "min_amount": 50000, "max_amount": "20000", "text": None,
        },
        "explanation": "Gastos de Rappi en octubre.",
    }
    response = await client.post(f"{API}/search/parse", headers=ADMIN, json={"q": "Gastos en Rappi del mes pasado para Maria Perez más de 50 mil"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "filters": {
            "from": "2026-10-01", "to": "2026-10-31", "account": "Nequi", "category": "Alimentación", "tag": "vacaciones", "merchant": "RAPPI",
            "kind": "withdrawal", "min_amount": "20000.00", "max_amount": "50000.00", "text": None,
        },
        "explanation": "Gastos de Rappi en octubre.",
    }
    content = sent(llm)
    assert "Maria Perez" not in json.dumps(content, ensure_ascii=False)
    assert content["CONTEXT"]["categories"] == ["Alimentación", "Transporte", "Ocio"] and "RAPPI" in content["CONTEXT"]["merchants"]
    assert "vacaciones" in content["CONTEXT"]["tags"] and "viaje" in content["CONTEXT"]["tags"] and content["CONTEXT"]["accounts"] == ["Nequi"]
    usage = await db.scalar(select(AiUsage).where(AiUsage.kind == "ai_search"))
    assert (usage.calls, usage.auto_calls) == (1, 0)

    again = await client.post(f"{API}/search/parse", headers=ADMIN, json={"q": "  GASTOS en rappi del MES pasado para maria perez más de 50 MIL "})
    assert again.json() == response.json() and llm.complete_json.await_count == 1


async def test_search_parse_discards_unknown_values_and_explains_deterministically(client, llm, fake):
    llm.complete_json.return_value = {"filters": {
        "account": "Cuenta Fantasma", "category": "Viajes", "tag": "nada", "merchant": "TIENDA INVENTADA", "kind": "gasto",
        "from": "ayer", "to": "2026-13-45", "min_amount": "mucho", "max_amount": -5, "text": None,
    }, "explanation": "Busco gastos de Fantasma en la categoría Viajes."}
    body = (await client.post(f"{API}/search/parse", headers=ADMIN, json={"q": "cosas raras"})).json()
    assert body["filters"] == {
        "from": None, "to": None, "account": None, "category": None, "tag": None, "merchant": None, "kind": None,
        "min_amount": None, "max_amount": None, "text": "TIENDA INVENTADA",
    }
    assert body["explanation"] == "Busco movimientos que mencionan «TIENDA INVENTADA»."


async def test_search_parse_errors_and_scope(client, llm):
    ingest = auth((await enroll_key(client, ["ingest"], "phone"))["key"])
    reader = auth((await enroll_key(client, ["read"], "reader"))["key"])
    assert (await client.post(f"{API}/search/parse", headers=ingest, json={"q": "x"})).status_code == 403
    assert (await client.post(f"{API}/search/parse", headers=ADMIN, json={"q": ""})).status_code == 422
    assert (await client.post(f"{API}/search/parse", headers=ADMIN, json={"q": "x" * 301})).status_code == 422
    llm.complete_json.return_value = {"filters": "nope"}
    bad = await client.post(f"{API}/search/parse", headers=reader, json={"q": "gastos"})
    assert bad.status_code == 502 and bad.json()["code"] == "llm_unavailable"
    llm.complete_json.side_effect = RuntimeError("LLM chain exhausted")
    assert (await client.post(f"{API}/search/parse", headers=reader, json={"q": "otra"})).json()["code"] == "llm_unavailable"


def test_search_cache_key_and_ttl():
    ai_search.clear_cache()
    assert ai_search.normalize_question("  Gastos EN Mercado ") == ai_search.normalize_question("gastos en  mercado")
    assert ai_search.CACHE_SECONDS == 24 * 3600


# --- Categorize and tag -----------------------------------------------------------------------------------------------------


async def test_confident_proposal_is_applied_audited_undoable_and_never_proposed_again(client, db, llm, fake, settings, clients):
    await add_item(db, fake, "h1", category="Alimentación", tags=["hogar"], firefly=False)
    await add_item(db, fake, "h2", category="Alimentación", firefly=False)
    target = await add_item(db, fake, "t1", merchant="SUPERMERCADO CENTRAL")
    llm.complete_json.return_value = {"items": [{
        "id": target.id, "category": "alimentación", "category_confidence": 0.95, "tags": ["hogar", "inventada"], "tags_confidence": 0.9, "reason": "Compra de mercado",
    }]}
    counts = await ai_categorize.run_batch(db, clients[0], llm, settings)
    assert counts == {"proposed": 1, "auto_applied": 1, "pending": 0, "skipped": None}

    content = sent(llm)
    (evidence,) = content["UNTRUSTED_DATA"]
    assert evidence["merchant"] == "SUPERMERCADO CENTRAL" and evidence["need"] == ["category", "tags"]
    assert evidence["history"]["categories"] == [{"name": "Alimentación", "count": 2}] and evidence["history"]["tags"] == [{"name": "hogar", "count": 1}]
    assert content["CONTEXT"]["categories"] == ["Alimentación", "Transporte", "Ocio"] and "hogar" in content["CONTEXT"]["tags"]

    (row,) = await rows(db)
    assert (row.status, row.category, row.tags, row.applied_fields) == ("auto_applied", "Alimentación", ["hogar"], ["category", "tags"])
    assert row.group_id
    [journal] = fake.journals(target.firefly_transaction_id)
    assert journal["category_name"] == "Alimentación" and journal["tags"] == ["auto", "sms", "hogar"]
    await db.refresh(target)
    assert target.category == "Alimentación" and target.tags == ["hogar"]
    change = await db.scalar(select(LedgerChange).where(LedgerChange.group_id == row.group_id))
    assert change.actor == "system" and change.action == "update"
    entry = await db.scalar(select(Audit).where(Audit.action == "ai_categorize.auto_apply"))
    assert entry.detail["fields"] == ["category", "tags"] and entry.detail["group_id"] == row.group_id
    usage = await db.scalar(select(AiUsage).where(AiUsage.kind == "ai_categorize"))
    assert (usage.calls, usage.auto_calls) == (1, 1)

    listing = (await client.get(f"{API}/ai/category-suggestions?status=auto_applied", headers=ADMIN)).json()
    assert listing["total"] == 1 and listing["items"][0]["group_id"] == row.group_id and listing["items"][0]["undone"] is False

    undo = await client.post(f"{API}/ledger/groups/{row.group_id}/undo", headers=ADMIN)
    assert undo.status_code == 200 and len(undo.json()["undone"]) == 1
    assert fake.journals(target.firefly_transaction_id)[0]["category_name"] is None
    assert (await client.get(f"{API}/ai/category-suggestions?status=auto_applied", headers=ADMIN)).json()["items"][0]["undone"] is True

    assert (await ai_categorize.run_batch(db, clients[0], llm, settings)) == {"skipped": "nothing_to_do"}
    assert llm.complete_json.await_count == 1


async def test_below_threshold_stays_a_suggestion_until_the_owner_applies_it(client, db, llm, fake, settings, clients):
    target = await add_item(db, fake, "t1")
    llm.complete_json.return_value = {"items": [{"id": target.id, "category": "Transporte", "category_confidence": 0.6, "tags": ["viaje"], "tags_confidence": 0.95}]}
    counts = await ai_categorize.run_batch(db, clients[0], llm, settings)
    assert counts["pending"] == 1 and counts["auto_applied"] == 0
    (row,) = await rows(db)
    assert row.status == "pending" and row.applied_fields == ["tags"] and ai_categorize.pending_fields(row) == ["category"]
    assert fake.journals(target.firefly_transaction_id)[0]["category_name"] is None
    assert fake.journals(target.firefly_transaction_id)[0]["tags"] == ["auto", "sms", "viaje"]

    listing = (await client.get(f"{API}/ai/category-suggestions", headers=ADMIN)).json()
    (item,) = listing["items"]
    assert item["pending_fields"] == ["category"] and item["category"] == "Transporte" and item["category_confidence"] == 0.6 and item["merchant"] == "SUPERMERCADO CENTRAL"
    assert (await client.post(f"{API}/ai/category-suggestions/apply", headers=ADMIN, json={})).status_code == 422
    assert (await client.post(f"{API}/ai/category-suggestions/apply", headers=ADMIN, json={"all_above": 0.9})).json() == {"applied": [], "failed": []}
    applied = await client.post(f"{API}/ai/category-suggestions/apply", headers=ADMIN, json={"ids": [row.id]})
    assert applied.json() == {"applied": [row.id], "failed": []}
    assert fake.journals(target.firefly_transaction_id)[0]["category_name"] == "Transporte"
    await db.refresh(row)
    assert row.status == "applied" and row.applied_fields == ["category", "tags"]
    assert (await client.post(f"{API}/ai/category-suggestions/apply", headers=ADMIN, json={"ids": [row.id]})).json()["failed"][0]["id"] == row.id


async def test_lower_threshold_applies_more_and_all_above_selects_by_confidence(client, db, llm, fake, settings, clients):
    first, second = await add_item(db, fake, "a"), await add_item(db, fake, "b", merchant="TIENDA B")
    llm.complete_json.return_value = {"items": [
        {"id": first.id, "category": "Transporte", "category_confidence": 0.7, "tags": [], "tags_confidence": 0},
        {"id": second.id, "category": "Ocio", "category_confidence": 0.5, "tags": [], "tags_confidence": 0},
    ]}
    assert (await client.put(f"{API}/settings/ai-features", headers=ADMIN, json={"confidence_threshold": 0.7})).status_code == 200
    counts = await ai_categorize.run_batch(db, clients[0], llm, settings)
    assert (counts["auto_applied"], counts["pending"]) == (1, 1)
    assert fake.journals(first.firefly_transaction_id)[0]["category_name"] == "Transporte"
    pending = (await client.get(f"{API}/ai/category-suggestions", headers=ADMIN)).json()["items"]
    assert [item["item_id"] for item in pending] == [second.id]
    assert (await client.post(f"{API}/ai/category-suggestions/apply", headers=ADMIN, json={"all_above": 0.6})).json()["applied"] == []
    assert (await client.post(f"{API}/ai/category-suggestions/apply", headers=ADMIN, json={"all_above": 0.5})).json()["applied"] == [pending[0]["id"]]


async def test_manual_rule_for_the_merchant_blocks_automatic_category(db, llm, fake, settings, clients):
    db.add(Rule(match="merchant_exact", pattern="SUPERMERCADO CENTRAL", category="Transporte", source="manual", hits=0))
    target = await add_item(db, fake, "t1")
    llm.complete_json.return_value = {"items": [{"id": target.id, "category": "Alimentación", "category_confidence": 0.99, "tags": [], "tags_confidence": 0}]}
    assert (await ai_categorize.run_batch(db, clients[0], llm, settings))["pending"] == 1
    (row,) = await rows(db)
    assert (row.status, row.note, row.applied_fields) == ("pending", "contradice_regla", [])
    assert fake.journals(target.firefly_transaction_id)[0]["category_name"] is None and not fake.writes("update")


async def test_switches_budget_and_failures_stop_or_retry_the_batch(client, db, llm, fake, settings, clients):
    target = await add_item(db, fake, "t1")
    await client.put(f"{API}/settings/ai-features", headers=ADMIN, json={"features": {"categorize": False, "tags": False}})
    assert await ai_categorize.run_batch(db, clients[0], llm, settings) == {"skipped": "disabled"}
    await client.put(f"{API}/settings/ai-features", headers=ADMIN, json={"features": {"categorize": False, "tags": True}})
    llm.complete_json.return_value = {"items": []}
    await ai_categorize.run_batch(db, clients[0], llm, settings)
    assert sent(llm)["UNTRUSTED_DATA"][0]["need"] == ["tags"]
    assert [row.status for row in await rows(db)] == ["none"]

    other = await add_item(db, fake, "t2")
    await client.put(f"{API}/settings/ai-features", headers=ADMIN, json={"features": {"categorize": True, "tags": True}})
    await ai_budget.set_policy(db, 0)
    await db.commit()
    assert await ai_categorize.run_batch(db, clients[0], llm, settings) == {"skipped": "budget"}
    assert llm.complete_json.await_count == 1
    await ai_budget.set_policy(db, 400)
    await db.commit()

    llm.complete_json.side_effect = RuntimeError("down")
    assert await ai_categorize.run_batch(db, clients[0], llm, settings) == {"skipped": "llm"}
    failed = [row for row in await rows(db) if row.item_id == other.id]
    assert failed[0].status == "error"
    assert await ai_categorize.run_batch(db, clients[0], llm, settings) == {"skipped": "nothing_to_do"}
    failed[0].updated_at = now_utc() - timedelta(hours=7)
    await db.commit()
    llm.complete_json.side_effect = None
    llm.complete_json.return_value = {"items": [{"id": other.id, "category": "Ocio", "category_confidence": 0.9, "tags": [], "tags_confidence": 0}]}
    assert (await ai_categorize.run_batch(db, clients[0], llm, settings))["auto_applied"] == 1
    assert target.id != other.id


async def test_eligibility_window_batch_size_and_already_classified(db, llm, fake, settings, clients):
    await add_item(db, fake, "old", age_days=45)
    await add_item(db, fake, "done", category="Ocio", tags=["ocio"])
    await add_item(db, fake, "generic", category="Otros", tags=["x"])
    await add_item(db, fake, "transfer", kind="transfer")
    for number in range(30):
        await add_item(db, fake, f"n{number}", merchant=f"TIENDA {number}")
    await ai_categorize.run_batch(db, clients[0], llm, settings)
    assert len(sent(llm)["UNTRUSTED_DATA"]) == 25
    await ai_categorize.run_batch(db, clients[0], llm, settings)
    assert len(sent(llm)["UNTRUSTED_DATA"]) == 6  # n24..n29
    assert await ai_categorize.run_batch(db, clients[0], llm, settings) == {"skipped": "nothing_to_do"}


async def test_discard_and_stale(client, db, llm, fake, settings, clients):
    target = await add_item(db, fake, "t1")
    llm.complete_json.return_value = {"items": [{"id": target.id, "category": "Ocio", "category_confidence": 0.5, "tags": [], "tags_confidence": 0}]}
    await ai_categorize.run_batch(db, clients[0], llm, settings)
    (row,) = await rows(db)
    assert (await client.post(f"{API}/ai/category-suggestions/{row.id}/discard", headers=ADMIN)).json() == {"id": row.id, "status": "discarded"}
    assert (await client.post(f"{API}/ai/category-suggestions/{row.id}/discard", headers=ADMIN)).status_code == 409
    assert (await client.post(f"{API}/ai/category-suggestions/999/discard", headers=ADMIN)).json()["code"] == "suggestion_not_found"
    other = await add_item(db, fake, "t2")
    llm.complete_json.return_value = {"items": [{"id": other.id, "category": "Ocio", "category_confidence": 0.5, "tags": [], "tags_confidence": 0}]}
    await ai_categorize.run_batch(db, clients[0], llm, settings)
    other.category = "Transporte"
    other.tags = ["ocio"]
    await db.commit()
    assert (await client.get(f"{API}/ai/category-suggestions", headers=ADMIN)).json()["total"] == 0
    assert (await rows(db))[-1].status == "stale"
    assert (await client.get(f"{API}/ai/category-suggestions?status=bogus", headers=ADMIN)).status_code == 422


# --- Anomaly explanations ---------------------------------------------------------------------------------------------------

ENTRY = {
    "key": "unusual_amount:5", "kind": "unusual_amount", "item_id": 5, "related_item_ids": [], "merchant": "RAPPI", "category": "Alimentación",
    "amount": "150000.00", "currency": "COP", "occurred_at": "2026-11-23T10:00:00+00:00", "score": 4.2,
    "detail": "150.000,00 COP supera el gasto habitual en Alimentación (promedio 30.000,00 COP, 4.2 desviaciones)",
}


async def test_new_anomalies_get_one_sentence_attached_to_the_output(client, db, llm, settings, monkeypatch):
    monkeypatch.setattr(anomalies, "detect", AsyncMock(side_effect=lambda *a, **k: [dict(ENTRY)]))
    llm.complete_json.return_value = {"items": [{"key": ENTRY["key"], "explanation": "Te lo marco porque RAPPI cobró 150.000,00 COP y tu promedio en Alimentación es 30.000,00 COP."}]}
    assert await ai_anomalies.explain_new(db, llm, settings) == {"explained": 1, "failed": 0}
    content = sent(llm)
    assert content["UNTRUSTED_DATA"][0]["key"] == ENTRY["key"] and "promedio 30.000,00 COP" in content["UNTRUSTED_DATA"][0]["facts"]
    body = (await client.get(f"{API}/anomalies", headers=ADMIN)).json()
    assert body["items"][0]["explanation"] == "Te lo marco porque RAPPI cobró 150.000,00 COP y tu promedio en Alimentación es 30.000,00 COP."
    assert await ai_anomalies.explain_new(db, llm, settings) == {"skipped": "nothing_to_do"} and llm.complete_json.await_count == 1
    await client.put(f"{API}/settings/ai-features", headers=ADMIN, json={"features": {"anomalies": False}})
    assert (await client.get(f"{API}/anomalies", headers=ADMIN)).json()["items"][0]["explanation"] is None
    assert await ai_anomalies.explain_new(db, llm, settings) == {"skipped": "disabled"}


async def test_explanation_with_invented_figures_is_rejected_and_retried_later(db, llm, settings, monkeypatch):
    monkeypatch.setattr(anomalies, "detect", AsyncMock(side_effect=lambda *a, **k: [dict(ENTRY)]))
    llm.complete_json.return_value = {"items": [{"key": ENTRY["key"], "explanation": "Te lo marco porque son $999.999 de más."}, {"key": "otra", "explanation": "x"}]}
    assert await ai_anomalies.explain_new(db, llm, settings) == {"explained": 0, "failed": 1}
    note = await db.scalar(select(AiNote).where(AiNote.kind == "anomaly"))
    assert note.text == "" and note.data == {"error": True}
    assert (await ai_anomalies.attach(db, [dict(ENTRY)]))[0]["explanation"] is None
    assert await ai_anomalies.explain_new(db, llm, settings) == {"skipped": "nothing_to_do"}
    note.updated_at = now_utc() - timedelta(hours=7)
    await db.commit()
    llm.complete_json.return_value = {"items": [{"key": ENTRY["key"], "explanation": "Te lo marco porque supera tu promedio de 30.000,00 COP."}]}
    assert (await ai_anomalies.explain_new(db, llm, settings))["explained"] == 1
    await db.refresh(note)
    assert note.text.startswith("Te lo marco") and note.data == {}


async def test_anomaly_explanations_respect_the_budget_guard(db, llm, settings, monkeypatch):
    monkeypatch.setattr(anomalies, "detect", AsyncMock(side_effect=lambda *a, **k: [dict(ENTRY)]))
    await ai_budget.set_policy(db, 0)
    await db.commit()
    assert await ai_anomalies.explain_new(db, llm, settings) == {"skipped": "budget"}
    llm.complete_json.assert_not_awaited()
    assert await ai_anomalies.explain_new(db, llm, settings, auto=False) == {"explained": 0, "failed": 1}


# --- Narrated monthly report ------------------------------------------------------------------------------------------------


def utc(year, month, day):
    return datetime(year, month, day, 17, 0, tzinfo=UTC)


async def seed_months(db, fake):
    data = [
        (utc(2026, 7, 15), "SUPERMERCADO CENTRAL", "Alimentación", "280000.00"),
        (utc(2026, 8, 15), "SUPERMERCADO CENTRAL", "Alimentación", "300000.00"),
        (utc(2026, 9, 15), "SUPERMERCADO CENTRAL", "Alimentación", "300000.00"),
        (utc(2026, 9, 16), "UBER", "Transporte", "120000.00"),
        (utc(2026, 10, 15), "SUPERMERCADO CENTRAL", "Alimentación", "300000.00"),
        (utc(2026, 10, 16), "EXITO", "Alimentación", "100000.00"),
        (utc(2026, 10, 17), "UBER", "Transporte", "50000.00"),
        *[(utc(2026, 10, 20 + n), "TINTO", "Café", "5000.00") for n in range(3)],
    ]
    for number, (when, merchant, category, amount) in enumerate(data):
        await add_item(db, fake, f"m{number}", merchant=merchant, category=category, amount=amount, when=when, firefly=False)
    await add_item(db, fake, "pay", merchant="EMPRESA", amount="1000000.00", kind="deposit", when=utc(2026, 10, 28), firefly=False)


def narrator(*, invent=False):
    def answer(prompt, content, cycle_id, usage=None):
        facts = content["UNTRUSTED_DATA"]["facts"]
        up, down = facts["categorías_que_subieron"][0], facts["categorías_que_bajaron"][0]
        sentences = [
            f"En {facts['mes']} gastaste {facts['gasto_total']} y recibiste {facts['ingresos']}.",
            f"El mes anterior habías gastado {facts['mes_anterior']['gasto_total']}.",
            f"Frente a ese mes la diferencia fue de {facts['frente_al_mes_anterior']['diferencia']}.",
            f"Frente al promedio de tres meses ({facts['promedio_3_meses']['gasto_total']}) la diferencia fue de {facts['frente_al_promedio']['diferencia']}.",
            f"{up['categoría']} subió {up['diferencia']}, sobre todo por {up['comercio_que_más_explica']['nombre']}.",
            f"{down['categoría']} bajó {down['diferencia']} y el gasto hormiga sumó {facts['gasto_hormiga']['total']}.",
            f"El presupuesto {facts['presupuestos'][0]['nombre']} quedó {facts['presupuestos'][0]['estado']}.",
        ]
        if invent:
            sentences.append("Además ahorraste $987.654 COP sin que lo supieras.")
        if usage is not None:
            usage.update({"model": "test-model", "calls": 1})
        return {"text": " ".join(sentences)}
    return answer


@pytest.fixture
def november(monkeypatch):
    monkeypatch.setattr(ai_narrative, "now_utc", lambda: datetime(2026, 11, 24, 15, 0, tzinfo=UTC))


async def test_narrative_figures_are_exact_and_the_text_uses_only_them(client, db, llm, fake, clients, november):
    await seed_months(db, fake)
    clients[0].budget_progress.return_value = [{"name": "Mercado", "spent": Decimal("150000.00"), "limit": Decimal("100000.00")}, {"name": "Sin tope", "spent": Decimal(5), "limit": Decimal(0)}]
    llm.complete_json.side_effect = narrator()
    response = await client.post(f"{API}/reports/narrative?month=2026-10", headers=ADMIN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"month", "text", "figures", "source", "model", "generated_at"}
    assert body["month"] == "2026-10" and body["source"] == "owner" and body["model"] == "test-model"
    figures = body["figures"]
    assert (figures["spent"], figures["income"], figures["balance"]) == ("465000.00", "1000000.00", "535000.00")
    assert figures["previous"] == {"month": "2026-09", "spent": "420000.00"}
    assert figures["average3"] == {"months": ["2026-07", "2026-08", "2026-09"], "spent": "333333.33"}
    assert figures["vs_previous"] == {"diff": "45000.00", "pct": "10.71"}
    assert figures["vs_average3"] == {"diff": "131666.67", "pct": "39.50"}
    (up,), (down,) = figures["categories_up"], figures["categories_down"]
    assert up["category"] == "Alimentación" and up["diff"] == "100000.00" and up["top_merchant"] == {"name": "EXITO", "amount": "100000.00", "previous": "0.00"}
    assert down["category"] == "Transporte" and down["diff"] == "-70000.00" and down["top_merchant"] == {"name": "UBER", "amount": "50000.00", "previous": "120000.00"}
    assert figures["small_spending"] == {
        "limit": "20000.00", "count": 3, "total": "15000.00", "share_pct": "3.23", "previous_total": "0.00",
        "top_merchants": [{"merchant": "TINTO", "count": 3, "total": "15000.00"}],
    }
    assert figures["budgets"] == [{"name": "Mercado", "spent": "150000.00", "limit": "100000.00", "over": True, "diff": "50000.00"}]
    assert "$465.000 COP" in body["text"] and "EXITO" in body["text"] and "excedido" in body["text"]
    content = sent(llm)
    assert "facts" in content["UNTRUSTED_DATA"] and content["UNTRUSTED_DATA"]["facts"]["mes"] == "octubre de 2026"

    assert (await client.get(f"{API}/reports/narrative?month=2026-10", headers=ADMIN)).json() == json.loads(json.dumps(body))
    assert (await client.get(f"{API}/reports/narrative", headers=ADMIN)).json()["month"] == "2026-10"
    alerts = (await db.scalars(select(Alert).where(Alert.type == "monthly_report"))).all()
    assert len(alerts) == 1 and alerts[0].title == "Tu informe de octubre de 2026"
    stored = await db.get(AiNarrative, "2026-10")
    raw = (await db.execute(sa.text("SELECT text, figures_json FROM ai_narratives"))).one()
    assert stored.text == body["text"] and raw[0].startswith("enc:v1:") and raw[1].startswith("enc:v1:")
    usage = await db.scalar(select(AiUsage).where(AiUsage.kind == "ai_narrative"))
    assert (usage.calls, usage.auto_calls) == (1, 0)

    await client.post(f"{API}/reports/narrative?month=2026-10", headers=ADMIN)
    assert len((await db.scalars(select(Alert).where(Alert.type == "monthly_report"))).all()) == 1
    assert len(await rows(db, AiNarrative)) == 1


async def test_narrative_rejects_invented_figures_and_bad_requests(client, db, llm, fake, clients, november):
    await seed_months(db, fake)
    clients[0].budget_progress.return_value = [{"name": "Mercado", "spent": Decimal("150000.00"), "limit": Decimal("100000.00")}]
    llm.complete_json.side_effect = narrator(invent=True)
    invented = await client.post(f"{API}/reports/narrative?month=2026-10", headers=ADMIN)
    assert invented.status_code == 502 and invented.json()["code"] == "llm_invalid"
    assert await rows(db, AiNarrative) == []
    llm.complete_json.side_effect = RuntimeError("down")
    assert (await client.post(f"{API}/reports/narrative?month=2026-10", headers=ADMIN)).json()["code"] == "llm_unavailable"
    assert (await client.post(f"{API}/reports/narrative?month=2026-11", headers=ADMIN)).json()["code"] == "month_not_closed"
    assert (await client.post(f"{API}/reports/narrative?month=2026-05", headers=ADMIN)).json()["code"] == "no_data"
    assert (await client.post(f"{API}/reports/narrative?month=2026-13", headers=ADMIN)).json()["code"] == "invalid_month"
    assert (await client.get(f"{API}/reports/narrative?month=2026-13", headers=ADMIN)).json()["code"] == "invalid_month"
    assert (await client.get(f"{API}/reports/narrative?month=2026-09", headers=ADMIN)).json()["code"] == "narrative_not_found"
    reader = auth((await enroll_key(client, ["read"], "reader"))["key"])
    assert (await client.post(f"{API}/reports/narrative?month=2026-10", headers=reader)).status_code == 403
    assert (await client.get(f"{API}/reports/narrative", headers=reader)).status_code == 404


async def test_previous_month_job_generates_once_respects_switch_and_budget(db, llm, fake, clients, settings, november):
    await seed_months(db, fake)
    clients[0].budget_progress.return_value = [{"name": "Mercado", "spent": Decimal("50.00"), "limit": Decimal("100.00")}]
    llm.complete_json.side_effect = narrator()
    await ai_features.update(db, features={"narrative": False})
    await db.commit()
    assert await ai_insights.run_monthly_narrative(db, clients[0], llm, settings) == {"skipped": "disabled"}
    await ai_features.update(db, features={"narrative": True})
    await ai_budget.set_policy(db, 0)
    await db.commit()
    assert await ai_insights.run_monthly_narrative(db, clients[0], llm, settings) == {"skipped": "budget"}
    await ai_budget.set_policy(db, 400)
    await db.commit()
    note = await db.scalar(select(AiNote).where(AiNote.kind == "narrative_try"))
    note.updated_at = ai_narrative.now_utc() - timedelta(hours=7)  # the job's clock (the `november` fixture), not the wall clock
    await db.commit()
    assert await ai_insights.run_monthly_narrative(db, clients[0], llm, settings) == {"generated": "2026-10"}
    assert (await db.get(AiNarrative, "2026-10")).source == "auto"
    assert await ai_insights.run_monthly_narrative(db, clients[0], llm, settings) == {"skipped": "exists", "month": "2026-10"}
    usage = await db.scalar(select(AiUsage).where(AiUsage.kind == "ai_narrative"))
    assert (usage.calls, usage.auto_calls) == (1, 1)
    assert len((await db.scalars(select(Alert).where(Alert.type == "monthly_report"))).all()) == 1


# --- Budget proposals -------------------------------------------------------------------------------------------------------


async def test_budget_proposals_use_median_trend_and_exclude_one_offs(client, db, fake, clients, settings):
    for month, amount in ((8, "300000.00"), (9, "320000.00"), (10, "400000.00")):
        await add_item(db, fake, f"a{month}", merchant="SUPERMERCADO CENTRAL", category="Alimentación", amount=amount, when=utc(2026, month, 10), firefly=False)
    for month in (8, 9, 10):
        for n in range(2):
            await add_item(db, fake, f"h{month}{n}", merchant="FERRETERIA", category="Hogar", amount="50000.00", when=utc(2026, month, 5 + n), firefly=False)
    await add_item(db, fake, "mueble", merchant="MUEBLES UNICO", category="Hogar", amount="900000.00", when=utc(2026, 9, 20), firefly=False)
    await add_item(db, fake, "viaje", merchant="AEROLINEA", category="Viajes", amount="700000.00", when=utc(2026, 10, 3), firefly=False)
    await add_item(db, fake, "nuevo", merchant="X", category=None, amount="10000.00", when=utc(2026, 10, 3), firefly=False)
    clients[0].budget_progress.return_value = [{"name": "alimentación", "spent": Decimal(1), "limit": Decimal("350000.00")}]
    result = await budget_suggest.suggest(db, clients[0], settings, now=datetime(2026, 11, 5, 12, tzinfo=UTC))
    assert result["months"] == ["2026-08", "2026-09", "2026-10"] and result["currency"] == "COP"
    assert [item["category"] for item in result["items"]] == ["Alimentación", "Hogar"]
    food, home = result["items"]
    assert (food["suggested_limit"], food["median"], food["last_month"], food["trend"], food["trend_pct"]) == ("360000.00", "320000.00", "400000.00", "up", "33.33")
    assert food["current_limit"] == "350000.00" and food["excluded"] == {"count": 0, "total": "0.00"}
    assert food["monthly"] == [{"month": "2026-08", "total": "300000.00"}, {"month": "2026-09", "total": "320000.00"}, {"month": "2026-10", "total": "400000.00"}]
    assert (home["suggested_limit"], home["trend"], home["excluded"], home["current_limit"]) == ("100000.00", "flat", {"count": 1, "total": "900000.00"}, None)
    assert "única" in home["reason"] or "único" in home["reason"]
    assert result["total_suggested"] == "460000.00"
    assert all(isinstance(item["reason"], str) and item["reason"] for item in result["items"])
    clients[0].create_transaction.assert_not_awaited()


async def test_budget_suggest_route_needs_review_and_creates_nothing(client, clients, fake):
    reader = auth((await enroll_key(client, ["read"], "reader"))["key"])
    assert (await client.post(f"{API}/budgets/suggest", headers=reader)).status_code == 403
    response = await client.post(f"{API}/budgets/suggest", headers=ADMIN)
    assert response.status_code == 200 and response.json()["items"] == [] and response.json()["total_suggested"] == "0.00"


# --- Subscriptions ----------------------------------------------------------------------------------------------------------


async def seed_subscriptions(db, fake):
    for number, (days, amount) in enumerate(((94, "30000.00"), (63, "30000.00"), (32, "30000.00"), (2, "45000.00"))):
        await add_item(db, fake, f"nf{number}", merchant="NETFLIX", category="Ocio", amount=amount, age_days=days, firefly=False)
    for number, days in enumerate((94, 63, 32, 2)):
        await add_item(db, fake, f"co{number}", merchant="COOPERATIVA", category="Deudas", amount="100000.00", age_days=days, firefly=False)


def judge(verdicts):
    def answer(prompt, content, cycle_id, usage=None):
        return {"items": [
            {"id": entry["id"], "verdict": next((v for name, v in verdicts.items() if name in entry["merchant"]), "other"), "reason": f"Es {entry['merchant']}"}
            for entry in content["UNTRUSTED_DATA"]
        ]}
    return answer


async def test_ai_confirms_subscriptions_and_alerts_the_price_increase_once(client, db, llm, fake, settings):
    await seed_subscriptions(db, fake)
    llm.complete_json.side_effect = judge({"NETFLIX": "subscription", "COOPERATIVA": "installment"})
    result = await ai_subscriptions.run(db, llm, settings)
    assert result == {"candidates": 2, "judged": 2, "budget_stopped": False, "price_alerts": 1}
    evidence = {entry["merchant"]: entry for entry in sent(llm)["UNTRUSTED_DATA"]}
    assert evidence["NETFLIX"]["previous_amount"] == "30000.00" and evidence["NETFLIX"]["amount"] == "45000.00" and len(evidence["NETFLIX"]["charges"]) == 4
    alert = await db.scalar(select(Alert).where(Alert.type == "price_increase"))
    assert alert.title == "Subió el precio de NETFLIX" and "$30.000 COP" in alert.body and "$45.000 COP" in alert.body and alert.dedupe_key == "price_increase:netflix:30000.00:45000.00"
    assert await ai_subscriptions.rejected_names(db) == {"cooperativa"} and await ai_subscriptions.owns_price_alerts(db)

    again = await ai_subscriptions.run(db, llm, settings)
    assert again["judged"] == 0 and again["price_alerts"] == 0 and llm.complete_json.await_count == 1
    listing = (await client.get(f"{API}/ai/subscriptions", headers=ADMIN)).json()["items"]
    by_name = {item["merchant"]: item for item in listing}
    assert by_name["NETFLIX"]["is_subscription"] is True and by_name["NETFLIX"]["verdict"] == "subscription" and by_name["NETFLIX"]["status"] == "price_increase"
    assert by_name["COOPERATIVA"]["is_subscription"] is False and by_name["COOPERATIVA"]["reason"] == "Es COOPERATIVA"

    weekly = await ai_insights.run_weekly_subscriptions(db, llm, settings)
    assert weekly["judged"] == 0
    for note in await rows(db, AiNote):
        note.updated_at = now_utc() - timedelta(days=31)
    await db.commit()
    llm.complete_json.side_effect = judge({"NETFLIX": "subscription", "COOPERATIVA": "subscription"})
    refreshed = await ai_insights.run_weekly_subscriptions(db, llm, settings)
    assert refreshed["judged"] == 2 and await ai_subscriptions.rejected_names(db) == set()
    usage = await db.scalar(select(AiUsage).where(AiUsage.kind == "ai_subscriptions"))
    assert (usage.calls, usage.auto_calls) == (2, 2)


async def test_a_rejected_price_increase_is_not_alerted_and_an_unjudged_one_still_is(db, llm, fake, settings):
    await seed_subscriptions(db, fake)
    llm.complete_json.side_effect = judge({"NETFLIX": "person_transfer"})
    assert (await ai_subscriptions.run(db, llm, settings))["price_alerts"] == 0
    assert await db.scalar(select(Alert).where(Alert.type == "price_increase")) is None

    for note in await rows(db, AiNote):
        await db.delete(note)
    await db.commit()
    llm.complete_json.side_effect = RuntimeError("down")
    result = await ai_subscriptions.run(db, llm, settings)
    assert result["price_alerts"] == 1
    assert {note.data.get("error") for note in await rows(db, AiNote)} == {True}
    assert await ai_subscriptions.rejected_names(db) == set()


async def test_subscription_ai_switch_and_budget(db, llm, fake, settings):
    await seed_subscriptions(db, fake)
    await ai_features.update(db, features={"subscriptions": False})
    await db.commit()
    assert await ai_subscriptions.run(db, llm, settings) == {"skipped": "disabled"}
    assert not await ai_subscriptions.owns_price_alerts(db) and await ai_subscriptions.rejected_names(db) == set()
    await ai_features.update(db, features={"subscriptions": True})
    await ai_budget.set_policy(db, 0)
    await db.commit()
    result = await ai_subscriptions.run(db, llm, settings)
    assert result["budget_stopped"] is True and result["price_alerts"] == 1
    llm.complete_json.assert_not_awaited()


async def test_reports_alert_check_leaves_price_increases_to_the_ai_and_skips_rejected_merchants(db, settings, monkeypatch):
    from app.services import reports

    monkeypatch.setattr(reports, "now_utc", lambda: datetime(2026, 11, 20, 17, 0, tzinfo=UTC))
    for number, month in enumerate((8, 9, 10)):
        await add_item(db, None, f"sp{number}", merchant="Spotify", amount="10000.00", when=utc(2026, month, 15), firefly=False)
    await add_item(db, None, "sp3", merchant="Spotify", amount="11000.00", when=utc(2026, 11, 15), firefly=False)
    await add_item(db, None, "di0", merchant="Disney", amount="20000.00", when=utc(2026, 10, 20), firefly=False)
    await add_item(db, None, "di1", merchant="Disney", amount="20000.00", when=utc(2026, 11, 19), firefly=False)

    # Feature on (default): the AI job owns price_increase; the old path only announces new subscriptions.
    assert await reports.alert_check(db, settings, None) == 1
    kinds = {alert.type for alert in (await db.scalars(select(Alert))).all()}
    assert kinds == {"new_subscription"}

    # Feature off: the old path emits price_increase again.
    await ai_features.update(db, features={"subscriptions": False})
    await db.commit()
    assert await reports.alert_check(db, settings, None) == 1
    assert {alert.type for alert in (await db.scalars(select(Alert))).all()} == {"new_subscription", "price_increase"}

    # Feature on and Disney judged not to be a subscription: nothing is announced for it.
    for alert in (await db.scalars(select(Alert))).all():
        await db.delete(alert)
    await ai_features.update(db, features={"subscriptions": True})
    db.add(AiNote(kind="subscription", key="disney", text="Es un plan familiar", data={"verdict": "other", "is_subscription": False}))
    await db.commit()
    from app.db.models import Notification

    for row in (await db.scalars(select(Notification))).all():
        await db.delete(row)
    await db.commit()
    assert await reports.alert_check(db, settings, None) == 0


# --- Cycle hook, routes, schema ---------------------------------------------------------------------------------------------


async def test_after_cycle_runs_every_step_and_never_raises(db, llm, fake, settings, clients):
    await add_item(db, fake, "t1")
    llm.complete_json.side_effect = RuntimeError("down")
    result = await ai_insights.after_cycle(db, clients[0], llm, settings)
    assert set(result) == {"categorize", "budgets", "anomalies", "subscriptions", "narrative", "events"}
    assert result["categorize"] == {"skipped": "llm"}
    clients[0].categories.side_effect = FireflyError("down")
    assert (await ai_insights.after_cycle(db, clients[0], llm, settings))["categorize"] in ({"skipped": "firefly"}, {"skipped": "nothing_to_do"})


def test_routes_declare_read_for_reads_and_review_for_writes():
    from app.main import app
    from tests.test_route_scopes import api_routes, route_scopes

    wanted = {
        ("/api/v1/search/parse", ("POST",)): ["read"],
        ("/api/v1/reports/narrative", ("GET",)): ["read"],
        ("/api/v1/reports/narrative", ("POST",)): ["review"],
        ("/api/v1/budgets/suggest", ("POST",)): ["review"],
        ("/api/v1/settings/ai-features", ("GET",)): ["read"],
        ("/api/v1/settings/ai-features", ("PUT",)): ["review"],
        ("/api/v1/ai/category-suggestions", ("GET",)): ["read"],
        ("/api/v1/ai/category-suggestions/apply", ("POST",)): ["review"],
        ("/api/v1/ai/category-suggestions/{suggestion_id}/discard", ("POST",)): ["review"],
        ("/api/v1/ai/subscriptions", ("GET",)): ["read"],
    }
    scopes = {(route.path, tuple(sorted(route.methods))): route_scopes(route) for route in api_routes(app) if (route.path, tuple(sorted(route.methods))) in wanted}
    assert scopes == wanted


def test_migration_matches_the_models_and_is_chained():
    spec = importlib.util.spec_from_file_location("ai_insights_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (module.revision, module.down_revision) == ("20261124_12", "20261124_11")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE inbox_items (id INTEGER PRIMARY KEY)"))
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        module.upgrade()
    inspector = sa.inspect(engine)
    for model in (AiItemSuggestion, AiNarrative, AiNote):
        assert {c["name"] for c in inspector.get_columns(model.__tablename__)} == {c.name for c in model.__table__.columns}
    with pytest.raises(RuntimeError):
        module.downgrade()
    engine.dispose()
