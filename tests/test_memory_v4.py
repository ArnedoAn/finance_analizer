"""Assistant memory, alert mutes and question alerts (Firefly and LLM mocked)."""
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.timezone import now_utc
from app.db.models import (
    AiItemSuggestion,
    AlertMute,
    AlertQuestion,
    InboxItem,
    Memory,
    MerchantCorrection,
)
from app.db.models_alerts import Alert
from app.db.models_assistant import Audit
from app.db.models_rules import Merchant, Rule
from app.services import alert_questions, memories, rules
from app.services import alerts as alerting
from app.services import ledger as ledger_service

ADMIN = {"X-API-Key": "test-admin-key-01234567890123456789"}
API = "/api/v1"


@pytest.fixture(autouse=True)
def alert_settings(monkeypatch, settings):
    monkeypatch.setattr(alerting, "get_settings", lambda: settings)


@pytest.fixture(autouse=True)
def catalogs(clients):
    firefly, _ = clients
    firefly.categories.return_value = ["Alimentación", "Transporte", "Arriendo"]
    firefly.budgets.return_value = [{"id": "1", "name": "Mercado"}]


@pytest.fixture
def fake_bulk(monkeypatch):
    """Stand-in for ``ledger.bulk``: writes the change on the inbox item like the real one does and records the bodies."""
    calls = []

    async def bulk(session, processor, body, principal):
        calls.append(body)
        item = await session.scalar(select(InboxItem).where(InboxItem.firefly_transaction_id == body.ids[0]))
        if body.changes.category:
            item.category = body.changes.category
        if body.changes.tags_add:
            item.tags = [*(item.tags or []), *body.changes.tags_add]
        await session.flush()
        return "group-1", list(body.ids), []

    monkeypatch.setattr(ledger_service, "bulk", bulk)
    return calls


async def item(service, **changes):
    values = dict(
        source="sms", source_ref=str(uuid4()), sender="85954", subject_or_title="Compra", raw_text="Compraste $12.000",
        occurred_at=now_utc() - timedelta(hours=1), status="created", method="parser", transaction_type="withdrawal",
        amount=Decimal("12000.00"), currency="COP", merchant="TIENDA UNO", asset_account="Nequi", category="Alimentación",
        firefly_transaction_id=str(uuid4()),
    )
    values.update(changes)
    row = await service.inbox.add(**values)
    await service.session.commit()
    return row


async def doubtful(service, db, confidence=0.7, **changes):
    """A registered movement whose pending AI category is not confident enough to be applied alone."""
    row = await item(service, **changes)
    db.add(AiItemSuggestion(item_id=row.id, status="pending", category="Transporte", category_confidence=confidence))
    await db.commit()
    return row


async def audits(db, action):
    return list((await db.scalars(select(Audit).where(Audit.action == action).order_by(Audit.id))).all())


def model_says(service, rule):
    service.llm.complete_json.return_value = {"rule": rule}


async def post_memory(client, text, kind="preference"):
    response = await client.post(f"{API}/memories", headers=ADMIN, json={"text": text, "kind": kind})
    return response


# --- memories: CRUD and secrets ---------------------------------------------------------------------------------------


async def test_crud_and_archive(client, service):
    created = await post_memory(client, "Prefiero ver los montos completos", "fact")
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["origin"] == "owner" and body["status"] == "active" and body["rule_ref"] is None and body["rule_proposal"] is None
    assert body["last_used_at"] is None
    service.llm.complete_json.assert_not_called()  # a plain fact is not translated

    listed = (await client.get(f"{API}/memories", headers=ADMIN)).json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == body["id"]

    patched = await client.patch(f"{API}/memories/{body['id']}", headers=ADMIN, json={"text": "Prefiero montos completos", "kind": "preference"})
    assert patched.status_code == 200 and patched.json()["text"] == "Prefiero montos completos" and patched.json()["kind"] == "preference"

    archived = await client.patch(f"{API}/memories/{body['id']}", headers=ADMIN, json={"status": "archived"})
    assert archived.json()["status"] == "archived"
    assert (await client.get(f"{API}/memories", headers=ADMIN)).json()["total"] == 0
    assert (await client.get(f"{API}/memories", headers=ADMIN, params={"status": "all"})).json()["total"] == 1

    assert (await client.delete(f"{API}/memories/{body['id']}", headers=ADMIN)).json() == {"deleted": True}
    missing = await client.delete(f"{API}/memories/{body['id']}", headers=ADMIN)
    assert missing.status_code == 404 and missing.json()["code"] == "memory_not_found"


