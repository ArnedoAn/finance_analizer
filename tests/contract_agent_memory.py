"""Contract section ``agent_memory``: long-task agent, simulator, weekly coach, assistant memory and question alerts.

Everything is offline: the LLM answers are scripted (``complete_json``), AI jobs are run one by one with ``ai_jobs.run_job`` and
the clock of the agent modules is pinned (``CLOCK``) so windows, weeks and projections never depend on the day of the run.
Docs: ``docs/contract-additions/ServerAgent.md`` and ``ServerMemory.md``.
"""
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.db.models import (
    AgentTask,
    AiItemSuggestion,
    AiJob,
    AlertMute,
    AlertQuestion,
    CoachTip,
    InboxItem,
)
from app.services import agent, agent_tools, ai_jobs, alert_questions, coach, simulator
from app.services import ledger as ledger_service

CLOCK = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)  # a Saturday: the coach week is the one starting on Monday 2026-11-02


def at(month, day, hour=17):
    return datetime(2026, month, day, hour, 0, tzinfo=UTC)


def clock():
    return CLOCK


async def add(db, ref, *, merchant, amount, when, category=None, kind="withdrawal"):
    """A registered movement without Firefly id (so the question alerts never pick it up). Returns its id."""
    row = InboxItem(
        source="sms", source_ref=f"agent-memory-{ref}", sender="Bancolombia", subject_or_title="", raw_text="x", status="created",
        amount=Decimal(amount), currency="COP", merchant=merchant, category=category, asset_account="Nequi", transaction_type=kind,
        occurred_at=when,
    )
    db.add(row)
    await db.commit()
    return row.id


# --- Agent tasks ---------------------------------------------------------------------------------------------------------


async def agent_tasks(env, patch):
    rec, db, reviewer = env.rec, env.db, env.reviewer
    firefly, llm = env.clients
    assert await rec.call("get_agent_policy", "GET", "/settings/agent", reviewer) == {"max_calls": 60}
    assert await rec.call("update_agent_policy", "PUT", "/settings/agent", reviewer, json={"max_calls": 40}) == {"max_calls": 40}

    bakery = await add(db, "bakery", merchant="PANADERIA EL TRIGAL", amount="18200.00", when=at(10, 15))
    pharmacy = await add(db, "pharmacy", merchant="FARMACIA SALUD", amount="36500.00", when=at(10, 18))

    created = await rec.call(
        "create_agent_task", "POST", "/agent/tasks", reviewer, status=202,
        json={"instruction": "Ordena los movimientos sin categoría de octubre", "month": "2026-10", "run_now": False},
    )
    assert set(created) == {"id"}
    task_id = created["id"]
    spare_id = (await rec.call(
        None, "POST", "/agent/tasks", reviewer, status=202, json={"instruction": "Revisa los cobros repetidos de octubre", "month": "2026-10"},
    ))["id"]
    waiting = await rec.call(None, "GET", f"/agent/tasks/{task_id}", reviewer)
    assert waiting["status"] == "deferred" and waiting["steps"] == [] and waiting["batch"] is None and waiting["run_now"] is False
    assert waiting["limits"] == {"max_steps": 40, "max_minutes": 25, "max_calls": 40}

    # The second task is cancelled while it still waits in the AI queue.
    cancelled = await rec.call("cancel_agent_task", "POST", f"/agent/tasks/{spare_id}/cancel", reviewer)
    assert cancelled["status"] == "cancelled" and cancelled["batch"] is None and cancelled["steps"] == []
    again = await rec.call(None, "POST", f"/agent/tasks/{spare_id}/cancel", reviewer, status=409)
    assert again["code"] == "task_not_active"
    assert (await rec.call(None, "GET", "/agent/tasks/999999", reviewer, status=404))["code"] == "task_not_found"

    # The first one runs from the queue with a scripted model: two reads, two proposals, one refused proposal and the summary.
    llm.complete_json = AsyncMock(side_effect=[
        {"action": "tool", "tool": "list_ledger", "args": {"category": "none", "merchant": "panaderia"}},
        {"action": "tool", "tool": "list_ledger", "args": {"category": "none", "merchant": "farmacia"}},
        {"action": "tool", "tool": "propose_change", "args": {"target_id": bakery, "field": "category", "new_value": "alimentación", "reason": "Una panadería vende alimentos"}},
        {"action": "tool", "tool": "propose_change", "args": {"target_id": pharmacy, "field": "tags", "new_value": ["salud"], "reason": "Compra en farmacia"}},
        {"action": "tool", "tool": "propose_change", "args": {"target_id": 999999, "field": "category", "new_value": "Transporte", "reason": "No leído antes"}},
        {"action": "finish", "summary": "Revisé octubre y dejé 2 propuestas para que las revises: una categoría y una etiqueta."},
    ])
    row = await db.get(AgentTask, task_id, populate_existing=True)
    job = await db.get(AiJob, row.job_id)
    assert job.runner_key == "agent_task" and job.payload == {"task_id": task_id}
    result = await ai_jobs.run_job(ai_jobs.JobContext(db, firefly, llm, env.settings), job)
    assert result["status"] == "done" and result["steps"] == 5 and result["batch_id"], result

    detail = await rec.call("get_agent_task", "GET", f"/agent/tasks/{task_id}", reviewer)
    assert detail["status"] == "done" and detail["steps_count"] == 5 and detail["calls_used"] == 6 and detail["error"] is None
    assert detail["month"] == "2026-10" and detail["window"]["from"].startswith("2026-10-01")
    assert detail["batch"]["status"] == "open" and detail["batch"]["proposals"] == 2 and detail["summary"].startswith("Revisé octubre")
    assert [(step["tool"], step["ok"]) for step in detail["steps"]] == [
        ("list_ledger", True), ("list_ledger", True), ("propose_change", True), ("propose_change", True), ("propose_change", False),
    ]
    listing = await rec.call("list_agent_tasks", "GET", "/agent/tasks", reviewer, params={"limit": 50, "offset": 0})
    by_id = {entry["id"]: entry for entry in listing["items"]}
    assert listing["total"] == len(listing["items"]) and by_id[task_id]["status"] == "done" and by_id[spare_id]["status"] == "cancelled"
    assert "steps" not in by_id[task_id] and listing["items"][0]["id"] == spare_id  # newest first
    await rec.call(None, "PUT", "/settings/agent", reviewer, json={"max_calls": 60})


