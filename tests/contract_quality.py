"""Contract calls of the AI insight and quality endpoints: search, category proposals, narrated report, budget proposals,
subscription verdicts, AI switches and budget policy, the AI job queue, trips/events and credit-card advice.

Everything is deterministic: fixed dates (the peak clock lives in 2030, far from any fixture date), simulated LLM answers and a
stateful Firefly stand-in. ``record(env)`` is called by ``test_contract_fixtures``.
"""
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.db.models import AiItemSuggestion, InboxItem
from app.services import (
    ai_budget,
    ai_categorize,
    ai_events,
    ai_jobs,
    ai_narrative,
    ai_search,
    ai_subscriptions,
    budget_suggest,
)
from tests.test_ledger_v4 import FireflyLedger

PEAK = datetime(2030, 1, 7, 2, 0, tzinfo=UTC)  # Monday 02:00 UTC: peak until 04:00
REPORT_NOW = datetime(2026, 11, 6, 17, 0, tzinfo=UTC)
EVENTS_NOW = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)


def _async(function):
    return AsyncMock(side_effect=function)


async def _item(env, ledger, ref, *, merchant, amount, when, category=None, kind="withdrawal"):
    """A registered movement that also lives in the stand-in ledger."""
    group = ledger.seed(external_id=f"fa:{ref}", tags=["auto", "sms"], category_name=category, amount=amount)
    row = InboxItem(
        source="sms", source_ref=ref, sender="Bancolombia", subject_or_title="", raw_text="x", status="created", amount=Decimal(amount),
        currency="COP", merchant=merchant, category=category, asset_account="Nequi", transaction_type=kind, firefly_transaction_id=group,
        tags=[], occurred_at=when,
    )
    env.db.add(row)
    await env.db.commit()
    return row


def _answer(by_merchant):
    """A model answer for the category batch: ``{merchant: (category, confidence)}``; unknown merchants get nothing."""
    def answer(prompt, content, cycle_id, usage=None):
        if usage is not None:
            usage.update({"model": "deepseek-v4.1-flash", "calls": 1})
        rows = []
        for entry in content["UNTRUSTED_DATA"]:
            category, confidence = by_merchant.get(entry["merchant"], (None, 0))
            rows.append({
                "id": entry["id"], "category": category, "category_confidence": confidence, "tags": [], "tags_confidence": 0,
                "reason": f"Comercio habitual de {category}" if category else "",
            })
        return {"items": rows}
    return answer


def _narrator(prompt, content, cycle_id, usage=None):
    facts = content["UNTRUSTED_DATA"]["facts"]
    if usage is not None:
        usage.update({"model": "deepseek-v4.1-flash", "calls": 1})
    return {"text": " ".join([
        f"En {facts['mes']} gastaste {facts['gasto_total']} y recibiste {facts['ingresos']}.",
        f"El mes anterior habías gastado {facts['mes_anterior']['gasto_total']}.",
        f"Frente a ese mes la diferencia fue de {facts['frente_al_mes_anterior']['diferencia']}.",
        f"Frente al promedio de tres meses ({facts['promedio_3_meses']['gasto_total']}) la diferencia fue de {facts['frente_al_promedio']['diferencia']}.",
        f"Tu saldo del mes fue {facts['saldo']}.",
    ])}


def _judge(prompt, content, cycle_id, usage=None):
    if usage is not None:
        usage.update({"model": "deepseek-v4.1-flash", "calls": 1})
    return {"items": [{"id": entry["id"], "verdict": "subscription", "reason": f"Es un servicio de {entry['merchant']}"} for entry in content["UNTRUSTED_DATA"]]}