async def test_duplicates_and_invalid_kind(client):
    assert (await post_memory(client, "Vivo en Cartagena", "fact")).status_code == 201
    repeated = await post_memory(client, "vivo en  cartagena", "fact")
    assert repeated.status_code == 409 and repeated.json()["code"] == "memory_duplicate"
    assert (await client.post(f"{API}/memories", headers=ADMIN, json={"text": "algo", "kind": "otra"})).status_code == 422


@pytest.mark.parametrize("text", [
    "Escríbele a juan.perez@example.com cuando haya duda",
    "Mi cuenta es 1234567890123",
    "La clave temporal es Ab12Cd34",
    "Entra a https://banco.example.com/login?token=abc123 para pagar",
])
async def test_secrets_are_rejected(client, db, text):
    response = await post_memory(client, text)
    assert response.status_code == 422 and response.json()["code"] == "sensitive_memory"
    assert (await db.scalars(select(Memory))).all() == []
    kept = (await post_memory(client, "Pago el arriendo cada mes", "fact")).json()
    patched = await client.patch(f"{API}/memories/{kept['id']}", headers=ADMIN, json={"text": text})
    assert patched.status_code == 422 and patched.json()["code"] == "sensitive_memory"


async def test_scopes(client, db):
    from app.api.dependencies import hash_secret
    from app.db.models_devices import Device

    key = "reader-key-" + "0123456789" * 3
    db.add(Device(name="reader", key_hash=hash_secret(key), scopes=["read"]))
    await db.commit()
    reader = {"X-API-Key": key}
    assert (await client.get(f"{API}/memories", headers=reader)).status_code == 200
    assert (await client.get(f"{API}/memories/context", headers=reader)).status_code == 200
    assert (await client.post(f"{API}/memories", headers=reader, json={"text": "algo corto", "kind": "fact"})).status_code == 403


async def test_context_is_compact_and_marks_use(client, db):
    for number in range(40):
        await post_memory(client, f"Nota número {number} " + "x" * 120, "fact")
    await post_memory(client, "Nunca me avises de cobros pequeños", "rule")
    body = (await client.get(f"{API}/memories/context", headers=ADMIN)).json()
    assert len(body["text"].encode()) <= 2048 and body["truncated"] is True and body["count"] < 41
    assert body["text"].startswith("Memoria del dueño") and "- Nunca me avises de cobros pequeños" in body["text"]  # rules first
    used = (await db.scalars(select(Memory).where(Memory.last_used_at.is_not(None)))).all()
    assert len(used) == body["count"]
    empty = await memories.context_text(db)
    assert empty.startswith("Memoria del dueño")


async def test_empty_context(client):
    assert (await client.get(f"{API}/memories/context", headers=ADMIN)).json() == {"text": "", "count": 0, "truncated": False}


# --- memories: translation into rules ---------------------------------------------------------------------------------


async def test_category_rule_is_proposed_then_created_on_approval(client, service, db):
    model_says(service, {"type": "category_rule", "match": "merchant_contains", "pattern": "JUAN", "category": "arriendo"})
    created = (await post_memory(client, "Los pagos a JUAN son arriendo", "rule")).json()
    assert created["rule_proposal"] == {
        "type": "category_rule", "match": "merchant_contains", "pattern": "JUAN", "category": "Arriendo",
        "summary": "Los movimientos de «JUAN» van a la categoría Arriendo.",
    }
    sent = service.llm.complete_json.call_args.args[1]
    assert sent["UNTRUSTED_DATA"]["memory"] == "Los pagos a JUAN son arriendo"
    assert await db.scalar(select(Rule)) is None  # nothing is created before approval

    applied = await client.post(f"{API}/memories/{created['id']}/apply-rule", headers=ADMIN)
    assert applied.status_code == 200, applied.text
    rule = await db.scalar(select(Rule))
    assert (rule.match, rule.pattern, rule.category, rule.source) == ("merchant_contains", "JUAN", "Arriendo", "manual")
    assert applied.json()["memory"]["rule_ref"] == f"rule:{rule.id}" and applied.json()["rule"]["id"] == rule.id
    assert await audits(db, "memory.apply_rule")

    again = await client.post(f"{API}/memories/{created['id']}/apply-rule", headers=ADMIN)
    assert again.status_code == 409 and again.json()["code"] == "rule_already_applied"