# --- Simulator -----------------------------------------------------------------------------------------------------------


async def simulation(env, patch):
    rec, db, reviewer = env.rec, env.db, env.reviewer
    llm = env.clients[1]
    for month in (8, 9, 10):
        await add(db, f"income-{month}", merchant="EMPRESA DEMO SAS", amount="3000000.00", when=at(month, 10), category="Ingresos", kind="deposit")
        await add(db, f"rent-{month}", merchant="ARRIENDO APARTAMENTO", amount="2000000.00", when=at(month, 10), category="Vivienda")
    llm.complete_json = AsyncMock(side_effect=[
        {"kind": "installment_purchase", "label": "Portátil a 12 cuotas", "price": 1200000, "installments": 12, "rate_monthly_pct": 2},
        {"text": "La cuota se suma a tus gastos fijos de cada mes. Revisa que el saldo mensual siga siendo positivo antes de comprar. Si algún mes queda ajustado, conviene dar una cuota inicial."},
    ])
    body = await rec.call(
        "simulate", "POST", "/simulate", reviewer,
        json={"question": "¿Puedo comprar un portátil de 1,2 millones a 12 cuotas al 2 % mensual?"},
    )
    assert body["scenario"]["kind"] == "installment_purchase" and body["scenario"]["label"] == "Portátil a 12 cuotas"
    assert body["scenario"]["params"]["installments"] == 12 and body["scenario"]["params"]["price"] == "1200000.00"
    outcome = body["scenario"]["outcome"]
    assert outcome["installments"] == 12 and Decimal(outcome["interest"]) > 0 and Decimal(outcome["total_paid"]) > Decimal("1200000")
    assert len(body["months"]) == 12 and body["months"][0]["month"] == "2026-12" and body["assumptions"] and body["summary"]


# --- Weekly coach --------------------------------------------------------------------------------------------------------