async def record(env):
    rec, db, reviewer = env.rec, env.db, env.reviewer
    firefly, llm = env.clients
    ledger = FireflyLedger(
        firefly, [*(await env.service.accounts()), {"id": "9", "name": "Tarjeta A", "type": "liabilities"}, {"id": "10", "name": "Tarjeta B", "type": "liabilities"}],
        budgets={"11": "Mercado"}, bills={},
    )
    firefly.accounts.return_value = [*firefly.accounts.return_value, {"id": "9", "name": "Tarjeta A", "type": "liabilities"}, {"id": "10", "name": "Tarjeta B", "type": "liabilities"}]
    firefly.categories.return_value = ["Alimentación", "Transporte", "Ocio"]

    # -- Switches and budget policy -----------------------------------------------------------------------------------------
    features = await rec.call("get_ai_features", "GET", "/settings/ai-features", reviewer)
    assert features["second_opinion"]["model"] == "mimo-v2.6-flash"
    updated = await rec.call(
        "update_ai_features", "PUT", "/settings/ai-features", reviewer,
        json={"features": {"coach": True}, "second_opinion": {"enabled": True, "model": "mimo-v2.6-flash", "min_amount_cop": "1500000", "verified_max_amount_cop": "5000000"}},
    )
    assert updated["second_opinion"] == {"enabled": True, "model": "mimo-v2.6-flash", "min_amount_cop": "1500000.00", "verified_max_amount_cop": "5000000.00"}
    await rec.call(None, "PUT", "/settings/ai-features", reviewer, json={"second_opinion": {"enabled": False, "min_amount_cop": "1000000", "verified_max_amount_cop": None}})
    budget = await rec.call("get_ai_budget", "GET", "/settings/ai-budget", reviewer)
    assert budget["peak_policy"] == "ask"
    changed = await rec.call("update_ai_budget", "PUT", "/settings/ai-budget", reviewer, json={"daily_calls": 500, "peak_policy": "ask"})
    assert changed["daily_calls"] == 500
    await rec.call(None, "PUT", "/settings/ai-budget", reviewer, json={"daily_calls": 400})

    # -- Natural-language search --------------------------------------------------------------------------------------------
    ai_search.clear_cache()
    llm.complete_json = _async(lambda *args, **kwargs: {
        "filters": {"from": "2026-10-01", "to": "2026-10-31", "category": "Alimentación", "kind": "withdrawal", "min_amount": 50000, "text": None},
        "explanation": "Gastos de alimentación en octubre de más de $50.000.",
    })
    parsed = await rec.call("parse_search", "POST", "/search/parse", reviewer, json={"q": "Gastos de comida del mes pasado de más de 50 mil"})
    assert parsed["filters"]["category"] == "Alimentación" and parsed["filters"]["min_amount"] == "50000.00"

    # -- Category and tag proposals (a confident one is applied by the server, two wait for the owner) -------------------
    recent = datetime.now(UTC) - timedelta(hours=3)
    auto = await _item(env, ledger, "cq-auto", merchant="PANADERIA UNO", amount="9000.00", when=recent)
    apply_me = await _item(env, ledger, "cq-apply", merchant="TAXI DOS", amount="18000.00", when=recent - timedelta(minutes=5))
    discard_me = await _item(env, ledger, "cq-discard", merchant="CINEMA TRES", amount="24000.00", when=recent - timedelta(minutes=10))
    llm.complete_json = _async(_answer({"PANADERIA UNO": ("Alimentación", 0.95), "TAXI DOS": ("Transporte", 0.6), "CINEMA TRES": ("Ocio", 0.55)}))

    async def only_mine(session, features, now=None):
        return [(auto, True, False), (apply_me, True, False), (discard_me, True, False)]

    with env.monkeypatch.context() as patch:
        patch.setattr(ai_categorize, "eligible", only_mine)
        counts = await ai_categorize.run_batch(db, firefly, llm, env.settings)
    assert counts == {"proposed": 3, "auto_applied": 1, "pending": 2, "skipped": None}, counts
    pending = await rec.call("list_category_suggestions", "GET", "/ai/category-suggestions", reviewer, params={"status": "pending"})
    assert pending["total"] == 2
    ids = {row["item_id"]: row["id"] for row in pending["items"]}
    applied = await rec.call("apply_category_suggestions", "POST", "/ai/category-suggestions/apply", reviewer, json={"ids": [ids[apply_me.id]]})
    assert applied == {"applied": [ids[apply_me.id]], "failed": []}
    discarded = await rec.call("discard_category_suggestion", "POST", f"/ai/category-suggestions/{ids[discard_me.id]}/discard", reviewer)
    assert discarded["status"] == "discarded"
    assert (await db.scalar(select(AiItemSuggestion.status).where(AiItemSuggestion.item_id == auto.id))) == "auto_applied"

    # History that the report and the budget proposals read (August to October, one category)
    for number, (month, amount) in enumerate(((8, "300000.00"), (9, "320000.00"), (10, "400000.00"))):
        await _item(env, ledger, f"cb-{number}", merchant="PLAZA DE MERCADO", amount=amount, when=datetime(2026, month, 10, 17, tzinfo=UTC), category="Despensa")

    # -- Narrated monthly report ------------------------------------------------------------------------------------------------
    llm.complete_json = _async(_narrator)
    with env.monkeypatch.context() as patch:
        patch.setattr(ai_narrative, "_local_month", lambda settings, now=None: "2026-11")
        generated = await rec.call("generate_narrative", "POST", "/reports/narrative", reviewer, params={"month": "2026-10"})
    assert generated["month"] == "2026-10" and generated["source"] == "owner" and "gastaste" in generated["text"]
    fetched = await rec.call("get_narrative", "GET", "/reports/narrative", reviewer, params={"month": "2026-10"})
    assert fetched["text"] == generated["text"]

    # -- Budget proposals (no model: the server computes them) -------------------------------------------------------------
    with env.monkeypatch.context() as patch:
        patch.setattr(budget_suggest, "now_utc", lambda: datetime(2026, 11, 5, 12, tzinfo=UTC))
        suggested = await rec.call("suggest_budgets", "POST", "/budgets/suggest", reviewer)
    assert suggested["months"] == ["2026-08", "2026-09", "2026-10"] and any(row["category"] == "Despensa" for row in suggested["items"])

    # -- Subscription verdicts -------------------------------------------------------------------------------------------------
    llm.complete_json = _async(_judge)
    with env.monkeypatch.context() as patch:
        patch.setattr("app.services.reports.now_utc", lambda: REPORT_NOW)
        result = await ai_subscriptions.run(db, llm, env.settings)
        assert result["judged"] >= 2, result
        subscriptions = await rec.call("list_subscriptions", "GET", "/ai/subscriptions", reviewer)
    assert any(row["is_subscription"] is True and row["verdict"] == "subscription" for row in subscriptions["items"])

    # -- AI job queue: four heavy jobs wait for the valley, the owner decides each one ----------------------------------------
    @ai_jobs.register("contract_job")
    async def runner(ctx, payload):
        return {"done": payload["name"]}

    ctx = ai_jobs.JobContext(db, firefly, llm, env.settings)
    with env.monkeypatch.context() as patch:
        patch.setattr(ai_budget, "_clock", lambda: PEAK)
        patch.setattr("app.api.routers.ai_jobs.kick", lambda: None)  # the test runs the due jobs itself, on its own session
        jobs = {}
        for kind, title in (
            ("categorize_batch", "Categorizar y etiquetar movimientos"), ("narrative", "Narrar el informe del mes anterior"),
            ("events", "Detectar viajes y eventos"), ("anomalies", "Explicar movimientos inusuales"),
        ):
            parked = await ai_jobs.dispatch(ctx, kind, title, "contract_job", {"name": kind}, estimated_calls=4, dedupe_key=f"contract:{kind}")
            jobs[kind] = parked["deferred"]
        listing = await rec.call("list_ai_jobs", "GET", "/ai/jobs", reviewer)
        assert listing["total"] == 4 and listing["peak"]["now"] is True and {row["status"] for row in listing["items"]} == {"waiting_offpeak"}
        assert all(row["estimated_allowance_pct_now"] == row["estimated_allowance_pct"] * 2 for row in listing["items"])
        now_job = await rec.call("run_ai_job_now", "POST", f"/ai/jobs/{jobs['categorize_batch']}/run-now", reviewer)
        assert (now_job["status"], now_job["decided_by"]) == ("queued", "owner")
        waited = await rec.call("wait_ai_job_for_valley", "POST", f"/ai/jobs/{jobs['narrative']}/wait", reviewer)
        assert (waited["status"], waited["decided_by"]) == ("waiting_offpeak", "owner")
        cancelled = await rec.call("cancel_ai_job", "POST", f"/ai/jobs/{jobs['events']}/cancel", reviewer)
        assert cancelled["status"] == "cancelled"
        everything = await rec.call("run_all_ai_jobs_now", "POST", "/ai/jobs/run-all-now", reviewer)
        assert everything == {"queued": 2}
        await ai_jobs.run_due(ctx)
        await ai_jobs.run_due(ctx)
        finished = await rec.call(None, "GET", "/ai/jobs", reviewer, params={"status": "done"})
        assert {row["result"]["done"] for row in finished["items"] if row["result"] and "done" in row["result"]} == {"categorize_batch", "narrative", "anomalies"}

    # -- Trips and events: two proposals, one applied (and tagged in Firefly), one discarded ------------------------------
    for number, (merchant, amount, day) in enumerate((("HOTEL POBLADO MEDELLIN", "320000.00", 12), ("RESTAURANTE EL CIELO MEDELLIN", "145000.00", 13), ("TAXI AEROPUERTO MEDELLIN", "60000.00", 14))):
        await _item(env, ledger, f"ev-med-{number}", merchant=merchant, amount=amount, when=datetime(2026, 11, day, 17, tzinfo=UTC))
    for number, (merchant, amount, day) in enumerate((("HOSTAL CENTRO HISTORICO CARTAGENA", "210000.00", 10), ("TOUR ISLAS CARTAGENA", "180000.00", 11), ("CEVICHERIA CARTAGENA", "95000.00", 12))):
        await _item(env, ledger, f"ev-car-{number}", merchant=merchant, amount=amount, when=datetime(2026, 10, day, 17, tzinfo=UTC))
    llm.complete_json = _async(lambda *args, **kwargs: {"items": []})
    with env.monkeypatch.context() as patch:
        patch.setattr(ai_events, "now_utc", lambda: EVENTS_NOW)
        outcome = await ai_events.run(db, llm, env.settings)
    assert outcome["proposed"] >= 2, outcome
    events = await rec.call("list_ai_events", "GET", "/ai/events", reviewer)
    trips = {row["city"]: row for row in events["items"] if row["kind"] == "trip"}
    assert set(trips) == {"medellin", "cartagena"}, events
    assert trips["medellin"]["label"] == "viaje-medellin-2026-11" and trips["medellin"]["total_amount"] == "525000.00"
    applied_event = await rec.call("apply_ai_event", "POST", f"/ai/events/{trips['medellin']['id']}/apply", reviewer, json={"label": "viaje-medellin-familia"})
    assert applied_event["status"] == "applied" and applied_event["label"] == "viaje-medellin-familia" and applied_event["group_id"]
    dropped = await rec.call("discard_ai_event", "POST", f"/ai/events/{trips['cartagena']['id']}/discard", reviewer)
    assert dropped["status"] == "discarded"

    # -- Credit cards ------------------------------------------------------------------------------------------------------
    firefly.account_transactions.return_value = [
        {"source_id": "9", "type": "withdrawal", "amount": "100000.00"}, {"source_id": "9", "type": "withdrawal", "amount": "150000.00"},
        {"source_id": "1", "destination_id": "9", "type": "transfer", "amount": "999.00"},
    ]
    for name, bank, cut, pay in (("Tarjeta A", "nu", 15, 28), ("Tarjeta B", "lulo", 25, 10)):
        card = await rec.call(None, "POST", "/own-accounts", reviewer, status=201, json={
            "firefly_account": name, "bank": bank, "kind": "credit", "statement_day": cut, "payment_day": pay,
        })
        assert (card["statement_day"], card["payment_day"]) == (cut, pay)
    advice = await rec.call("get_cards_advice", "GET", "/cards/advice", reviewer, params={"date": "2026-11-24"})
    assert advice["recommended"] == {"account": "Tarjeta A", "interest_free_days": 34}
    assert [row["account"] for row in advice["items"]] == ["Tarjeta A", "Tarjeta B"] and advice["items"][0]["period_spent"] == "250000.00"
    assert json.dumps(advice)  # plain JSON