async def test_budget_rule_creates_a_learned_mapping(client, service, db):
    from app.db.models import BudgetMap

    model_says(service, {"type": "budget_rule", "merchant": "Éxito", "budget": "mercado"})
    created = (await post_memory(client, "Lo de Éxito va al presupuesto Mercado", "rule")).json()
    assert created["rule_proposal"]["type"] == "budget_rule" and created["rule_proposal"]["budget"] == "Mercado"
    applied = await client.post(f"{API}/memories/{created['id']}/apply-rule", headers=ADMIN)
    assert applied.status_code == 200, applied.text
    entry = await db.scalar(select(BudgetMap))
    assert (entry.kind, entry.key, entry.budget, entry.learned) == ("merchant", rules.merchant_name("Éxito"), "Mercado", True)
    assert applied.json()["memory"]["rule_ref"] == f"budget_map:{entry.id}"


async def test_unusable_model_answers_leave_no_proposal(client, service):
    answers = [
        {"rule": None},
        {"rule": {"type": "category_rule", "match": "merchant_exact", "pattern": "OTRO", "category": "Arriendo"}},  # not in the note
        {"rule": {"type": "category_rule", "match": "merchant_exact", "pattern": "JUAN", "category": "Inventada"}},  # not in Firefly
        {"rule": {"type": "alert_mute", "merchant": "JUAN", "alert_type": None, "below_amount": 99999}},  # amount not in the note
        {"rule": {"type": "alert_mute", "merchant": "JUAN", "alert_type": "system_failure", "below_amount": 5000}},  # never mutable
        {"nada": 1},
    ]
    for index, answer in enumerate(answers):
        service.llm.complete_json.return_value = answer
        created = await post_memory(client, f"Los pagos a JUAN van a arriendo {index} con 5000", "rule")
        assert created.status_code == 201 and created.json()["rule_proposal"] is None, answer
        refused = await client.post(f"{API}/memories/{created.json()['id']}/apply-rule", headers=ADMIN)
        assert refused.status_code == 409 and refused.json()["code"] == "no_rule_proposal"


async def test_model_failure_keeps_the_memory_and_can_be_retried(client, service):
    service.llm.complete_json.side_effect = RuntimeError("LLM chain exhausted")
    created = await post_memory(client, "Los pagos a JUAN son arriendo", "rule")
    assert created.status_code == 201 and created.json()["rule_proposal"] is None
    service.llm.complete_json.side_effect = None
    model_says(service, {"type": "category_rule", "match": "merchant_exact", "pattern": "JUAN", "category": "Arriendo"})
    retried = await client.post(f"{API}/memories/{created.json()['id']}/propose-rule", headers=ADMIN)
    assert retried.status_code == 200 and retried.json()["rule_proposal"]["pattern"] == "JUAN"


async def test_editing_the_text_drops_the_old_proposal(client, service):
    model_says(service, {"type": "category_rule", "match": "merchant_exact", "pattern": "JUAN", "category": "Arriendo"})
    created = (await post_memory(client, "Los pagos a JUAN son arriendo", "rule")).json()
    model_says(service, None)
    patched = await client.patch(f"{API}/memories/{created['id']}", headers=ADMIN, json={"text": "Juan es un buen amigo"})
    assert patched.json()["rule_proposal"] is None


# --- alert mutes ------------------------------------------------------------------------------------------------------


async def mute_memory(client, service, merchant="RAPPI", alert_type=None, amount=20000):
    model_says(service, {"type": "alert_mute", "merchant": merchant, "alert_type": alert_type, "below_amount": amount})
    created = (await post_memory(client, f"No avisar de {merchant or 'cobros'} menor a 20.000", "rule")).json()
    assert created["rule_proposal"]["type"] == "alert_mute" and created["rule_proposal"]["below_amount"] == "20000.00"
    applied = await client.post(f"{API}/memories/{created['id']}/apply-rule", headers=ADMIN)
    assert applied.status_code == 200, applied.text
    return created["id"]