async def coach_tips(env, patch):
    rec, db, reviewer = env.rec, env.db, env.reviewer
    firefly, llm = env.clients
    # Leisure spending went from 50.000 (Sep 7 - Oct 4) to 300.000 (Oct 5 - Nov 1); the Mercado budget is over its limit.
    await add(db, "club-before", merchant="CLUB DEPORTIVO", amount="50000.00", when=at(9, 15), category="Ocio")
    for number in range(3):
        await add(db, f"club-{number}", merchant="CLUB DEPORTIVO", amount="100000.00", when=at(10, 20), category="Ocio")
    firefly.budget_progress = AsyncMock(return_value=[{"name": "Mercado", "spent": Decimal("130000"), "limit": Decimal("100000")}])

    async def model(prompt, content, request_id, usage=None):
        facts = {entry["kind"]: entry["facts"] for entry in content["UNTRUSTED_DATA"]["candidates"]}
        up, over = facts["category_up"], facts["budget_over"]
        return {"tips": [
            {
                "kind": "category_up", "title": f"{up['categoría']} subió",
                "detail": f"En las últimas 4 semanas gastaste {up['últimas_4_semanas']} en {up['categoría']}, frente a {up['4_semanas_anteriores']} antes. Subió {up['aumento']}.",
                "suggestion": "Revisa las compras más grandes de la categoría.",
            },
            {
                "kind": "budget_over", "title": f"Presupuesto {over['presupuesto']} excedido",
                "detail": f"Llevas {over['gastado']} en {over['presupuesto']} y su límite es {over['límite']}. Te pasaste por {over['excedido_por']}.",
                "suggestion": "Frena las compras de este presupuesto hasta fin de mes.",
            },
        ]}

    llm.complete_json = AsyncMock(side_effect=model)
    assert await coach.generate(db, firefly, llm, env.settings, auto=False) == {"tips": 2, "week": "2026-11-02"}

    first = await rec.call(None, "GET", "/coach/tips", reviewer)
    assert first["total"] == 2 and {tip["kind"] for tip in first["items"]} == {"category_up", "budget_over"}
    tip = next(entry for entry in first["items"] if entry["kind"] == "category_up")
    assert tip["useful"] is None and tip["feedback_at"] is None and tip["week"] == "2026-11-02"
    assert any(item.startswith("item:") for item in tip["evidence"]) and tip["evidence"][-1].startswith("Ocio:")

    voted = await rec.call("send_coach_feedback", "POST", f"/coach/tips/{tip['id']}/feedback", reviewer, json={"useful": True})
    assert voted["id"] == tip["id"] and voted["useful"] is True and voted["feedback_at"]
    assert (await rec.call(None, "POST", "/coach/tips/999999/feedback", reviewer, status=404, json={"useful": False}))["code"] == "tip_not_found"
    listed = await rec.call("list_coach_tips", "GET", "/coach/tips", reviewer, params={"limit": 20, "offset": 0})
    assert listed["total"] == 2 and sorted(entry["useful"] is True for entry in listed["items"]) == [False, True]
    assert (await db.scalar(select(CoachTip.useful).where(CoachTip.id == tip["id"]), execution_options={"populate_existing": True})) is True


# --- Assistant memory ----------------------------------------------------------------------------------------------------


def answers(model, rule=None, error=None):
    """What the model says to the next memory translations: ``{"rule": rule}``, or a failure."""
    model.side_effect = error
    model.return_value = {"rule": rule}


