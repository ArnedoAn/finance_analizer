"""Declined, failed or rejected attempts never become movements: one rule (``is_declined`` over subject + body)
guards the parser, the LLM, the AI review and the automatic approval; only the owner's manual approval registers one."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.clients.llm import _PROMPT as EXTRACTION_PROMPT
from app.db.models import AiSuggestion
from app.models.schemas import ManualTransaction
from app.parsers import DECLINED_REASON, DECLINED_TEMPLATE, is_declined, parse
from app.services import ai_review

PAID = "Pagaste $12.000 en SUPERMERCADO CENTRAL"

PHRASES = [
    "Transacción rechazada", "Compra rechazada", "Pago rechazado", "Rechazo de la transacción", "Transacción declinada",
    "Tu compra fue declinada", "Transacción no aprobada", "Transacción no autorizada", "Transacción no exitosa",
    "Transacción fallida", "No fue posible completar tu compra", "No se pudo procesar tu pago", "Fondos insuficientes",
    "Saldo insuficiente", "Cupo insuficiente", "Transacción no procesada", "Tarjeta bloqueada",
    "La compra excede el límite diario", "La compra excede el monto máximo", "Intento de compra",
]

APPROVED_WITH_FOOTER = [
    PAID + ". Si su pago fue rechazado, contacte a su banco.",
    PAID + ". Si no reconoces esta transacción, llámanos de inmediato.",
    PAID + ". En caso de rechazo comunícate con nosotros.",
    "Comprobante de pago PSE. Estado: Aprobada. Valor $12.000 a SUPERMERCADO CENTRAL. Si su pago fue rechazado, contacte a su banco.",
    "Comprobante de pago PSE. Estado de la transacción: Aprobada. Valor $12.000 a SUPERMERCADO CENTRAL. Los pagos rechazados se reversan solos.",
]


async def item(service, source="email", **changes):
    values = dict(
        source=source, source_ref=str(uuid4()), sender="alertas@nequi.com.co", raw_text=PAID, subject_or_title="Compra",
        occurred_at=datetime.now(UTC), status="pending",
    )
    if source == "sms":
        values.update(sender="85954")
    values.update(changes)
    row = await service.inbox.add(**values)
    await service.session.commit()
    return row


def extracted(**changes):
    return dict(
        method="parser", amount=Decimal("12000.00"), currency="COP", merchant="SUPERMERCADO CENTRAL", asset_account="Nequi",
        transaction_type="withdrawal", original_kind="withdrawal", **changes,
    )


def nothing_written(clients):
    firefly, llm = clients
    firefly.create_transaction.assert_not_awaited()
    firefly.find_by_external_id.assert_not_awaited()
    firefly.create_account.assert_not_awaited()
    llm.analyze.assert_not_awaited()


def assert_ignored(row):
    assert row.status == "ignored" and row.review_reason == DECLINED_REASON and row.template_id == DECLINED_TEMPLATE


# --- the rule itself ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", PHRASES)
def test_every_phrase_is_declined_in_body_or_subject(phrase):
    assert is_declined(f"{phrase}. {PAID}")
    assert is_declined(f"Compra\n{phrase.upper()}")
    assert is_declined(f"{phrase}\n{PAID}".replace("ó", "o").replace("í", "i").replace("á", "a"))


@pytest.mark.parametrize("text", APPROVED_WITH_FOOTER + [PAID, "", None])
def test_warnings_and_approved_states_are_not_declines(text):
    assert not is_declined(text)


@pytest.mark.parametrize("phrase", PHRASES)
def test_the_bank_parser_ignores_every_phrase(phrase):
    for sender in ("87400", "alertas@nequi.com.co", ""):
        result = parse(f"{phrase}. {PAID}", "", sender)
        assert result is not None and (result.kind, result.template_id, result.reason) == ("ignore", DECLINED_TEMPLATE, DECLINED_REASON)
    assert parse(PAID, phrase, "87400").kind == "ignore"
    approved = parse(PAID, "Compra", "87400")
    assert approved is None or approved.kind != "ignore"


# --- the pipeline never writes a declined message ---------------------------------------------------------------------


@pytest.mark.parametrize("phrase", PHRASES)
async def test_declined_email_is_ignored_without_touching_firefly(service, clients, phrase):
    row = await service.process(await item(service, raw_text=f"{phrase}. {PAID}"))
    assert_ignored(row)
    nothing_written(clients)


@pytest.mark.parametrize("phrase", PHRASES)
async def test_declined_sms_is_ignored_without_touching_firefly(service, clients, phrase):
    row = await service.process(await item(service, source="sms", subject_or_title="85954", raw_text=f"{phrase}. {PAID}"))
    assert_ignored(row)
    nothing_written(clients)


async def test_a_declined_subject_with_a_neutral_body_is_ignored(service, clients):
    row = await service.process(await item(service, subject_or_title="Transacción rechazada", raw_text=PAID))
    assert_ignored(row)
    nothing_written(clients)


async def test_the_llm_is_not_asked_and_cannot_register_a_declined_message(service, clients, monkeypatch):
    monkeypatch.setattr("app.services.processor.parse", lambda *args, **kwargs: None)  # no template: the LLM would extract it
    row = await service.process(await item(service, raw_text="Tu compra no fue aprobada. " + PAID))
    assert_ignored(row)
    nothing_written(clients)


@pytest.mark.parametrize("raw_text", APPROVED_WITH_FOOTER)
async def test_an_approved_receipt_with_a_warning_footer_is_still_registered(service, clients, raw_text):
    row = await service.process(await item(service, raw_text=raw_text))
    assert row.status == "created"
    clients[0].create_transaction.assert_awaited_once()


async def test_reevaluation_and_retries_of_old_rows_ignore_declined_messages(service, clients):
    reviewed = await item(service, status="needs_review", raw_text="Fondos insuficientes. " + PAID, review_reason="merchant absent", **extracted())
    assert_ignored(await service.process(reviewed, reevaluate=True))
    waiting = await item(service, raw_text="Compra rechazada. " + PAID, **extracted(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert_ignored(await service.process(waiting))
    nothing_written(clients)


# --- the AI review cannot approve it -----------------------------------------------------------------------------------


def answer(**fields):
    return {
        "action": "approve", "firefly_account": "Nequi", "category": "Alimentación", "merchant": "Tienda Uno",
        "transaction_type": "withdrawal", "amount": None, "duplicate_of_id": None, "confidence": 0.99, "reason": "Comercio habitual",
    } | fields


async def test_ai_review_approval_of_a_declined_message_becomes_ignore(service, clients, db):
    firefly, llm = clients
    row = await item(service, status="needs_review", raw_text="Transacción rechazada. " + PAID, review_reason="merchant absent", **extracted())
    row_id = row.id
    llm.complete_json.return_value = answer()
    assert (await ai_review.suggest_items(db, firefly, llm, [row_id], auto=False))["suggested"] == 1
    suggestion = await db.scalar(select(AiSuggestion))
    assert (suggestion.action, suggestion.reason) == ("ignore", DECLINED_REASON)
    assert suggestion.category is None and suggestion.firefly_account is None
    applied, failed = await ai_review.apply(db, service, None, ids=[suggestion.id], all_above=None)
    assert (applied, failed) == ([suggestion.id], [])
    assert (await service.inbox.get(row_id)).status == "rejected"
    firefly.create_transaction.assert_not_awaited()


async def test_a_stored_ai_approval_of_a_declined_message_is_not_applied_in_bulk(service, clients, db):
    firefly, _ = clients
    row = await item(service, status="needs_review", raw_text="Saldo insuficiente. " + PAID, review_reason="merchant absent", **extracted())
    row_id = row.id
    stored = await ai_review._store(db, row_id, {"action": "approve", "confidence": 0.99, "firefly_account": "Nequi", "category": "Alimentación"})
    await db.commit()
    applied, failed = await ai_review.apply(db, service, None, ids=[], all_above=0.5)
    assert (applied, failed) == ([stored.id], [])
    assert (await service.inbox.get(row_id)).status == "rejected"
    firefly.create_transaction.assert_not_awaited()


# --- the owner decides -------------------------------------------------------------------------------------------------


async def test_the_owners_manual_approval_registers_a_declined_message(service, clients):
    row = await item(service, status="needs_review", raw_text="Transacción rechazada. " + PAID, review_reason="merchant absent", **extracted())
    done = await service.review(row, "approve", principal=None)
    assert done.status == "created"
    clients[0].create_transaction.assert_awaited_once()


async def test_the_owners_manual_entry_is_never_read_as_a_message(service, clients):
    row = await service.manual(ManualTransaction(
        client_ref="declined-words", amount=7000, description="Reembolso de compra rechazada", merchant="SUPERMERCADO CENTRAL", account="Nequi",
    ))
    assert row.status == "created"
    clients[0].create_transaction.assert_awaited_once()


# --- prompts -----------------------------------------------------------------------------------------------------------


def test_prompts_state_the_rule_and_the_categories_guidance():
    for prompt, field in ((EXTRACTION_PROMPT, "kind = ignore"), (ai_review._PROMPT, "action = ignore")):
        assert "REGLA" in prompt and f'{field} con reason "declined"' in prompt and "Nunca la" in prompt
    assert "DiDi, Uber, InDrive, Cabify → Transporte" in ai_review._PROMPT
    assert "Si ninguna aplica" in ai_review._PROMPT and "Nunca inventes categorías" in ai_review._PROMPT