async def test_mute_is_respected_by_emit(client, service, db):
    await mute_memory(client, service)
    mute = await db.scalar(select(AlertMute))
    assert (mute.merchant, mute.alert_type, mute.below_amount, mute.active) == ("RAPPI", None, Decimal("20000.00"), True)

    small = await item(service, merchant="RAPPI", amount=Decimal("15000.00"))
    big = await item(service, merchant="RAPPI", amount=Decimal("25000.00"))
    other = await item(service, merchant="EXITO", amount=Decimal("15000.00"))

    async def emit(row, type="pending_review", key=None):
        return await alerting.emit(db, type, title="t", body="b", item_id=row.id, amount=row.amount, currency="COP", dedupe_key=key or f"{type}:{row.id}")

    assert await emit(small) is None
    assert await emit(big) is not None  # above the threshold
    assert await emit(other) is not None  # another merchant
    assert await emit(small, "system_failure") is not None  # operational alerts are never muted
    assert await alerting.emit(db, "pending_review", title="t", body="b", item_id=small.id, dedupe_key="no-amount") is not None


async def test_mute_without_merchant_covers_a_type_and_follows_the_memory(client, service, db):
    memory_id = await mute_memory(client, service, merchant=None, alert_type="big_amount")
    row = await item(service, merchant="CUALQUIERA", amount=Decimal("1000.00"))
    assert await alerting.emit(db, "big_amount", title="t", body="b", item_id=row.id, amount=row.amount, dedupe_key="a") is None
    assert await alerting.emit(db, "pending_review", title="t", body="b", item_id=row.id, amount=row.amount, dedupe_key="b") is not None

    assert (await client.patch(f"{API}/memories/{memory_id}", headers=ADMIN, json={"status": "archived"})).status_code == 200
    assert await alerting.emit(db, "big_amount", title="t", body="b", item_id=row.id, amount=row.amount, dedupe_key="c") is not None
    await client.patch(f"{API}/memories/{memory_id}", headers=ADMIN, json={"status": "active"})
    assert await alerting.emit(db, "big_amount", title="t", body="b", item_id=row.id, amount=row.amount, dedupe_key="d") is None

    assert (await client.delete(f"{API}/memories/{memory_id}", headers=ADMIN)).status_code == 200
    mute = await db.scalar(select(AlertMute))
    assert mute.active is False and mute.memory_id is None
    assert await alerting.emit(db, "big_amount", title="t", body="b", item_id=row.id, amount=row.amount, dedupe_key="e") is not None


# --- question alerts --------------------------------------------------------------------------------------------------


async def questions(db):
    return list((await db.scalars(select(AlertQuestion).order_by(AlertQuestion.id))).all())


async def test_question_answer_applies_and_learns(client, service, db, settings, fake_bulk):
    row = await doubtful(service, db, merchant="TIENDA UNO")
    assert await alert_questions.generate(db, settings) == 1
    question, = await questions(db)
    alert = await db.get(Alert, question.alert_id)
    assert alert.type == "question" and alert.item_id == row.id and alert.question == "¿Este cobro de $12.000 COP en TIENDA UNO es Transporte?"
    assert question.choices[0] == {"id": "c1", "label": "Transporte"} and question.choices[-1] == {"id": "skip", "label": "Ahora no"}

    listed = (await client.get(f"{API}/alerts", headers=ADMIN)).json()["items"]
    shown = next(entry for entry in listed if entry["id"] == alert.id)
    assert shown["choices"] == question.choices and shown["question_status"] == "open" and shown["question"] == alert.question

    invalid = await client.post(f"{API}/alerts/{alert.id}/answer", headers=ADMIN, json={"choice": "c9"})
    assert invalid.status_code == 422 and invalid.json()["code"] == "invalid_choice"

    answered = await client.post(f"{API}/alerts/{alert.id}/answer", headers=ADMIN, json={"choice": "c1"})
    assert answered.status_code == 200, answered.text
    assert answered.json()["result"] == {"action": "category", "category": "Transporte"} and answered.json()["alert"]["acked_at"] is not None
    assert len(fake_bulk) == 1 and fake_bulk[0].ids == [row.firefly_transaction_id] and fake_bulk[0].changes.category == "Transporte"
    await db.refresh(row)
    assert row.category == "Transporte"
    suggestion = await db.scalar(select(AiItemSuggestion))
    assert suggestion.status == "applied"
    # it learns: the owner's category counts for the merchant streak of rules.py
    streak = await db.scalar(select(MerchantCorrection))
    assert (streak.merchant, streak.category, streak.streak) == (rules.merchant_name("TIENDA UNO"), "Transporte", 1)
    assert (await audits(db, "alert.answer"))[0].detail["choice"] == "c1"

    again = await client.post(f"{API}/alerts/{alert.id}/answer", headers=ADMIN, json={"choice": "c1"})
    assert again.status_code == 409 and again.json()["code"] == "already_answered"
    assert len(fake_bulk) == 1