async def memory(env, patch):
    rec, db, reviewer = env.rec, env.db, env.reviewer
    llm = env.clients[1]
    model = AsyncMock()
    llm.complete_json = model

    async def create(text, kind, **extra):
        return await rec.call(None, "POST", "/memories", reviewer, status=201, json={"text": text, "kind": kind, **extra})

    fact = await create("Vivo en Cartagena", "fact")
    assert fact["kind"] == "fact" and fact["origin"] == "owner" and fact["rule_proposal"] is None and fact["rule_ref"] is None
    answers(model)
    preference = await create("Prefiero ver los montos completos", "preference", origin="assistant")
    assert preference["origin"] == "assistant" and preference["rule_proposal"] is None

    answers(model, {"type": "category_rule", "match": "merchant_contains", "pattern": "JUAN", "category": "transporte"})
    created = await rec.call(
        "create_memory", "POST", "/memories", reviewer, status=201, json={"text": "Los pagos a JUAN son transporte", "kind": "rule", "origin": "owner"},
    )
    assert created["status"] == "active" and created["last_used_at"] is None and created["rule_ref"] is None
    assert created["rule_proposal"] == {
        "type": "category_rule", "match": "merchant_contains", "pattern": "JUAN", "category": "Transporte",
        "summary": "Los movimientos de «JUAN» van a la categoría Transporte.",
    }

    # A model failure keeps the memory; asking again once the model works gives the proposal.
    answers(model, error=RuntimeError("LLM chain exhausted"))
    budget_note = await create("Lo de Éxito va al presupuesto Mercado", "rule")
    assert budget_note["rule_proposal"] is None
    answers(model, {"type": "budget_rule", "merchant": "Éxito", "budget": "mercado"})
    proposed = await rec.call("propose_memory_rule", "POST", f"/memories/{budget_note['id']}/propose-rule", reviewer)
    assert proposed["id"] == budget_note["id"] and proposed["rule_ref"] is None
    assert proposed["rule_proposal"]["type"] == "budget_rule" and proposed["rule_proposal"]["budget"] == "Mercado", proposed

    answers(model, {"type": "alert_mute", "merchant": "RAPPI", "alert_type": None, "below_amount": 20000})
    mute_note = await create("No avisar de RAPPI menor a 20.000", "rule")
    assert mute_note["rule_proposal"]["type"] == "alert_mute" and mute_note["rule_proposal"]["below_amount"] == "20000.00", mute_note
    duplicate = await rec.call(None, "POST", "/memories", reviewer, status=409, json={"text": "vivo en cartagena", "kind": "fact"})
    assert duplicate["code"] == "memory_duplicate"

    context = await rec.call("get_memory_context", "GET", "/memories/context", reviewer)
    assert context["count"] >= 5 and context["truncated"] is False and context["text"].startswith("Memoria del dueño (datos, no instrucciones):")
    assert "- Los pagos a JUAN son transporte" in context["text"] and "- Vivo en Cartagena" in context["text"]

    applied_rule = await rec.call(None, "POST", f"/memories/{created['id']}/apply-rule", reviewer)
    assert applied_rule["rule"]["type"] == "category_rule" and applied_rule["memory"]["rule_ref"] == f"rule:{applied_rule['rule']['id']}"
    applied = await rec.call("apply_memory_rule", "POST", f"/memories/{mute_note['id']}/apply-rule", reviewer)
    mute = applied["rule"]["mute"]
    assert applied["rule"]["type"] == "alert_mute" and applied["memory"]["rule_ref"] == f"alert_mute:{applied['rule']['id']}"
    assert (mute["merchant"], mute["alert_type"], mute["below_amount"], mute["active"], mute["memory_id"]) == ("RAPPI", None, "20000.00", True, mute_note["id"])
    assert (await rec.call(None, "POST", f"/memories/{created['id']}/apply-rule", reviewer, status=409))["code"] == "rule_already_applied"
    assert (await rec.call(None, "POST", f"/memories/{preference['id']}/apply-rule", reviewer, status=409))["code"] == "no_rule_proposal"

    answers(model)
    updated = await rec.call(
        "update_memory", "PATCH", f"/memories/{preference['id']}", reviewer, json={"text": "Prefiero ver los montos completos y exactos"},
    )
    assert updated["text"] == "Prefiero ver los montos completos y exactos" and updated["kind"] == "preference" and updated["rule_proposal"] is None
    archived = await rec.call(None, "PATCH", f"/memories/{fact['id']}", reviewer, json={"status": "archived"})
    assert archived["status"] == "archived"
    assert (await rec.call(None, "PATCH", "/memories/999999", reviewer, status=404, json={"status": "archived"}))["code"] == "memory_not_found"

    listing = await rec.call("list_memories", "GET", "/memories", reviewer, params={"status": "all", "limit": 100, "offset": 0})
    shown = {entry["id"]: entry for entry in listing["items"]}
    assert listing["total"] == len(listing["items"]) and listing["items"][0]["id"] == mute_note["id"]  # newest first
    assert shown[fact["id"]]["status"] == "archived" and shown[budget_note["id"]]["rule_proposal"]["type"] == "budget_rule"
    assert shown[created["id"]]["rule_ref"].startswith("rule:") and shown[mute_note["id"]]["rule_ref"].startswith("alert_mute:")
    assert shown[created["id"]]["last_used_at"] is not None and shown[preference["id"]]["origin"] == "assistant"

    deleted = await rec.call("delete_memory", "DELETE", f"/memories/{mute_note['id']}", reviewer)
    assert deleted == {"deleted": True}
    silence = await db.scalar(select(AlertMute).where(AlertMute.id == applied["rule"]["id"]), execution_options={"populate_existing": True})
    assert silence.active is False and silence.memory_id is None  # the mute is switched off, so it never leaks to other sections


# --- Question alerts -----------------------------------------------------------------------------------------------------