async def test_new_merchant_answer_creates_a_learned_rule(client, service, db, settings, fake_bulk):
    await item(service, merchant="PANADERIA SOL", category="Alimentación", amount=Decimal("3000.00"))  # gives the options
    new = await item(service, merchant="TIENDA NUEVA", category=None, amount=Decimal("45000.00"))
    db.add(Merchant(name=rules.merchant_name("TIENDA NUEVA"), count=1))
    await db.commit()
    assert await alert_questions.generate(db, settings) == 1
    question, = await questions(db)
    assert question.kind == "new_merchant" and question.item_id == new.id
    alert = await db.get(Alert, question.alert_id)
    assert "comercio nuevo" in alert.question
    response = await client.post(f"{API}/alerts/{alert.id}/answer", headers=ADMIN, json={"choice": "c1"})
    assert response.status_code == 200, response.text
    rule = await db.scalar(select(Rule))
    assert (rule.match, rule.pattern, rule.category, rule.source) == ("merchant_exact", rules.merchant_name("TIENDA NUEVA"), "Alimentación", "learned")
    assert response.json()["result"]["rule"]["id"] == rule.id and await audits(db, "rule.learn")
    # nothing is asked again about that merchant, and the rule makes it stop being «new without rule»
    assert await alert_questions.generate(db, settings) == 0


async def test_duplicate_question_voids_or_keeps(client, service, db, settings):
    original = await item(service)
    duplicate = await item(service, status="duplicate", duplicate_of_id=original.id, firefly_transaction_id=None, category=None)
    assert await alert_questions.generate(db, settings) == 1
    question, = await questions(db)
    assert question.kind == "duplicate" and [entry["id"] for entry in question.choices] == ["void", "keep", "skip"]
    response = await client.post(f"{API}/alerts/{question.alert_id}/answer", headers=ADMIN, json={"choice": "void"})
    assert response.status_code == 200, response.text
    await db.refresh(duplicate)
    assert duplicate.status == "rejected" and response.json()["result"]["action"] == "voided"
    assert await audits(db, "inbox.reject")


async def test_personal_question_on_a_shared_account(client, service, db, settings, add_own_account, fake_bulk):
    await add_own_account("Compartida")
    await item(service, merchant="GIMNASIO FIT", asset_account="Nequi", amount=Decimal("90000.00"))
    shared = await item(service, merchant="GIMNASIO FIT", asset_account="Compartida", amount=Decimal("90000.00"))
    assert await alert_questions.generate(db, settings) == 0  # no shared accounts configured

    configured = await client.put(f"{API}/settings/questions", headers=ADMIN, json={"daily_limit": 3, "shared_accounts": ["compartida"]})
    assert configured.status_code == 200 and configured.json()["shared_accounts"] == ["Compartida"]
    unknown = await client.put(f"{API}/settings/questions", headers=ADMIN, json={"daily_limit": 3, "shared_accounts": ["Nada"]})
    assert unknown.status_code == 422 and unknown.json()["code"] == "unknown_account"

    assert await alert_questions.generate(db, settings) == 1
    question, = await questions(db)
    assert question.kind == "personal" and question.item_id == shared.id
    response = await client.post(f"{API}/alerts/{question.alert_id}/answer", headers=ADMIN, json={"choice": "personal"})
    assert response.status_code == 200, response.text
    assert fake_bulk[0].changes.tags_add == ["personal"]
    memory = await db.scalar(select(Memory))
    assert memory.origin == "assistant" and "GIMNASIO FIT" in memory.text and "personales" in memory.text


async def test_skip_only_closes_the_question(client, service, db, settings, fake_bulk):
    await doubtful(service, db)
    await alert_questions.generate(db, settings)
    question, = await questions(db)
    response = await client.post(f"{API}/alerts/{question.alert_id}/answer", headers=ADMIN, json={"choice": "skip"})
    assert response.status_code == 200 and response.json()["result"] == {"action": "skip"} and fake_bulk == []
    assert (await db.get(Alert, question.alert_id)).acked_at is not None


async def test_stale_question_is_not_applied(client, service, db, settings, fake_bulk):
    row = await doubtful(service, db)
    await alert_questions.generate(db, settings)
    question, = await questions(db)
    row.status = "voided"
    await db.commit()
    response = await client.post(f"{API}/alerts/{question.alert_id}/answer", headers=ADMIN, json={"choice": "c1"})
    assert response.status_code == 409 and response.json()["code"] == "question_stale" and fake_bulk == []


async def test_failed_apply_leaves_the_question_open(client, service, db, settings, monkeypatch):
    from app.services.ledger import LedgerError

    async def broken(*args, **kwargs):
        raise LedgerError(422, "validation_error", "no")

    monkeypatch.setattr(ledger_service, "bulk", broken)
    await doubtful(service, db)
    await alert_questions.generate(db, settings)
    question, = await questions(db)
    response = await client.post(f"{API}/alerts/{question.alert_id}/answer", headers=ADMIN, json={"choice": "c1"})
    assert response.status_code == 409 and response.json()["code"] == "apply_failed"
    await db.refresh(question)
    assert question.status == "open" and (await db.get(Alert, question.alert_id)).acked_at is None


async def test_plain_alerts_are_not_answerable(client, db):
    alert = await alerting.emit(db, "question", title="t", body="b", question="¿Taxi?", options=["Sí", "No"], dedupe_key="legacy")
    await db.commit()
    response = await client.post(f"{API}/alerts/{alert.id}/answer", headers=ADMIN, json={"choice": "Sí"})
    assert response.status_code == 409 and response.json()["code"] == "question_not_answerable"
    assert (await client.post(f"{API}/alerts/9999/answer", headers=ADMIN, json={"choice": "x"})).status_code == 404
    shown = (await client.get(f"{API}/alerts", headers=ADMIN)).json()["items"][0]
    assert "choices" not in shown and shown["question"] == "¿Taxi?" and shown["options"] == ["Sí", "No"]


async def test_daily_limit_prioritizes_amount(client, service, db, settings):
    amounts = [5000, 90000, 20000, 70000, 10000]
    for number, amount in enumerate(amounts):
        await doubtful(service, db, merchant=f"COMERCIO {number}", amount=Decimal(amount))
    assert (await client.get(f"{API}/settings/questions", headers=ADMIN)).json() == {"daily_limit": 3, "shared_accounts": [], "asked_today": 0}
    assert await alert_questions.generate(db, settings) == 3
    assert sorted(question.amount for question in await questions(db)) == [Decimal("20000.00"), Decimal("70000.00"), Decimal("90000.00")]
    assert await alert_questions.generate(db, settings) == 0  # the rest stays an ordinary alert

    raised = await client.put(f"{API}/settings/questions", headers=ADMIN, json={"daily_limit": 4, "shared_accounts": []})
    assert raised.json()["asked_today"] == 3
    assert await alert_questions.generate(db, settings) == 1
    assert max(question.amount for question in await questions(db) if question.amount < Decimal("20000")) == Decimal("10000.00")
    assert (await client.put(f"{API}/settings/questions", headers=ADMIN, json={"daily_limit": 99, "shared_accounts": []})).status_code == 422


async def test_limit_zero_asks_nothing_and_confidence_bounds(client, service, db, settings):
    await doubtful(service, db, confidence=0.4, merchant="BAJA")  # below 0.5: not doubtful, just a weak guess
    await doubtful(service, db, confidence=0.9, merchant="ALTA")  # at or above the threshold: applied or suggested elsewhere
    assert await alert_questions.generate(db, settings) == 0
    await doubtful(service, db, confidence=0.5, merchant="BORDE")
    await client.put(f"{API}/settings/questions", headers=ADMIN, json={"daily_limit": 0, "shared_accounts": []})
    assert await alert_questions.generate(db, settings) == 0
    await client.put(f"{API}/settings/questions", headers=ADMIN, json={"daily_limit": 1, "shared_accounts": []})
    assert await alert_questions.generate(db, settings) == 1


async def test_a_muted_question_is_not_recorded(client, service, db, settings):
    db.add(AlertMute(alert_type="question", merchant=None, below_amount=Decimal("50000"), active=True))
    await doubtful(service, db, amount=Decimal("12000.00"))
    assert await alert_questions.generate(db, settings) == 0
    assert await questions(db) == []
    assert not (await db.scalars(select(Alert).where(Alert.type == "question"))).all()