async def questions(env, patch):
    rec, db, reviewer, service = env.rec, env.db, env.reviewer, env.service
    limits = await rec.call("update_question_settings", "PUT", "/settings/questions", reviewer, json={"daily_limit": 1, "shared_accounts": ["nequi"]})
    assert limits == {"daily_limit": 1, "shared_accounts": ["Nequi"], "asked_today": 0}
    unknown = await rec.call(None, "PUT", "/settings/questions", reviewer, status=422, json={"daily_limit": 1, "shared_accounts": ["Cuenta inexistente"]})
    assert unknown["code"] == "unknown_account"

    row = await service.inbox.add(
        source="sms", source_ref="agent-memory-taxi", sender="85954", subject_or_title="Compra", raw_text="Compraste $120.000 en TAXIS EL DORADO",
        occurred_at=at(11, 6, 18), status="created", method="parser", transaction_type="withdrawal", amount=Decimal("120000.00"), currency="COP",
        merchant="TAXIS EL DORADO", asset_account="Nequi", category="Alimentación", firefly_transaction_id="ff-agent-memory-taxi",
    )
    await service.session.commit()
    item_id, ledger_id = row.id, row.firefly_transaction_id
    db.add(AiItemSuggestion(item_id=item_id, status="pending", category="Transporte", category_confidence=0.55))
    await db.commit()
    assert await alert_questions.generate(db, env.settings) == 1
    question = await db.scalar(select(AlertQuestion).where(AlertQuestion.item_id == item_id))
    assert question is not None and question.kind == "category" and question.choices[0] == {"id": "c1", "label": "Transporte"}
    assert question.choices[-1] == {"id": "skip", "label": "Ahora no"}
    alert_id = question.alert_id

    shown = (await rec.call(None, "GET", "/alerts", reviewer, params={"after_id": alert_id - 1, "limit": 50, "include_acked": "true"}))["items"][0]
    assert shown["id"] == alert_id and shown["type"] == "question" and shown["question_status"] == "open" and shown["choices"] == question.choices
    assert (await rec.call(None, "POST", f"/alerts/{alert_id}/answer", reviewer, status=422, json={"choice": "c9"}))["code"] == "invalid_choice"

    calls = []

    async def bulk(session, processor, body, principal):
        """Stand-in for ``ledger.bulk`` (Firefly): applies the change on the inbox item like the real one does."""
        calls.append(body)
        target = await session.scalar(select(InboxItem).where(InboxItem.firefly_transaction_id == body.ids[0]))
        target.category = body.changes.category
        await session.flush()
        return "contract-group", list(body.ids), []

    patch.setattr(ledger_service, "bulk", bulk)
    answered = await rec.call("answer_alert", "POST", f"/alerts/{alert_id}/answer", reviewer, json={"choice": "c1"})
    assert answered["choice"] == "c1" and answered["result"] == {"action": "category", "category": "Transporte"}
    assert answered["alert"]["question_status"] == "answered" and answered["alert"]["acked_at"] and answered["alert"]["id"] == alert_id
    assert len(calls) == 1 and calls[0].ids == [ledger_id] and calls[0].changes.category == "Transporte"
    again = await rec.call(None, "POST", f"/alerts/{alert_id}/answer", reviewer, status=409, json={"choice": "c1"})
    assert again["code"] == "already_answered"

    current = await rec.call("get_question_settings", "GET", "/settings/questions", reviewer)
    assert current == {"daily_limit": 1, "shared_accounts": ["Nequi"], "asked_today": 1}
    await rec.call(None, "PUT", "/settings/questions", reviewer, json={"daily_limit": 3, "shared_accounts": []})


async def record(env):
    firefly = env.clients[0]
    saved_budgets, saved_bills = firefly.budget_progress.return_value, firefly.bills_status.return_value
    with env.monkeypatch.context() as patch:
        for module in (agent, agent_tools, simulator, coach):
            patch.setattr(module, "now_utc", clock)
        env.clients[0].bills_status = AsyncMock(return_value=[])
        await agent_tasks(env, patch)
        await simulation(env, patch)
        await coach_tips(env, patch)
    with env.monkeypatch.context() as patch:
        await memory(env, patch)
    with env.monkeypatch.context() as patch:
        await questions(env, patch)
    # Later sections get the Firefly mock as the main test left it (a replaced attribute is not restored by monkeypatch).
    firefly.budget_progress = AsyncMock(return_value=saved_budgets)
    firefly.bills_status = AsyncMock(return_value=saved_bills)
